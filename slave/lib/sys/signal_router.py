# lib/sys/signal_router.py
# ═══════════════════════════════════════════════════════════════════════════
# 訊號 Router — 讓 ESP-NOW / 網路 / 實體線之間互相轉送（可設定，不寫死）
#
# 完整設計與所有被否決的方案 → doc/02_guides/16_signal_router.md
#
# 形態: 解碼鏈上的一道關卡（gate），**不是**另一個讀 rx_hub 的消費者。
#   AtomicStreamHub 是 SPSC — 多一個讀者會跟 BusDecodeTask 搶幀、
#   並在 decode_budget_slots 配額上互相餓死（見 doc §2.1）。
#
#    BusDecodeTask.loop()
#      ├─ poll 各 bus → rx_hub → StreamParser 解幀
#      ├─ ★ router.gate(bus, addr, cmd) → verdict   ← 本模組
#      └─ Dispatcher.dispatch()   ← 只有 verdict 允許才執行
#
# 設定（config.json，只有兩個欄位）:
#   "Router": {
#     "enable": 0,
#     "routes": [
#       { "in": "now",   "out": ["uart1"] },
#       { "in": "uart1", "out": ["self"] },
#       { "in": "net",   "out": ["uart2"] }
#     ]
#   }
#
# 規則:
#   enable=0              → 完全不作用（與未導入前 100% 相同）
#   in 沒配對             → 不執行、不轉發（沒配對＝沒路走）
#   out=["uart1"]         → 只轉發，本地不執行
#   out=["self"]          → 只本地執行，不轉發
#   out=["self","net"]    → 本地執行 ＋ 同時轉發
#   out 含 in             → 開機拒絕該 route（自我反射，永遠是錯的）
#
# 生命週期契約:
#   pack() 風格 — 本模組的 _forward() **同步**寫出，不持有 memoryview 跨呼叫。
#   組幀用 Proto.pack_into() 寫進本模組私有的 _tx_buf（不能借用 Proto 的共享
#   buffer: 一對多轉送時第二個目的地會拿到被覆蓋的髒資料）。
# ═══════════════════════════════════════════════════════════════════════════

# 協議常數 —— 一律沿用 proto.py，不在此重打
from lib.sys.proto import HDR_LEN, CRC_LEN, RX_BUF_SIZE, Proto

# gate() 回傳值（逐幀的判定）
V_EXECUTE = 1    # 進本地解碼
V_FORWARD = 2    # 轉送出去
V_BOTH = 3       # 兩者都做（out 同時含 self 與其他介面）
V_OK = 4         # Router 不介入，照原路徑執行（未啟用／無來源／無此設定）
V_DROP = 0       # 不執行、不轉發

# 保留字
SELF = "self"    # out 裡的保留字：進本地解碼鏈

# ── 邏輯名 ↔ bus label 的對應（唯一真相，只寫一次）──────────────────────
# 路由表用的是「邏輯名」（now / net / udp / uart1 / vbus），
# 而 BusDecodeTask 傳進來的 transport label 是各 bus 自帶的實體名。
# 這兩個名字本來就不同調，所以在此集中翻譯一次 —— 各 Task 不需要重複註冊別名。
#
#   now   ← NowBus.label        "NOW-Bus"
#   net   ← NetBus(TYPE_WS)     "CTRL-WS"      （控制通道；不叫 lan，因為 WS 也可能跑在 WiFi 上）
#   udp   ← NetBus(TYPE_UDP)    "UDP-DISCV"    （發現通道）
#   uartN ← CircuitBus(uartN)   "CIRCUIT-UARTn"
#   vbus  ← CircuitBus(io=None) "VBUS"
_LABEL_EXACT = {
    "NOW-BUS": "now",
    "CTRL-WS": "net",
    "UDP-DISCV": "udp",
    "VBUS": "vbus",
}
_LABEL_PREFIX = (
    ("CIRCUIT-UART", "uart"),
    ("CIRCUIT-", "uart"),        # 舊標籤相容
)
_KNOWN_IFACES = ("now", "net", "udp", "vbus", "self",
                 "uart1", "uart2", "uart3", "uart4")

