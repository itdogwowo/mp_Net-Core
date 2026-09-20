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
#       { "in": "lan",   "out": ["uart2"] }
#     ]
#   }
#
# 規則:
#   enable=0              → 完全不作用（與未導入前 100% 相同）
#   in 沒配對             → 不執行、不轉發（沒配對＝沒路走）
#   out=["uart1"]         → 只轉發，本地不執行
#   out=["self"]          → 只本地執行，不轉發
#   out=["self","lan"]    → 本地執行 ＋ 同時轉發
#   out 含 in             → 開機拒絕該 route（自我反射，永遠是錯的）
#
# 生命週期契約:
#   pack() 風格 — 本模組的 _send() **同步**寫出，不持有 memoryview 跨呼叫。
# ═══════════════════════════════════════════════════════════════════════════

import struct

# 協議常數 —— 一律沿用 proto.py，不在此重打
from lib.sys.proto import SOF, CUR_VER, HDR_LEN, CRC_LEN, RX_BUF_SIZE, ADDR_BROADCAST

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

        if not cfg:
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

        if self.enable and n == 0:
            self._warn("enable=1 但沒有任何有效 route → 所有幀都不執行（等同關閉）")

        return n

    def _add_route(self, idx, r):
        """驗證並加入一條 route。回傳 True/False。"""
        if not isinstance(r, dict):
            self._bad("routes[{}] 不是物件".format(idx))
            return False

        # ── in: 純量字串 ──────────────────────────────────
        src = r.get("in")
        if isinstance(src, (list, tuple)):
            self._bad("routes[{}]: 'in' 必須是單一介面名稱，不可為列表 "
                      "（一個來源 = 一條 route；要多個請寫多條）".format(idx))
            return False
        if not isinstance(src, str) or not src.strip():
            self._bad("routes[{}]: 'in' 必須是非空字串".format(idx))
            return False
        src = src.strip()

        # ── out: 一律列表（字串容忍但提示）────────────────
        dst = r.get("out")
        if dst is None:
            self._bad("routes[{}]: 缺少 'out'".format(idx))
            return False
        if isinstance(dst, str):
            self._warn("routes[{}]: 'out' 建議一律寫列表，例如 [\"{}\"]".format(idx, dst))
            dst = [dst]
        if not isinstance(dst, (list, tuple)):
            self._bad("routes[{}]: 'out' 必須是列表".format(idx))
            return False

        outs = []
        for o in dst:
            if not isinstance(o, str) or not o.strip():
                self._bad("routes[{}]: 'out' 內含非字串項 → 整條跳過".format(idx))
                return False
            outs.append(o.strip())

        if not outs:
            self._warn("routes[{}]: 'out' 是空的 → 這條 route 沒有作用".format(idx))

        # ── 結構檢查（永遠是錯的設定，開機就拒絕）──────────
        if src in outs:
            self._bad("routes[{}]: 'out' 不可包含 'in'（{}）— 自我反射，永遠是錯的"
                      .format(idx, src))
            return False

        # ── 已否決的欄位提示 ──────────────────────────────
        for k in r.keys():
            if k not in ("in", "out") and _is_bad_key(k):
                self._warn("routes[{}]: 欄位 '{}' 已移除 → 已忽略".format(idx, k))

        # ── 順序（後者覆寫前者）────────────────────────────
        if src in self.by_in:
            self._warn("來源 '{}' 重複定義 → 以最後一條為準".format(src))

        self.by_in[src] = {
            "id": idx,
            "in": src,
            "out": tuple(outs),
            "verdict": self._verdict_of(outs),
            # 統計
            "hit": 0,
            "fwd": 0,
            "drop": 0,
            "nomatch": 0,
        }

        # ── 已知的成環風險提示（不禁止，見 doc §7.2）──────
        if "udp" in outs:
            self._warn("routes[{}]: 'out' 含 'udp' — udp 對面是會自動回話的 discovery "
                       "服務，若另有 {{'in':'udp'}} 的反向規則就會成環".format(idx))

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

        payload 為 None 時只重組 header+CRC（無負載幀）。
        """
        p = payload
        if p is None:
            p = b""
        elif not isinstance(p, (bytes, bytearray, memoryview)):
            p = bytes(p)
        n = len(p)
        total = HDR_LEN + n + CRC_LEN
        if total > RX_BUF_SIZE:
            self._warn_once("_oversize", "訊框 {}B 超過 RX_BUF_SIZE({}) → 不轉送"
                            .format(total, RX_BUF_SIZE))
            return None

        if self._tx_buf is None or len(self._tx_buf) < total:
            self._tx_buf = bytearray(RX_BUF_SIZE)
            self._tx_mv = memoryview(self._tx_buf)

        b = self._tx_mv
        struct.pack_into("<2sBHHH", b, 0, SOF, CUR_VER, addr, cmd, n)
        if n:
            b[HDR_LEN:HDR_LEN + n] = p
        crc = self._crc32(b[2:HDR_LEN + n]) & 0xFFFFFFFF
        struct.pack_into("<I", b, HDR_LEN + n, crc)
        return b[:total]

    @staticmethod
    def _crc32(data):
        try:
            import binascii
            return binascii.crc32(data)
        except Exception:                       # pragma: no cover
            return 0

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
                "hit": s["hit"], "fwd": s["fwd"], "drop": s["drop"],
            })
        return {
            "enable": self.enable,
            "ifaces": sorted(self.ifaces.keys()),
            "unbound": [n for n in self.by_in.keys() if n not in self.ifaces],
            "routes": routes,
            "seen": dict(self.seen),
            "stats": dict(self.stats),
            "errors": list(self._errors),
        }

    def _bump(self, name, event):
        k = name + ":" + event
        self.stats[k] = self.stats.get(k, 0) + 1

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
