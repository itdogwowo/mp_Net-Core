"""
控制面板 Task — 面板裝置（編碼器 + 按鈕 + LVGL → ESP-NOW 訊號)

兩模式分層（由 bus.shared["_ui_active"] 切換）:
  LV 模式  (LVGL 在跑):實體按鈕/encoder 歸 LVGL 消費,本 task 不發 vbtn;
           改負責把 LVGL 頁面直寫的 bus._display_cmd 轉成 0x1501 WTT_CTL 廣播。
  按鈕模式 (LVGL 沒跑):維持原行為,每組按鈕事件發送兩次 ESP-NOW(0x1401):
           1. 真實按鈕: type=HW.PIN(0), id=0, label="btn"|"encC",  value=state
           2. 虛擬按鈕: type=HW.VBTN(8), id=vbtn_id, label="vbtn", value=state
           同時寫入本地 HW.VBTN 緩衝。
  接收:on_status(0x1502) 由 waiting_to_trash_actions 寫 _display_* Global,
        LVGL 頁面讀同一欄位顯示(已確認/倒數)。
"""

import time, struct
from machine import Encoder
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.hw_manager import HW, get_pin_configured
from lib.sys.proto import Proto
from lib.sys.log_service import get_log

CMD_HW = 0x1401
CMD_WTT_CTL = 0x1501
_NO_CHANGE = 0xFF   # WTT_CTL u8 約定: 255 = 不改該欄位
_EX_IC_SLOT_KEY = "_ex_ic_slot"
_EX_IC_PENDING_KEY = "_ex_ic_pending"
_UART_SOF = 0xB4
_UART_EOF = 0xFF
_ENC_DELTA_KEY = "_enc_delta"
_ENC_EVENT_TYPE = 0xFE
_ENC_EVENT_LABEL = b"enc_delta"

# 需要同步的實體按鈕: [(label, vbtn_id), ...]
_VBTN_SYNC = [
    ("btn",  0),
    ("encC", 1),
]


def _find_pin_obj(label, fallback=0):
    """從統一資源取得 Pin（pin_by_label / pin_list / _PIN_CACHE）。

    一律不自行 new Pin()：找不到已配置的腳位就回 None，由呼叫端安全跳過，
    避免自行初始化踩到其他外設（如 WiFi SDMMC GPIO 39-48）佔用的腳位。
    """
    pin = get_pin_configured(label)
    if pin is not None:
        return pin
    return None


def _label_gpio(label):
    cfg = bus.shared.get("PIN") or {}
    lst = cfg.get("list") or []
    for item in lst:
        if isinstance(item, dict) and item.get("label") == label:
            return item.get("GPIO", "?")
    return "?"


def _format_mode_bits(mode):
    return "{:08b}".format(mode & 0xFF)


