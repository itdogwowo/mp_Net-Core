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
#       { "in": "now",   "out": ["uart0"] },
#       { "in": "uart0", "out": ["self"] },
#       { "in": "self",  "out": ["self"] },      ← 本機發起的幀（vBus 注入）
#       { "in": "net",   "out": ["uart1"] }
#     ]
#   }
#   （uartN 的 N = UART.list 的索引，0-based；見 circuit.py 與 doc §3.3）
#
# 規則:
#   enable=0              → 完全不作用（與未導入前 100% 相同）
#   in 沒配對             → 不執行、不轉發（沒配對＝沒路走）
#   out=["uart0"]         → 只轉發，本地不執行
#   out=["self"]          → 只本地執行，不轉發
#   out=["self","net"]    → 本地執行 ＋ 同時轉發
#   out=[]                → 明確不執行也不轉發
#   out 含 in             → 開機拒絕該 route（自我反射，永遠是錯的）
#
# ★ "self" 同時是**來源**也是**目的地**（對稱）:
#     in:  "self"   → 本機發起的幀（vBus 注入等 io=None 的迴路；見 is_local_bus）
#     out: ["self"] → 進本地解碼鏈執行
#   本機來源**不進 ifaces**（ifaces 是出口表），要轉發自己請寫
#   { "in": "self", "out": ["now"] } —— out 那側才是出口。
#
# ⚠️ 要「不受路由政策影響、絕對執行」時，不要繞 Router —— 那會連 CRC 與
#    ADDR 過濾都跳過。程式內部直呼 handler 請用 app.disp.dispatch()
#    （見 tasks/web_ui.py 的 /api/cmd），語意是「我呼叫一個函式」，
#    而不是「我假裝收到一幀」。兩者場合不同，不要混用。
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
# 路由表用的是「邏輯名」（now / net / udp / uart0 / self），
# 而 BusDecodeTask 傳進來的 transport label 是各 bus 自帶的實體名。
# 這兩個名字本來就不同調，所以在此集中翻譯一次 —— 各 Task 不需要重複註冊別名。
#
#   now   ← NowBus.label        "NOW-Bus"
#   net   ← NetBus(TYPE_WS)     "CTRL-WS"      （控制通道；不叫 lan，因為 WS 也可能跑在 WiFi 上）
#   udp   ← NetBus(TYPE_UDP)    "UDP-DISCV"    （發現通道）
#   uartN ← CircuitBus(uartN)   "CIRCUIT-UARTn"  ← N = UART.list 的**索引(0-based)**
#                                                 （不是 config 的 `id`；見 circuit.py 的說明）
#   self  ← CircuitBus(io=None) "VBUS"           ← 本機來源（見 is_local_bus）
_LABEL_EXACT = {
    "NOW-BUS": "now",
    "CTRL-WS": "net",
    "UDP-DISCV": "udp",
    "VBUS": "self",          # ★ vBus 的實質就是「把幀餵回自己的解碼鏈」
}                            #   → 它是**來源 SELF**，不是一條叫 vbus 的出口
_LABEL_PREFIX = (
    ("CIRCUIT-UART", "uart"),
    ("CIRCUIT-", "uart"),        # 舊標籤相容
)
_KNOWN_IFACES = ("now", "net", "udp", "self",
                 "uart0", "uart1", "uart2", "uart3")

# 幾乎必然存在的來源 —— autofill **不判斷**它存不存在，一律進路由表。
#   self : 本機發起的幀。任何裝置都有「自己」，不需要等 vBus 上線。
#
# ⚠️ vBus **不在這裡**（2026-09 定案）：它「只負責發送」，不是一條可路由的來源。
#    它的幀一律以來源名 `self` 進 Router（見 is_local_bus），
#    所以在路由表裡不該出現 `in: "vbus"` 這種條目 —— 那會是第二個名字指同一件事。
#    使用者在 in 寫 vbus 會被當成「不存在的通道」跳過；在 out 寫 vbus 會被無視。
ALWAYS_PRESENT = (SELF,)


def is_local_bus(obj):
    """這個 bus 是不是「本機來源」（把幀餵回自己解碼鏈的迴路）。

    判準：CircuitBus 的 `io is None` —— 它不接任何實體腳位，唯一用途就是
    被注入 rx_hub（`schedule._get_vbus()` 的 `CircuitBus(None, label="VBUS")`）。
    全樹只有 vBus 一個（真實線路都帶 uart 物件），所以這個判準穩定。

    為什麼不用 label 判斷：label 是給 log 看的名字，機制不該綁在字串上；
    哪天有人改 label，來源身分不該跟著壞掉。
    """
    return obj is not None and getattr(obj, "io", "missing") is None

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


