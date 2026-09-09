"""
schedule.py — 定時指令排程任務（ScheduleTask）

用途：開機時自行尋找排程檔（預設 /schedule.json，不靠 config 開關），
找到就依時間軸把 NC4 指令「寫進 vBus」→ 走內部 收指令 → 解碼 → 執行 鏈路。
找不到就（第一次）自動生成一個空範本並 idle。

為什麼只寫 vBus：
  - 實體總線（uart1/uart2）的 rx_hub 同時被 CircuitTask.poll()/BusDecodeTask
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
    bus  : "vBus"（預設）＝注入給自己（走內部解碼鏈）
           "circuit:<i>" ＝ 選 circuit bus 列表第 i 項，用物件.write() 發出去
           "net:<i>"     ＝ 選 net bus 列表第 i 項，用物件.write() 發出去
           （從列表選取，不需要知道它是 uart 還是網路段）
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
VBUS_NAME = "vbus"   # 唯一允許的 bus 值（大小寫不拘）

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

    # ── bus 解析：vBus（自我注入）／circuit:N、net:N（列表選取，不需知實體）──
    def _circuit_list(self):
        """circuit bus 列表：CircuitTask 註冊的 circuit_bus_list（decode 使用同一份）。"""
        lst = bus.get_service("circuit_bus_list")
        if lst is None:
            lst = bus.get_service("circuit_bus_all_list") or []
        return lst

    def _net_list(self):
        """net bus 列表：依服務註冊順序收集（net_bus_ctrl / net_bus_discovery…）。"""
        out = []
        for svc in ("net_bus_ctrl", "net_bus_discovery"):
            c = bus.get_service(svc)
            if c is not None:
                out.append(c)
        return out

    def _resolve_target(self, key):
        """bus 值 → (物件, 寫法)。"rx" = 注入 rx_hub（自我解碼），"tx" = 物件.write() 發出去。"""
        k = str(key).strip().lower()
        if k in ("vbus", "v", "sim", "virtual", ""):
            return self._get_vbus(), "rx"
        for prefix, lst in (("circuit", self._circuit_list()),
                            ("net", self._net_list())):
            if k.startswith(prefix):
                idx = _to_int(k[len(prefix):].lstrip(":_- "), -1)
                if 0 <= idx < len(lst):
                    return lst[idx], "tx"
                get_log().warn("[Schedule] bus {!r} index 超出 {} 列表（{} 項）→ 跳過".format(
                    key, prefix, len(lst)))
                return None, None
        get_log().warn("[Schedule] 未知 bus {!r}（支援 vBus / circuit:0.. / net:0..）→ 跳過".format(key))
        return None, None

    def _fire(self, it):
        """bus 預設 vBus（自我注入）；circuit:N / net:N = 由列表選取後用物件.write() 發出。"""
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
                if mode == "rx":
                    ok = self._inject(cb, frame)
                else:
                    if not hasattr(cb, "write"):
                        print("[Schedule] item#{} bus {} 沒有 write() → 跳過".format(
                            it["no"], it.get("bus")))
                        continue
                    ok = cb.write(frame)
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