class ControlPanelTask(Task):
    log_schema = ["enc_pos"]

    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._now_bus = None
        self._btns = []
        self._enc = None
        self._enc_last = 0

    def on_start(self):
        super().on_start()

        # 從統一資源取得腳位; 未配置 (None) 就安全跳過, 不自行初始化
        btn1 = _find_pin_obj("btn", 40)
        btn2 = _find_pin_obj("encC", 17)
        if btn1 is not None:
            self._btns.append([btn1, btn1.value(), "btn", 0])
        if btn2 is not None:
            self._btns.append([btn2, btn2.value(), "encC", 0])

        pin_a = _find_pin_obj("encA", 18)
        pin_b = _find_pin_obj("encB", 8)
        if pin_a is not None and pin_b is not None:
            self._enc = Encoder(0, pin_a, pin_b)
            self._enc_last = self._enc.value()

        self._now_bus = bus.get_service("NowBus")
        for _, vbtn_id in _VBTN_SYNC:
            HW.set(HW.VBTN, vbtn_id, 1)
        # LVGL 在跑時,實體按鈕歸 LVGL 消費,不廣播初始 vbtn 狀態(避免重複)
        if not bus.shared.get("_ui_active", False):
            for pin, stable, label, _ in self._btns:
                for sync_label, vbtn_id in _VBTN_SYNC:
                    if label == sync_label:
                        HW.set(HW.VBTN, vbtn_id, stable)
                        self._send_vbtn(vbtn_id, stable)
                        break
        get_log().info("[CP] encA={} encB={} btn={} encC={}".format(
            _label_gpio("encA"), _label_gpio("encB"),
            _label_gpio("btn"), _label_gpio("encC")))

    def _read_buttons(self, now):
        triggered = []
        for entry in self._btns:
            pin, stable, label, ts = entry
            raw = pin.value()
            if raw != stable:
                if time.ticks_diff(now, ts) >= 30:
                    entry[1] = raw
                    entry[3] = now
                    triggered.append((label, raw))
            else:
                entry[3] = now
        return triggered

    def _send(self, label, state):
        """ESP-NOW 發送真實按鈕 (type=PIN, id=0, label="btn"|"encC")"""
        if self._now_bus is None:
            return
        lb = label.encode()
        payload = struct.pack("<BB", HW.PIN, 0)
        payload += struct.pack("<H", len(lb)) + lb
        payload += struct.pack("<H", state)
        self._now_bus.broadcast(Proto.pack(CMD_HW, payload))

    def _send_vbtn(self, vbtn_id, state):
        """ESP-NOW 發送虛擬按鈕 (type=VBTN, id=vbtn_id, label="vbtn")"""
        if self._now_bus is None:
            return
        label = b"vbtn"
        payload = struct.pack("<BB", HW.VBTN, vbtn_id)
        payload += struct.pack("<H", len(label)) + label
        payload += struct.pack("<H", state)
        self._now_bus.broadcast(Proto.pack(CMD_HW, payload))

    def _send_encoder_delta(self, delta):
        """ESP-NOW 發送編碼器增量事件，value 以 u16 裝載 (+1 / 0xFFFF[-1])"""
        if self._now_bus is None:
            return
        encoded = delta & 0xFFFF
        payload = struct.pack("<BB", _ENC_EVENT_TYPE, 0)
        payload += struct.pack("<H", len(_ENC_EVENT_LABEL)) + _ENC_EVENT_LABEL
        payload += struct.pack("<H", encoded)
        self._now_bus.broadcast(Proto.pack(CMD_HW, payload))

    def _poll_ex_ic(self):
        if not bus.shared.get(_EX_IC_PENDING_KEY):
            return

        event = bus.shared.get(_EX_IC_SLOT_KEY) or {}
        chip_type = int(event.get("chip_type", -1) or -1)
        chip_id = int(event.get("chip_id", -1) or -1)
        data = event.get("data", b"") or b""
        if isinstance(data, memoryview):
            data = bytes(data)
        elif not isinstance(data, (bytes, bytearray)):
            data = bytes(data)

        bus.shared[_EX_IC_PENDING_KEY] = 0

        if len(data) != 5 or data[0] != _UART_SOF or data[4] != _UART_EOF:
            get_log().info("[CP][RX][0x1403][DROP] chip={} id={} len={}".format(
                chip_type, chip_id, len(data)))
            return

        mode = data[1]
        brightness = data[2]
        time_remaining = data[3]
        bus.shared["_display_mode"] = mode
        bus.shared["_display_brightness"] = brightness
        bus.shared["_display_time"] = time_remaining
        # seq 遞增:同值重送(重新計時)也讓 LVGL 頁面重置本地倒數
        bus.shared["_display_time_seq"] = (bus.shared.get("_display_time_seq", 0) + 1) & 0xFF
        get_log().info("[CP][RX][0x1403] chip={} id={} mod={} bit={} bri={} time={}".format(
            chip_type,
            chip_id,
            mode & 0x3F,
            _format_mode_bits(mode),
            brightness,
            time_remaining))

    def _forward_display_cmd(self):
        """消費 bus._display_cmd → 廣播 0x1501 WTT_CTL 給執行裝置(mode/bri,255=不改)。
        LVGL 頁面同板直寫的指令由本 task 轉成 ESP-NOW 送出。
        不做同值去重:本板 _display_cmd 只有使用者操作才會寫入(讀完即清),
        每次操作都是新的 request;執行裝置端已保證每個 request 都會回 0x1502。
        若同值去重,「重送已確認的模式」時指令被丟棄 → 接收器不會回 →
        頁面永遠卡在琥珀等不到確認。"""
        cmd = bus.shared.get("_display_cmd")
        if not cmd:
            return
        bus.shared["_display_cmd"] = None
        try:
            mode = cmd.get("mode")
            brightness = cmd.get("brightness")
            m = _NO_CHANGE if mode is None else int(mode) & 0xFF
            b = _NO_CHANGE if brightness is None else max(0, min(36, int(brightness)))
            if self._now_bus is not None:
                self._now_bus.broadcast(Proto.pack(CMD_WTT_CTL, bytes([m, b])))
            get_log().immediate("[CP][TX][0x1501] mode={:02X} bri={}".format(m, b))
        except Exception as e:
            get_log().error("[CP][WTT] fwd err: {}".format(e))

    def loop(self):
        if not self.running:
            return

        now = time.ticks_ms()
        self._poll_ex_ic()
        self._forward_display_cmd()

        # 兩模式分層:LVGL 在跑 → 實體按鈕/encoder 歸 LVGL 消費(hw_manager 快照),
        # 不再廣播 vbtn/enc_delta(避免同一次按壓被兩邊各處理一次)。
        if bus.shared.get("_ui_active", False):
            return

        if self._enc is None:
            # 無 encoder 硬體（測試/無 encoder 面板）→ 只處理按鈕，不讀 encoder
            pass
        else:
            pos = self._enc.value()
            if pos != self._enc_last:
                step = 1 if pos > self._enc_last else -1
                self._enc_last = pos
                self._lw_ex(0, pos)
                cur = int(bus.shared.get(_ENC_DELTA_KEY, 0) or 0)
                bus.shared[_ENC_DELTA_KEY] = cur + step
                self._send_encoder_delta(step)
                get_log().immediate("[CP] enc_delta={:+d} pos={}".format(step, pos))
                self.success += 1

        for label, raw in self._read_buttons(now):
            # ── 第 1 次 ESP-NOW: 真實按鈕 ──
            self._send(label, raw)

            # ── 第 2 次 ESP-NOW + 本地緩衝: 虛擬按鈕 ──
            for sync_label, vbtn_id in _VBTN_SYNC:
                if label == sync_label:
                    self._send_vbtn(vbtn_id, raw)
                    HW.set(HW.VBTN, vbtn_id, raw)
                    # 同核旗標，供 action_task_1 即時讀取
                    if vbtn_id == 1:
                        bus.shared["_vbtn1_event"] = raw
                    break

            get_log().immediate("[CP] {}={}".format(label, raw))
            self.success += 1
