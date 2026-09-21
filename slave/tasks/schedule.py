"""
schedule.py — 定時指令排程任務（ScheduleTask）

用途：開機時自行尋找排程檔（預設 /schedule.json，不靠 config 開關），
找到就依時間軸把 NC4 指令「寫進 vBus」→ 走內部 收指令 → 解碼 → 執行 鏈路。
找不到就（第一次）自動生成一個空範本並 idle。

為什麼只寫 vBus：
  - 實體總線（uart0/uart1）的 rx_hub 同時被 CircuitTask.poll()/BusDecodeTask
    不斷寫入與讀走（單寫入者 SPSC），外部再塞幀既非「寫入讀取緩衝」的語義、
    又會與輪詢競爭 → 不可行。
  - vBus = 任務自己建立的內部虛擬總線（io=None，不碰任何腳位），註冊進
    bus_sources，由 BusDecodeTask 當一般來源消費；唯一寫入者就是 schedule，
    乾淨且無競爭。

排程檔格式（首次啟動若不存在，自動產生空範本 /schedule.json）：
  {
    "repeat": 0,
    "schedule": [
      { "addr": "0xFFFF",
        "ms":   1000,
        "bus":  "vBus",
        "cmds": {"cmd": "0x3105", "payload": "00 05 00 00 FF"}
      }
    ]
  }

  repeat : 0 = 播完一次；-1 = 無限循環；N = 循環 N 次（可選，預設 0）
  schedule[] 每一筆：
    ms   : 由任務啟動起算第幾 ms 發送
    addr : 目標位址（0xFFFF = 廣播，可選）
    bus  : 只支援 "vBus"（預設）＝注入自己的解碼鏈。
           ★ 沒有其他值 —— 要送去別的地方**一律經 Router**：
             注入後的來源是 `self`，Router 的
             `{ "in": "self", "out": ["self", "now"] }` 決定它去哪。
           （舊版的 "circuit:<i>" / "net:<i>" 直通出口已移除：那條路
             繞過 Router.gate()，讓路由政策管不到排程送出的東西。）
    cmds : 一個 {cmd, payload}（自動打包 NC4 含 CRC32）
           或 [ {cmd,payload}, ... ] 多筆、或純 hex 字串（raw 完整訊框原樣送出）
  cmd / payload / addr 都支援 0x 前綴與空格分隔 hex。
  payload 欄位格式依 slave/schema/*.json。

發送紀錄：print + 追加到 /schedule_trace.log（USB log 串流不可靠時的可靠證據）。
"""

import json
import time
import struct

try:
    import ubinascii as _binascii
except ImportError:
    import binascii as _binascii

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log
from lib.sys.proto import RX_BUF_SIZE

SCHEDULE_FILE = "/schedule.json"
TRACE_FILE = "/schedule_trace.log"

SOF = b"NC"
VER = 4
ADDR_BROADCAST = 0xFFFF


def build_nc4(cmd, payload=b"", addr=ADDR_BROADCAST):
    """打包 NC4 訊框（SOF 2B + VER 1B + ADDR u16 + CMD u16 + LEN u16 + payload + CRC32 LE）。

    CRC32 範圍 = header[2:] + payload（不含 SOF、不含 CRC 自己），對齊 lib.proto。
    """
    payload = bytes(payload)
    header = struct.pack("<2sBHHH", SOF, VER, int(addr) & 0xFFFF,
                         int(cmd) & 0xFFFF, len(payload))
    crc = _binascii.crc32(header[2:] + payload) & 0xFFFFFFFF
    return header + payload + struct.pack("<I", crc)