def _route_order(name):
    """路由表的排序鍵：**`self` 排第一**，其餘照字母序。

    為什麼 self 要排第一：它是本機來源，讀路由表的人第一眼就該看到
    「我自己發的幀往哪走」，而不是在一堆線路名中間找它。
    （用「self 優先」而不是字典序 —— 字典序會讓它夾在 now 與 uart0 之間。）
    """
    return (0, "") if name == SELF else (1, name)


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
        #   ⚠️ 2026-09：**不再寫回 config.json**（`_persist_autofill()` 只記錄）。
        #      原因：使用者刪掉的 route 會被補回來並寫進檔案、通道晚上線會被
        #      預設值覆蓋並寫進檔案 —— 設定檔會自己長出他沒寫過的東西。
        #      要落盤請明確用 ROUTER_SAVE（0x1605）。
        #   本機來源（vBus，見 is_local_bus）也會被補成 {in: "self", out: ["self"]}，
        #   所以「自己發的指令會被自己執行」是預設行為，不必手寫。
        #   _auto_done : 已經自動檢查過的線路（同一 session 不重複補；
        #                ROUTE_DEL 刪掉後不會被下一次 sync 又補回來）
        #   _autofilled: 這次補了哪幾條，等呼叫端 take_autofill() 取走去存檔
        #   _locals    : 本機迴路（vBus 等）。它們不進 ifaces（那是出口表），
        #                所以 _autofill 要另外走這一份，否則會「隱形」不被補 route。
        self._auto_done = set()
        self._autofilled = []
        self._locals = {}
        # 使用者寫了、但通道還不確定的 route（等 sync_ifaces 結算，見 load）
        self._pending = []
        self._skipped = []

    # ─────────────────────────────────────────────────────────────
    # 介面註冊（各 Task 在 on_start 呼叫；重複註冊同一 name 為 no-op）
    # ─────────────────────────────────────────────────────────────
    def register_iface(self, name, bus_obj, label=None):
        """把邏輯名綁到實際 bus 物件（各 Task 在 on_start 呼叫）。

        name  : 路由表用的邏輯名（"uart0" / "now" / "net" / "udp"）
                —— **不要傳 "self"**：本機來源不進 ifaces（見 is_local_bus），
                   它由 name_of() 直接判別，不需要註冊。出口側才需要 ifaces。
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
          1. 本機來源（io=None 的迴路，如 vBus）→ **SELF**（"self"）
             —— 這條是「來源」不是「出口」：本機發起的幀，其來源就是自己。
          2. register_iface 註冊過 → 直接命中
          3. bus_obj.label 經 iface_name_from_label 翻譯（"NOW-Bus" → "now"）
        都沒有則回 None（gate 會視為 V_OK，不介入）。
        """
        if is_local_bus(bus_obj):
            return SELF
        n = self._by_id.get(id(bus_obj))
        if n is not None:
            return n
        return iface_name_from_label(getattr(bus_obj, "label", None))

    def _is_live(self, name):
        """這條 route 的來源「現在真的存在」嗎？

        特例（見 ALWAYS_PRESENT）：`self` 與 `vbus` 一律算存在 ——
        它們是語意上必然有的來源，不該因為「本機迴路還沒被建立」
        就被誤報成 unbound（寫了卻不存在的來源）。
        其餘照舊查 ifaces。
        """
        if name in ALWAYS_PRESENT:
            return True
        return name in self.ifaces

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
          3. 解碼來源   → bus_sources（其餘沒有具名服務的線；
                          本機迴路 vBus 也在這裡，但登記成來源 self）
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
            # 本機來源（vBus 等 io=None 的迴路）**不進 ifaces** —— ifaces 是「出口表」，
            # 而本機來源只當來源（見 name_of 的說明）。它由 _by_id 對應到 SELF。
            if is_local_bus(b):
                self._locals[id(b)] = b
                self._by_id[id(b)] = SELF
                continue
            nm = iface_name_from_label(getattr(b, "label", None))
            if nm and self.ifaces.get(nm) is not b:
                if self.register_iface(nm, b):
                    n += 1

        # ── 先看表：使用者寫的 route，通道在就註冊 ────────────────
        #   **一定要排在 autofill 前面** —— 順序反了的話，新通道會先被
        #   autofill 標成「已處理」，使用者的 route 才被判跳過（實際踩過）。
        self._reconcile_pending()

        return n

    def _reconcile_pending(self):
        """把「使用者寫了、且通道現在存在」的 route 補進表；其餘留在 pending。

        `load()` 跑在 `sync_ifaces()` 之前，開機當下通道都還沒註冊，
        所以使用者的 route 必須延後到這裡結算 —— 但**不能一次就判死**：
        通道可能是幾秒後才上線的（vBus 惰性建立、ESP-NOW 等 WiFi 就緒），
        所以「還沒看到」的繼續留在 `_pending` 等下一輪。
        真的不存在的，由 `finalize()` 在開機結束時統一判跳過。
        """
        if not self._pending:
            return
        still = []
        for idx, r in self._pending:
            src = r.get("in")
            if not isinstance(src, str):
                continue
            if src.strip() in self.ifaces:
                # 通道在 → 用**使用者寫的**（不是 autofill 的預設值）
                self._add_route(idx, r, overwrite_ok=True,
                                where="routes[{}]".format(idx))
            else:
                still.append((idx, r))
        self._pending = still

    def finalize(self):
        """開機結束時呼叫一次：結算 pending，然後補預設。

        為什麼要分開（不再由 sync_ifaces 每輪做）：
          1. `sync_ifaces()` 每 ~100ms 跑一次。若它每次都補預設，
             **任何通道晚上線都會立刻被填預設值** —— 包含使用者已經寫了、
             只是還沒輪到結算的那些。
          2. 使用者的規格：補預設**只在開機做一次 + 使用者主動要求**。

        ★ pending **不在此清空** —— 通道可能幾分鐘後才上線（vBus 惰性建立、
          ESP-NOW 等 WiFi 就緒），那時 `sync_ifaces()` 的 `_reconcile_pending()`
          會用**使用者寫的內容**把它註冊進表（而不是 autofill 的預設值）。

        回傳這次補出來的來源名稱。
        """
        self._reconcile_pending()
        # 還沒對到的通道 → 這一輪不註冊（不進表、不生效、不顯示），
        # 但**保留在 _pending** 等它上線。記一筆讓使用者知道就好。
        if self._pending:
            names = sorted(set(r.get("in") for _, r in self._pending
                               if isinstance(r.get("in"), str)))
            self._warn(
                "{} 條 route 的通道目前不存在（{}）—— 暫不註冊；"
                "該通道上線時會用你寫的內容自動註冊".format(len(names), "、".join(names)))
        return self._autofill()

    # ─────────────────────────────────────────────────────────────
    # 自動註冊 —— 每次啟動的固定動作（沒有開關）
    # ─────────────────────────────────────────────────────────────
    def _autofill(self):
        """把「現在存在的通道」補進路由表 —— 使用者沒寫的，補預設。

        規則（2026-09 定案）：

          1. **self 與 vbus 幾乎必然存在，不判斷** —— 不管 `_locals` 有沒有東西，
             這兩個來源一律進表（見 `ALWAYS_PRESENT`）。
          2. **self 的預設是 `out: []`** —— `self` **不指向自己**。
             （因為 vBus 注入的幀「已經在本地」了，`out` 再寫 self 等於
              多 dispatch 一次；使用者要本地執行就自己寫 `["self", ...]`。）
          3. **其他通道的預設是 `out: ["self"]`** —— 這正是未導入 Router 前的
             行為（每條線自己收、自己執行），所以打開 `enable:1` 不會讓任何
             一條線失效。
          4. 使用者寫過的那條，一個字都不動（`auto: False`）。

        只對「這一輪新看到的通道」動手（`_auto_done`）:
          - 同一條線不會被重複補，也不會在使用者 ROUTE_DEL 之後又被補回來
          - 但**每次啟動都是全新的一輪**（`_auto_done` 清空）→ 一定會重新檢查建立

        ★ 本機迴路（vBus）不進 ifaces（見 is_local_bus），所以要另外走 `_locals`。
        ★ 這裡只在**記憶體**裡建表，不寫回 config.json —— 避免使用者的設定檔
          被自動塞進他沒寫過的 route（要落盤請用 ROUTER_SAVE / 0x1605）。

        回傳這一輪補出來的來源名稱。
        """
        new = []
        names = set(self.ifaces.keys())
        if self._locals:
            names.add(SELF)
        names |= set(ALWAYS_PRESENT)     # self / vbus 不判斷，一律進表
        for name in sorted(names, key=_route_order):
            if name in self._auto_done:
                continue
            self._auto_done.add(name)
            if name in self.by_in:
                continue                      # 使用者已經寫了 → 尊重他
            # self 不指向自己（預設空）；其他通道預設自己收自己執行
            outs = () if name == SELF else (SELF,)
            self.by_in[name] = {
                "id": len(self.by_in),
                "in": name,
                "out": outs,
                "verdict": self._verdict_of(outs),
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
        self._pending = []
        self._skipped = []
        for idx, r in enumerate(routes):
            if not isinstance(r, dict) or not isinstance(r.get("in"), str):
                # 明顯壞掉的（型別錯）當場報錯 —— 這與「通道不存在」不同，
                # 前者是設定寫錯，後者只是還沒上線。
                self._add_route(idx, r)
                continue
            src = r["in"].strip()
            if src in ALWAYS_PRESENT or src in self.ifaces:
                # 已知存在的通道 → 當場註冊
                if self._add_route(idx, r):
                    n += 1
            else:
                # 還不確定 → 等 sync_ifaces() 看到它上線再結算（見 _reconcile_pending）
                self._pending.append((idx, r))

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
        # 反自我反射：{"in":"now","out":["now"]} 會把幀轉回自己來的線 → 無限迴圈。
        # ★ 例外：來源是 SELF（本機發起的幀）。
        #   {"in":"self","out":["self"]} 不是迴圈，而是「我自己發、我自己執行」
        #   —— 這正是本機迴路（vBus）的預設語意，_autofill() 補的就是這一條。
        if src in outs and src != SELF:
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
        """由 out 清單推導 verdict。

        `self` 是「本地執行」的開關；`vbus` **不是出口**（見 _forward），
        所以它也不算「有轉發目標」—— `["vbus"]` 推導成 V_DROP（不執行、不轉發），
        與使用者的規格一致：「vBus 不存在、無視他，他只負責發送」。
        """
        has_self = SELF in outs
        has_fwd = any(o != SELF and o != "vbus" for o in outs)
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
        names = sorted((n for n in self.by_in.keys() if self._is_live(n)),
                       key=_route_order)
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
        """目前生效的設定（可寫回 config.json 的 Router 區塊）。

        ⚠️ autofill **不會**自動呼叫這個來落盤（2026-09 起）。
        只有明確的 `ROUTER_SAVE`（0x1605）與 `router_actions` 會用它。
        """
        return {
            "enable": 1 if self.enable else 0,
            "routes": [{"in": n, "out": list(self.by_in[n]["out"])}
                       for n in sorted(self.by_in.keys(), key=_route_order)],
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
        """把幀送到 spec['out'] 的每個出口。

        兩個保留字**不會**被寫出（在 verdict 階段就處理掉了）：
          `self` —— 本地執行的開關
          `vbus` —— **只負責發送，不是出口**（見下）

        契約: 逐出口同步寫出；**每個出口各組一份自己的 bytes**
        （不能共用 Proto 的模組級共享 buffer — 一對多時第二個會拿到髒資料）。
        """
        outs = spec["out"]
        frame = None
        for name in outs:
            if name == SELF:
                continue
            if name == "vbus":
                # vBus 只當「來源」，不能當出口 —— 它沒有 io（CircuitBus(None)，
                # write() 永遠回 False），也刻意不進 ifaces（那是出口表）。
                # 使用者若在 out 寫了它 → **無視，不轉送、不警告、不當錯誤**
                # （規格：不存在，無視他，他只負責發送）。
                self._bump(name, "ignored")
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
        for name in sorted(self.by_in.keys(), key=_route_order):
            s = self.by_in[name]
            routes.append({
                "id": s["id"], "in": s["in"], "out": list(s["out"]),
                "auto": s.get("auto", False),
                "live": self._is_live(name),
                "hit": s["hit"], "fwd": s["fwd"], "drop": s["drop"],
            })
        return {
            "enable": self.enable,
            "ifaces": sorted(self.ifaces.keys()),
            "local": SELF in self.by_in and bool(self._locals),
            # 寫了但實體不存在 → 無視它、跳過它的建立（route 仍在 config 裡等上線）
            "unbound": [n for n in self.by_in.keys() if not self._is_live(n)],
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