# ── 介面來源（邏輯名 ↔ bus 服務名）────────────────────────────────────
# 由 BusDecodeTask 週期性呼叫 sync_ifaces() 補註冊（各 Task 上線順序不定）。
# 這裡**不 import sys_bus** —— registry 由呼叫端注入，本模組保持零裝置依賴，
# CPython 可離線測試（測試傳假 registry 即可）。
_IFACE_SERVICES = (
    ("now", "NowBus"),            # NetworkTask / NowTask
    ("net", "net_bus_ctrl"),      # NetworkTask（WS）
    ("udp", "net_bus_discovery"),  # NetworkTask（UDP 發現）
)
# 出口這側本來就全開（doc §3.2）: 沒進 CircuitDecode 的 UART 也能當出口，
# 所以從 circuit_bus_all_list 補齊，而不是只認 bus_sources 裡的那幾條。
_ALL_UART_SVC = "circuit_bus_all_list"
_BUS_SOURCES_SVC = "bus_sources"


def iface_name_from_label(label):
    """bus label → 邏輯名。認不出來就回原本的 label（讓它能被原樣查表）。"""
    if not label:
        return None
    key = label.upper()
    n = _LABEL_EXACT.get(key)
    if n is not None:
        return n
    for pre, base in _LABEL_PREFIX:
        if key.startswith(pre):
            return base + key[len(pre):]
    return label


def _is_bad_key(k):
    """config 裡殘留的、已被否決的欄位名（見 doc §9.1）— 提示用。"""
    return k in ("peers", "links", "protect", "bypass_cmds", "dedup_ms",
                 "max_hops", "max_fwd_per_sec", "stat_window_ms",
                 "ifaces", "marks", "self", "max_frame", "send_retry")