def _to_int(value, default=0):
    """接受 int、'18'、'0x12'、'FF' → int。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        s = str(value).strip()
        if s.lower().startswith("0x"):
            return int(s, 16)
        if not s:
            return default
        return int(s, 10) if s.isdigit() else int(s, 16)
    except Exception:
        return default


def _hex_to_bytes(s):
    """hex 字串（可含 0x/空格）→ bytes。空字串 → b''。"""
    s = str(s).replace("0x", "").replace("0X", "")
    t = "".join(s.split())
    if not t:
        return b""
    return bytes(int(t[i:i + 2], 16) for i in range(0, len(t), 2))


class ScheduleTask(Task):
    """依 /schedule.json 定時把 NC4 指令寫進 vBus 的排程任務。"""

    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._schedule = []
        self._idx = 0
        self._cycle = 0
        self._done = False
        self._t0 = None
        self._repeat = 0
        self._vbus = None

    # ── 啟動：自行找檔 ──────────────────────
    def on_start(self):
        super().on_start()
        # ★ 先把 vBus 建起來（不要等第一次真要注入才建）。
        #   為什麼：Router（BusDecodeTask，layer 1）在 on_start 時做第一次
        #   `sync_ifaces()`，那一刻看到的通道就決定了路由表。vBus 若等到
        #   第一次觸發才惰性建立，就趕不上那個時間點 → Router 看不到它。
        #   在 on_start 建立則保證它跟其他通道同時在場（分層的用意）。
        self._get_vbus()
        self._schedule = []
        self._done = False
        self._idx = 0
        self._cycle = 0
        self._t0 = None
        try:
            self._schedule = self._load()
        except Exception as e:
            get_log().error("[Schedule] 載入 {} 失敗: {}".format(SCHEDULE_FILE, e))
            self._schedule = []
        if self._schedule:
            get_log().info("[Schedule] armed: {} item(s), repeat={}, file={}".format(
                len(self._schedule), self._repeat, SCHEDULE_FILE))
        else:
            get_log().info("[Schedule] {} 沒有可執行項目（idle）".format(SCHEDULE_FILE))

    # ── 檔案讀取／首次產生空範本 ──────────────
    def _load(self):
        import os
        try:
            os.stat(SCHEDULE_FILE)
        except OSError:
            self._create_template()
        with open(SCHEDULE_FILE) as f:
            d = json.load(f)
        self._repeat = int(d.get("repeat", 0) or 0)
        items = []
        for i, it in enumerate(d.get("schedule", [])):
            if not isinstance(it, dict):
                continue
            ms = int(it.get("ms", -1))
            if ms < 0:
                continue
            items.append({
                "ms": ms,
                "addr": it.get("addr", "0xFFFF"),
                "cmds": it.get("cmds"),
                "no": i + 1,
            })
        items.sort(key=lambda e: e["ms"])
        return items

    def _create_template(self):
        template = {
            "repeat": 0,
            "_note": "schedule 排程檔（第一次啟動自動產生）。填好後重開機即生效：每筆 = {addr, ms, bus:vBus, cmds}；bus 只支援 vBus；cmds 可為單個 {cmd,payload}、多筆清單或 raw hex 字串。payload 欄位依 slave/schema/*.json，例 0x3105 MODE_SET = type(u8) id(u8) delay(u16LE) brightness(u8)。",
            "schedule": [],
        }
        with open(SCHEDULE_FILE, "w") as f:
            json.dump(template, f)
        print("[Schedule] 首次啟動：已產生空範本 {}".format(SCHEDULE_FILE))
        get_log().info("[Schedule] 已產生空範本 {}（填 schedule[] 後重開機執行）".format(SCHEDULE_FILE))

    # ── 主迴圈：定時發射 ─────────────────────
    def loop(self):
        if not self.running:
            return
        if self._done or not self._schedule:
            return
        if self._t0 is None:
            self._t0 = time.ticks_ms()
            return
        now = time.ticks_diff(time.ticks_ms(), self._t0)
        while self._idx < len(self._schedule):
            it = self._schedule[self._idx]
            if now < it["ms"]:
                break
            self._idx += 1
            self._fire(it)
        if self._idx >= len(self._schedule):
            self._cycle += 1
            if self._repeat == 0 or (self._repeat > 0 and self._cycle >= self._repeat):
                self._done = True
                print("[Schedule] 排程完成（{} cycle(s)，共 {} item(s)）".format(
                    self._cycle, len(self._schedule)))
            else:
                self._idx = 0
                self._t0 = time.ticks_ms()

    # ── vBus ──────────────────────────────
    def _get_vbus(self):
        """建立（一次）內部虛擬總線並註冊進 bus_sources，回傳 CircuitBus。"""
        if self._vbus is None:
            from lib.sys.circuit_bus import CircuitBus
            self._vbus = CircuitBus(None, label="VBUS")
            sources = bus.get_service("bus_sources")
            if sources is None:
                from lib.sys.bus_sources import BusSources
                sources = BusSources()
                bus.register_service("bus_sources", sources)
            sources.add(self._vbus)
        return self._vbus

    def _inject(self, cb, frame):
        """把完整訊框寫進 vBus 的 rx_hub（2-byte len + data，BusDecodeTask 消費）。"""
        hub = getattr(cb, "rx_hub", None)
        if hub is None:
            return False
        n = len(frame)
        if n > RX_BUF_SIZE:
            print("[Schedule] 訊框過長 {}>{}，跳過".format(n, RX_BUF_SIZE))
            return False
        view = hub.get_write_view()
        if view is None:
            return False   # 解碼端消化不及 → 掉（不重送）
        struct.pack_into("<H", view, 0, n)
        view[2:2 + n] = frame
        hub.commit()
        return True

    # ── bus 解析：只有 vBus（注入自己的解碼鏈）────────────────
    #   為什麼移除 circuit:N / net:N 直通出口：
    #     1. 那條路是 `cb.write(frame)` —— 直接寫出去，**不經過 Router.gate()**。
    #        於是「排程送出的東西」不受路由政策管，`{in:"self", out:[]}` 也擋不住。
    #        現在所有出口都收斂到 Router：注入後的來源是 `self`，
    #        由 `{ "in": "self", "out": [...] }` 決定去哪（含轉發到任何介面）。
    #     2. 附帶查到：那條路**從來沒有真的生效過** —— `_load()` 建立 items 時
    #        只放 {ms, addr, cmds, no}，**沒有放 "bus"**，所以
    #        `it.get("bus", "vBus")` 永遠回預設值。文件寫了、程式碼寫了，
    #        但排程檔裡的 "bus" 欄位根本不會被讀到。
    #        → 所以這次移除是純清理，沒有行為變更。
    def _resolve_target(self, key):
        """bus 值 → (物件, 寫法)。只有 vBus → ("rx", 注入 rx_hub 走解碼鏈)。

        舊介面回傳 (cb, "tx")，呼叫端才用 cb.write()；現在不再產生 "tx"，
        但仍保留「第二個回傳值是寫法」的形狀，讓呼叫端不必大改。
        """
        k = str(key).strip().lower()
        if k in ("vbus", "v", "sim", "virtual", ""):
            return self._get_vbus(), "rx"
        get_log().warn(
            "[Schedule] 未知 bus {!r} → 跳過（只支援 vBus；"
            "要送去別的地方請經 Router：{in:self, out:[...]}）".format(key))
        return None, None

    def _fire(self, it):
        """bus 一律 vBus（注入自己的解碼鏈），出口由 Router 的 self route 決定。"""
        cb, mode = self._resolve_target(it.get("bus", "vBus"))
        if cb is None:
            return
        cmds = it["cmds"]
        if not isinstance(cmds, list):
            cmds = [cmds]
        for item in cmds:
            try:
                if isinstance(item, dict):
                    cmd = _to_int(item.get("cmd"), 0)
                    payload = _hex_to_bytes(item.get("payload", ""))
                    addr = _to_int(item.get("addr", it["addr"]), ADDR_BROADCAST)
                    frame = build_nc4(cmd, payload, addr)
                    desc = "cmd=0x{:04X} payload={}B".format(cmd, len(payload))
                else:
                    frame = _hex_to_bytes(item)
                    desc = "raw {}B".format(len(frame))
                if not frame:
                    continue
                # 只可能 "rx"：出口全交給 Router（見 _resolve_target 的說明）
                ok = self._inject(cb, frame)
                if ok:
                    print("[Schedule] item#{} +{}ms {} {} -> {} ({})".format(
                        it["no"], it["ms"], mode, it.get("bus", "vBus"), desc, cb.label))
                    self._trace("item#{} +{}ms {} {} -> {}".format(
                        it["no"], it["ms"], mode, it.get("bus", "vBus"), desc))
                    self.success += 1
                else:
                    print("[Schedule] item#{} +{}ms {} 緩衝滿/送出失敗，掉幀".format(
                        it["no"], it["ms"], it.get("bus", "vBus")))
            except Exception as e:
                print("[Schedule] item#{} 發送失敗: {}".format(it["no"], e))

    def _trace(self, line):
        """發送記錄追加到 trace 檔（USB log 串流不可靠時的可靠證據）。"""
        try:
            with open(TRACE_FILE, "a") as f:
                f.write("[{}] {}\n".format(time.ticks_ms(), line))
        except Exception:
            pass

    def on_stop(self):
        super().on_stop()