class SignalRouter:
    """路由表 + 匹配 + 轉送 + 統計。

    設計約束:
      - 不 import espnow / network / machine → CPython 可離線測試（test/protocol/router_selftest.py）
      - 不在 __init__ 讀 bus（初始化由呼叫端注入）→ 可單獨測
      - gate() 在熱路徑上: 不做字串切割、不配置物件
    """

    def __init__(self, label="Router", log=None):
        self.label = label
        self._log = log
        self.enable = False

        # 介面註冊表: 邏輯名 → 實際 bus 物件（由各 Task 註冊）
        self.ifaces = {}
        # 反查: id(bus 物件) → 邏輯名（gate 的熱路徑用，避免走訪 dict）
        self._by_id = {}

        # 路由表: 邏輯名 → spec dict（一個來源恰好一條）
        self.by_in = {}

        # 每個來源介面被 gate 看到的次數（診斷用）
        self.seen = {}

        # 統計: 鍵為 "event" 或 "iface:event" 或 "iface:iface:event"
        self.stats = {}

        # 轉送用的私有 buffer（惰性配置，永久重用）
        # 為什麼不直接用 Proto.pack(): 它回傳指向模組級共享 buffer 的 memoryview，
        # 下一個 pack() 就覆蓋 —— 一對多轉送時第二個目的地會拿到髒資料。
        self._tx_buf = None
        self._tx_mv = None

        self._errors = []    # load() 期間的錯誤訊息（開機除錯用）

        # ── 自動註冊（每次啟動都跑，不是開關）──────────────────────────
        # 開機時檢視「實際存在的線路」，config 裡沒對應 route 的就自動補一條
        # {in: 線路, out: ["self"]}（＝該線自己收自己執行，與未導入 Router 前相同），
        # 並由呼叫端寫回 config.json —— 使用者打開 config 就看到全部真實線路，
        # 只要改想轉送的那幾條。
        #   _auto_done : 已經自動檢查過的線路（同一 session 不重複補；
        #                ROUTE_DEL 刪掉後不會被下一次 sync 又補回來）
        #   _autofilled: 這次補了哪幾條，等呼叫端 take_autofill() 取走去存檔
        self._auto_done = set()
        self._autofilled = []

    # ─────────────────────────────────────────────────────────────
    # 介面註冊（各 Task 在 on_start 呼叫；重複註冊同一 name 為 no-op）
    # ─────────────────────────────────────────────────────────────
    def register_iface(self, name, bus_obj, label=None):
        """把邏輯名綁到實際 bus 物件（各 Task 在 on_start 呼叫）。

        name  : 路由表用的邏輯名（"uart1" / "now" / "net" / "udp" / "vbus"）
        bus_obj: 有 .label 與 .write() 的物件（NetBus / CircuitBus / NowBus）
        label : 選填；bus_obj 沒有 .label 時用來補（比對 decode 送的 label）

        回傳 True；bus_obj 為 None 時回 False。
        （介面尚未上線時傳 None 是正常的 —— 例如 ESP-NOW 沒開。）
        """
        if bus_obj is None:
            return False
        self.ifaces[name] = bus_obj
        self._by_id[id(bus_obj)] = name
        if label and not getattr(bus_obj, "label", None):
            try:
                bus_obj.label = label
            except Exception:
                pass
        return True

    def unregister_iface(self, name):
        obj = self.ifaces.pop(name, None)
        if obj is not None:
            self._by_id.pop(id(obj), None)
        return obj is not None

    def name_of(self, bus_obj):
        """bus 物件 → 邏輯名。

        優先序:
          1. register_iface 註冊過 → 直接命中
          2. bus_obj.label 經 iface_name_from_label 翻譯（"NOW-Bus" → "now"）
        兩者都沒有則回 None（gate 會視為 V_OK，不介入）。
        """
        n = self._by_id.get(id(bus_obj))
        if n is not None:
            return n
        return iface_name_from_label(getattr(bus_obj, "label", None))

    def sync_ifaces(self, registry):
        """把「已經上線的通道」補註冊進來。回傳這次新註冊的數量。

        為什麼需要: 各 Task 的上線順序不固定（NetworkTask / CircuitTask /
        ScheduleTask 的 vBus 都是惰性建立），Router 不該假設某條線一定存在。
        由 BusDecodeTask 在既有的 100ms 節流區塊裡呼叫，重複呼叫是 no-op。

        registry: 任何有 `get_service(name)` 的物件（實際上就是 lib.sys.sys_bus.bus）。
                  刻意用注入而不是 import —— 本模組保持零裝置依賴，CPython 可離線測。

        來源三處（依權威性排序）:
          1. 具名服務   → now / net / udp
          2. 全部 UART  → circuit_bus_all_list（含**沒進 CircuitDecode** 的線）
          3. 解碼來源   → bus_sources（vbus 等沒有具名服務的線）
        """
        get = getattr(registry, "get_service", None)
        if get is None:
            return 0
        n = 0

        for name, svc in _IFACE_SERVICES:
            obj = get(svc)
            if obj is None or self.ifaces.get(name) is obj:
                continue
            old = self.ifaces.get(name)
            if old is not None:
                self._by_id.pop(id(old), None)
            if self.register_iface(name, obj):
                n += 1

        for cb in (get(_ALL_UART_SVC) or ()):
            nm = iface_name_from_label(getattr(cb, "label", None))
            if nm and self.ifaces.get(nm) is not cb:
                if self.register_iface(nm, cb):
                    n += 1

        srcs = get(_BUS_SOURCES_SVC)
        lst = None
        if srcs is not None:
            try:
                lst = srcs.list()
            except Exception:
                lst = None
        for b in (lst or ()):
            nm = iface_name_from_label(getattr(b, "label", None))
            if nm and self.ifaces.get(nm) is not b:
                if self.register_iface(nm, b):
                    n += 1

        # ★ 每次啟動（與每次有新線路上線）都跑一次：沒 route 的線路自動補 self
        self._autofill()

        return n

    # ─────────────────────────────────────────────────────────────
    # 自動註冊 —— 每次啟動的固定動作（沒有開關）
    # ─────────────────────────────────────────────────────────────
    def _autofill(self):
        """為「實際存在、但 config 裡沒有 route」的線路補上 `out: ["self"]`。

        為什麼是 self: 這正是未導入 Router 前的行為（每條線自己收、自己執行）。
        自動補完之後，**打開 `enable:1` 不會讓任何一條線失效** —— 使用者只需要改
        想轉送的那幾條，不必先把每條線都寫一遍。

        只對「這一輪新看到的線路」動手（`_auto_done`）:
          - 同一條線不會被重複補，也不會在使用者 ROUTE_DEL 之後又被補回來
          - 但**每次啟動都是全新的一輪**（`_auto_done` 清空）→ 一定會重新檢查建立

        回傳這一輪補出來的來源名稱（給呼叫端寫回 config.json）。
        """
        new = []
        for name in sorted(self.ifaces.keys()):
            if name in self._auto_done:
                continue
            self._auto_done.add(name)
            if name in self.by_in:
                continue                      # 使用者已經寫了 → 尊重他
            self.by_in[name] = {
                "id": len(self.by_in),
                "in": name,
                "out": (SELF,),
                "verdict": V_EXECUTE,
                "auto": True,
                "hit": 0, "fwd": 0, "drop": 0, "nomatch": 0,
            }
            new.append(name)
        if new:
            self._autofilled.extend(new)
        if self.enable and not self.by_in:
            # 開著卻什麼都沒有（連一條線路都沒上線）—— 這種才值得出聲
            self._warn_once("_empty", "enable=1，但沒有任何線路也沒有任何 route "
                                      "→ 所有幀都不執行（等同關閉）")
        return new

    def take_autofill(self):
        """取走（並清空）「自動補出來的來源」清單 —— 呼叫端據此決定要不要存檔。"""
        out = self._autofilled
        self._autofilled = []
        return out

    # ─────────────────────────────────────────────────────────────
    # 設定載入
    # ─────────────────────────────────────────────────────────────
    def load(self, cfg):
        """從 config.json 的 Router 區塊載入。回傳 route 條數。

        不 raise —— 壞設定一律「記錄 + 跳過該條」，讓其它路線照常運作。
        這也是修掉 CircuitDecode「靜默失敗」毛病的做法: 不確定就出聲，不猜。
        """
        self._errors = []
        self.by_in = {}
        self.seen = {}
        self.stats = {}
        # 每次啟動都是全新的一輪自動註冊（見 _autofill）
        self._auto_done = set()
        self._autofilled = []

        if not cfg:
            self.enable = False
            return 0
        if not isinstance(cfg, dict):
            # 型別錯也要活著: 記錄 + 關閉，不要讓呼叫端整個 router=None（解碼照常）
            self._bad("Router 設定必須是物件，實際是 {}".format(type(cfg).__name__))
            self.enable = False
            return 0

        self.enable = bool(int(cfg.get("enable", 0) or 0))

        # config 殘留欄位提示（我們討論中被否決的參數，見 doc §9.1）
        for k in cfg.keys():
            if _is_bad_key(k):
                self._warn("config 有已移除的欄位 '{}' → 已忽略（見 doc §9.1）".format(k))

        routes = cfg.get("routes")
        if routes is None:
            self._warn("缺少 routes 欄位 → 路由表為空（所有介面都不執行）")
            return 0
        if not isinstance(routes, (list, tuple)):
            self._bad("routes 必須是列表，實際是 {}".format(type(routes).__name__))
            return 0

        n = 0
        for idx, r in enumerate(routes):
            if self._add_route(idx, r):
                n += 1

        # ⚠️ 不在這裡警告「enable=1 卻沒有 route」—— 自動註冊會在 sync_ifaces()
        #    之後把實際存在的線路補上，`routes: []` 是**正常初始狀態**。
        #    真的「沒線路也沒 route」時由 _autofill() 出聲（那裡才知道線路狀況）。
        return n

    def _add_route(self, idx, r, overwrite_ok=False, where=None):
        """驗證並加入一條 route。回傳 True/False。

        overwrite_ok: 執行期 ROUTE_ADD 的「覆寫」是**預期行為**，不再印重複警告。
        where       : 訊息前綴（開機用 routes[i]，執行期用 ROUTE_ADD）。
        """
        tag = where or "routes[{}]".format(idx)
        if not isinstance(r, dict):
            self._bad("{} 不是物件".format(tag))
            return False

        # ── in: 純量字串 ──────────────────────────────────
        src = r.get("in")
        if isinstance(src, (list, tuple)):
            self._bad("{}: 'in' 必須是單一介面名稱，不可為列表 "
                      "（一個來源 = 一條 route；要多個請寫多條）".format(tag))
            return False
        if not isinstance(src, str) or not src.strip():
            self._bad("{}: 'in' 必須是非空字串".format(tag))
            return False
        src = src.strip()

        # ── out: 一律列表（字串容忍但提示）────────────────
        dst = r.get("out")
        if dst is None:
            self._bad("{}: 缺少 'out'".format(tag))
            return False
        if isinstance(dst, str):
            self._warn("{}: 'out' 建議一律寫列表，例如 [\"{}\"]".format(tag, dst))
            dst = [dst]
        if not isinstance(dst, (list, tuple)):
            self._bad("{}: 'out' 必須是列表".format(tag))
            return False

        outs = []
        for o in dst:
            if not isinstance(o, str) or not o.strip():
                self._bad("{}: 'out' 內含非字串項 → 整條跳過".format(tag))
                return False
            outs.append(o.strip())

        if not outs:
            self._warn("{}: 'out' 是空的 → 這條 route 沒有作用".format(tag))

        # ── 結構檢查（永遠是錯的設定，開機就拒絕）──────────
        if src in outs:
            self._bad("{}: 'out' 不可包含 'in'（{}）— 自我反射，永遠是錯的"
                      .format(tag, src))
            return False

        # ── 已否決的欄位提示 ──────────────────────────────
        for k in r.keys():
            if k not in ("in", "out") and _is_bad_key(k):
                self._warn("{}: 欄位 '{}' 已移除 → 已忽略".format(tag, k))

        # ── 順序（後者覆寫前者）────────────────────────────
        if src in self.by_in and not overwrite_ok:
            self._warn("來源 '{}' 重複定義 → 以最後一條為準".format(src))

        self.by_in[src] = {
            "id": idx,
            "in": src,
            "out": tuple(outs),
            "verdict": self._verdict_of(outs),
            "auto": False,
            # 統計
            "hit": 0,
            "fwd": 0,
            "drop": 0,
            "nomatch": 0,
        }

        # ── 已知的成環風險提示（不禁止，見 doc §7.2）──────
        if "udp" in outs:
            self._warn("{}: 'out' 含 'udp' — udp 對面是會自動回話的 discovery "
                       "服務，若另有 {{'in':'udp'}} 的反向規則就會成環".format(tag))

        return True

    @staticmethod
    def _verdict_of(outs):
        has_self = SELF in outs
        has_fwd = any(o != SELF for o in outs)
        if has_self and has_fwd:
            return V_BOTH
        if has_self:
            return V_EXECUTE
        if has_fwd:
            return V_FORWARD
        return V_DROP    # out 為空

    # ─────────────────────────────────────────────────────────────
    # 執行期增刪（P5 的 ROUTE_ADD / ROUTE_DEL / TABLE_GET / SAVE 用）
    # ─────────────────────────────────────────────────────────────
    def route_add(self, route):
        """新增或覆寫一條 route（驗證邏輯與開機 load() 完全相同，不另寫一套）。

        回傳 (ok: bool, message: str)。**不 raise** —— 錯誤一律變成訊息給呼叫端。
        """
        if not isinstance(route, dict):
            return False, "route 必須是物件（{\"in\":..., \"out\":[...]}）"
        src = route.get("in")
        if isinstance(src, str):
            src = src.strip()
        before = len(self._errors)
        prev = self.by_in.get(src) if isinstance(src, str) else None
        ok = self._add_route(prev["id"] if prev else len(self.by_in), route,
                             overwrite_ok=True, where="ROUTE_ADD")
        if ok:
            spec = self.by_in[src]
            return True, "route '{}' → {} 已{}".format(
                src, list(spec["out"]), "覆寫" if prev else "加入")
        errs = self._errors[before:]
        return False, (errs[-1] if errs else "route 驗證失敗（詳見開機訊息）")

    def route_del(self, in_name):
        """依 `in` 刪除一條 route。回傳 (ok, message)。"""
        if not isinstance(in_name, str) or not in_name.strip():
            return False, "'in' 必須是非空字串"
        name = in_name.strip()
        if name not in self.by_in:
            return False, "找不到來源 '{}'".format(name)
        del self.by_in[name]
        return True, "已刪除來源 '{}' 的 route".format(name)

    def set_enable(self, on):
        """開關 Router（執行期）。回傳切換後的值。"""
        self.enable = bool(on)
        return self.enable

    def table(self, page=0, page_size=32):
        """回傳可 JSON 化的路由表分頁（ROUTER_TABLE_GET 用）。

        **只列「線路現在真的存在」的來源**（`in` 在 `ifaces` 裡）——
        config 寫了、實體不存在的 route 不生效也不列出來（無視它、跳過它的建立），
        它留在 config 裡，線路上線就自動生效。想知道哪些被跳過看 status()['unbound']。

        page_size 只是為了讓 payload 守在 MAX_PAYLOAD(8192) 以內 —— 實務上
        路由表只有個位數條，第一頁就全部回完。
        """
        names = sorted(n for n in self.by_in.keys() if n in self.ifaces)
        total = len(names)
        try:
            size = int(page_size)
        except Exception:
            size = 32
        if size <= 0:
            size = 32
        pages = (total + size - 1) // size
        if pages <= 0:
            pages = 1
        try:
            pg = int(page)
        except Exception:
            pg = 0
        if pg < 0:
            pg = 0
        if pg >= pages:
            pg = pages - 1

        start = pg * size
        items = []
        for name in names[start:start + size]:
            s = self.by_in[name]
            items.append({"in": s["in"], "out": list(s["out"])})
        return {
            "page": pg, "pages": pages, "page_size": size,
            "total": total, "routes": items,
        }

    def snapshot(self):
        """目前生效的設定（可寫回 config.json 的 Router 區塊）。"""
        return {
            "enable": 1 if self.enable else 0,
            "routes": [{"in": n, "out": list(self.by_in[n]["out"])}
                       for n in sorted(self.by_in.keys())],
        }

    # ─────────────────────────────────────────────────────────────
    # gate — 熱路徑（BusDecodeTask 每幀呼叫一次）
    # ─────────────────────────────────────────────────────────────
    def gate(self, bus_obj, addr, cmd, payload=None):
        """逐幀判定。

        回傳:
          V_OK      Router 不介入，照原路徑執行（＝未導入 Router 前的行為）
          V_EXECUTE 只本地執行（不轉發）
          V_FORWARD 只轉發（本地不執行）
          V_BOTH    本地執行 ＋ 轉發
          V_DROP    不執行、不轉發

        呼叫端（app.py::handle_stream）只需判斷:
          if verdict != V_OK and not (verdict & V_EXECUTE): continue
          if verdict != V_OK and (verdict & V_FORWARD): (已在本函式內轉送)
        """
        if not self.enable:
            return V_OK
        name = self.name_of(bus_obj)
        if name is None:
            return V_OK
        self.seen[name] = self.seen.get(name, 0) + 1

        spec = self.by_in.get(name)
        if spec is None:
            # 「去解碼的就填寫，沒寫的就空著」— 沒配對＝沒路走
            self._bump(name, "nomatch")
            return V_DROP

        spec["hit"] += 1
        verdict = spec["verdict"]

        if verdict & V_FORWARD:
            self._forward(spec, bus_obj, addr, cmd, payload)

        return verdict

    # ─────────────────────────────────────────────────────────────
    # 轉送
    # ─────────────────────────────────────────────────────────────
    def _forward(self, spec, src_bus, addr, cmd, payload):
        """把幀送到 spec['out'] 的每個出口（self 已由 verdict 處理，不在這裡）。

        契約: 逐出口同步寫出；**每個出口各組一份自己的 bytes**
        （不能共用 Proto 的模組級共享 buffer — 一對多時第二個會拿到髒資料）。
        """
        outs = spec["out"]
        frame = None
        for name in outs:
            if name == SELF:
                continue
            dst = self.ifaces.get(name)
            if dst is None:
                self._warn_once(name, "介面 '{}' 尚未註冊 → 無法轉送（該幀丟棄）".format(name))
                spec["drop"] += 1
                self._bump(name, "no_iface")
                continue
            write = getattr(dst, "write", None)
            if write is None:
                spec["drop"] += 1
                self._bump(name, "no_write")
                continue

            if frame is None:
                frame = self._build_frame(addr, cmd, payload)
                if frame is None:
                    spec["drop"] += 1
                    self._bump(name, "build_fail")
                    return

            try:
                ok = write(frame)
                if ok is False:
                    spec["drop"] += 1
                    self._bump(name, "tx_fail")
                else:
                    spec["fwd"] += 1
                    self._bump(name, "fwd")
            except Exception as e:
                spec["drop"] += 1
                self._bump(name, "tx_err")
                self._warn_once(name, "轉送到 '{}' 失敗: {}".format(name, e))

    def _build_frame(self, addr, cmd, payload):
        """組出要送出的 NC4 幀（本模組私有 buffer，不用 Proto 的共享 buffer）。

        P2: 組幀內核已搬進 `Proto.pack_into()`（與 `pack()` 共用 `_write_frame`），
        本模組只負責「自己的 buffer + 生命週期」。輸出與 `Proto.pack()` 位元組完全相同
        （selftest 逐位元組對比）。

        payload 為 None 時只重組 header+CRC（無負載幀）。
        """
        p = payload
        if p is None:
            p = b""
        elif not isinstance(p, (bytes, bytearray, memoryview)):
            p = bytes(p)
        total = HDR_LEN + len(p) + CRC_LEN
        if total > RX_BUF_SIZE:
            self._warn_once("_oversize", "訊框 {}B 超過 RX_BUF_SIZE({}) → 不轉送"
                            .format(total, RX_BUF_SIZE))
            return None

        # 惰性配一次，永久重用：一幀最多 RX_BUF_SIZE，裝得下就不用再長
        if self._tx_buf is None:
            self._tx_buf = bytearray(RX_BUF_SIZE)
            self._tx_mv = memoryview(self._tx_buf)

        if Proto.pack_into(self._tx_mv, 0, cmd, p, addr) < 0:
            self._warn_once("_oversize", "訊框 {}B 超過轉送 buffer({}) → 不轉送"
                            .format(total, RX_BUF_SIZE))
            return None
        return self._tx_mv[:total]

    # ─────────────────────────────────────────────────────────────
    # 家事（BusDecodeTask.loop() 尾端呼叫；不做資料路徑的事）
    # ─────────────────────────────────────────────────────────────
    def housekeep(self):
        """目前無狀態需要清理（防環機制與 window 參數皆已否決，見 doc §7.3）。

        保留這個方法作為**介面契約**: 之後若要加「非同步重送／佇列」就掛在這裡，
        呼叫端不用改。成本: 一次函式呼叫。
        """
        return

    # ─────────────────────────────────────────────────────────────
    # 診斷
    # ─────────────────────────────────────────────────────────────
    def status(self):
        """回傳可 JSON 化的狀態（P5 的 ROUTER_STATUS 用）。"""
        routes = []
        for name in sorted(self.by_in.keys()):
            s = self.by_in[name]
            routes.append({
                "id": s["id"], "in": s["in"], "out": list(s["out"]),
                "auto": s.get("auto", False),
                "live": name in self.ifaces,
                "hit": s["hit"], "fwd": s["fwd"], "drop": s["drop"],
            })
        return {
            "enable": self.enable,
            "ifaces": sorted(self.ifaces.keys()),
            # 寫了但實體不存在 → 無視它、跳過它的建立（route 仍在 config 裡等上線）
            "unbound": [n for n in self.by_in.keys() if n not in self.ifaces],
            "routes": routes,
            "seen": dict(self.seen),
            "stats": dict(self.stats),
            "errors": list(self._errors),
        }

    def _bump(self, name, event):
        k = name + ":" + event
        self.stats[k] = self.stats.get(k, 0) + 1

    def errors(self):
        """load() 期間的錯誤清單（副本；呼叫端拿去決定要不要印/回報）。"""
        return list(self._errors)

    def _bad(self, msg):
        self._errors.append(msg)
        self._emit("❌ [{}] {}".format(self.label, msg))

    def _warn(self, msg):
        self._emit("⚠️ [{}] {}".format(self.label, msg))

    def _warn_once(self, key, msg):
        k = "_wo:" + str(key)
        if self.stats.get(k):
            return
        self.stats[k] = 1
        self._emit("⚠️ [{}] {}".format(self.label, msg))

    def _emit(self, msg):
        if self._log is not None:
            try:
                self._log(msg)
                return
            except Exception:
                pass
        print(msg)
