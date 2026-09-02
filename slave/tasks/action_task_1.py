"""
action_task_1.py — 綜合動作控制任務 + UART Display 協定

三階段馬達控制:
  RISE (升高) → 正轉
  WAIT (等待) → 停止
  FALL (下降) → 反轉

觸發: 直接輪詢虛擬按鈕 VBTN[0]/VBTN[1]
  VBTN[0] 短按:
    空閒(IDLE)       → 啟動 RISE 階段
    升高/等待中      → 提早跳到 FALL 階段
  VBTN[0] 長按:
    反轉 Bit6 保留旗標
  VBTN[1] 短按:
    mode + 1 (限制於低 6 bit: 0-63, 保留 Bit6/Bit7)
  VBTN[1] 長按:
    反轉 Bit7 特殊模式旗標

UART Display 協定 (與 DisplayController 相容):
  幀格式: [0xB4] [mode(8-bit)] [brightness(0-31)] [time] [0xFF]
  mode byte: Bit7=特殊模式, Bit6=保留, Bit5-0=模式值(0-63)
  - 本地 mode/brightness 改變 → 發送 UART
  - 收到 UART fram → 更新狀態, 不回傳

可設定參數 (bus.shared):
  _motor_rise_ms            (預設 5000)
  _motor_wait_ms            (預設 500)
  _motor_fall_ms            (預設 5000)
  _motor_startup_delay_ms   (預設 10000)
"""

import time
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.hw_manager import HW, _PIN_CACHE, get_pin_configured
from lib.sys.log_service import get_log
from lib.hw.mp3_tf_16p import MP3TF16P
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec

# ═══ UART 協定常數 ═══

_UART_SOF = 0xB4
_UART_EOF = 0xFF
_UART_BRIGHTNESS_MAX = 31  # APA102 5-bit

# mode byte 位元結構 (8-bit, 0-255 完整傳遞):
#   Bit 7 (0x80): 特殊模式旗標 (1=特殊模式)
#   Bit 6 (0x40): 保留, 暫不使用
#   Bit 5-0:     實際模式值 (0-63)
MODE_SPECIAL  = 0x80  # Bit 7
MODE_RESERVED = 0x40  # Bit 6
MODE_VALUE    = 0x3F  # Bits 5-0

# WTT 狀態廣播(取代舊 0x1403):執行裝置 → 面板裝置,on_status 寫 _display_* Global
CMD_WTT_STATUS = 0x1502
_ENC_DELTA_KEY = "_enc_delta"

# ═══ 腳位解析 ═══

def _resolve_pin(gpio_or_label):
    """從統一資源取得 Pin（pin_by_label / pin_list / _PIN_CACHE）。

    一律不自行 new Pin()：找不到已配置的腳位就回 None，由呼叫端安全跳過，
    避免自行初始化踩到其他外設（如 WiFi SDMMC GPIO 39-48）佔用的腳位。
    """
    if isinstance(gpio_or_label, str):
        return get_pin_configured(gpio_or_label)
    gpio = int(gpio_or_label)
    if gpio in _PIN_CACHE:
        return _PIN_CACHE[gpio]
    return None


def _find_pin_cfg(labels):
    if isinstance(labels, str):
        labels = (labels,)
    cfg = bus.shared.get("PIN") or {}
    lst = cfg.get("list") or []
    for label in labels:
        for item in lst:
            if isinstance(item, dict) and item.get("label") == label:
                return item
    return None


# ═══ 常數 ═══

STATE_IDLE      = 0
STATE_RISE      = 1
STATE_WAIT      = 2
STATE_FALL      = 3
STATE_PRE_DELAY = 4   # 進入模式後, 啟動電機前的延遲

_DEFAULT_RISE_MS = 7300
_DEFAULT_WAIT_MS = 90000
_DEFAULT_FALL_MS = 8000
_BOOT_FALL_MS = 20000


def _read_cfg(key, default):
    v = bus.shared.get(key)
    return int(v) if v is not None else default


# 馬達腳位預設 GPIO（label 找不到時的回落）
_MOTOR_DEFAULT_PINS = {
    "m1":   8,
    "m2":   9,
    "m_en": 10,
}


def _resolve_pin_or(labels, fallback_gpio):
    """按 label/alias 解析 pin，找不到則用 fallback GPIO"""
    if isinstance(labels, str):
        labels = (labels,)
    for label in labels:
        p = _resolve_pin(label)
        if p is not None:
            return p
    print("[PIN] labels={} all missed → fallback gpio={}".format(labels, fallback_gpio))
    return _resolve_pin(fallback_gpio)


# ═══ 模式設定 (硬編碼) ═══
_MAX_MODE = MODE_VALUE  # mode 僅使用低 6 bit (0-63)
_LONG_PRESS_MS = 3000

# 電機觸發列表: (mod, entry_delay_ms, wait_ms)
# 只放需要觸發電機的模式, 不在列表 = 不觸發
_MOTOR_MODE_LIST = [
    (1, 0,  17700),    # mode 1: delay 500ms → RISE → wait 500ms → FALL
]

_STATE_NAME = {
    STATE_IDLE: "閒置",
    STATE_RISE: "上升",
    STATE_WAIT: "等待",
    STATE_FALL: "下降",
    STATE_PRE_DELAY: "延遲等待",
}


# ═══ ActionTask1 ═══

class ActionTask1(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._m1 = None
        self._m2 = None
        self._m_en = None
        self._state = STATE_IDLE
        self._deadline = 0
        self._rise_ms = _DEFAULT_RISE_MS
        self._wait_ms = _DEFAULT_WAIT_MS
        self._fall_ms = _DEFAULT_FALL_MS

        # UART Display 狀態
        self._uart = None
        self._display_mode = 0
        self._temp_mode = 0
        self._display_brightness = 0
        self._temp_brightness = 0
        self._display_time = 0
        self._mode_list = _MOTOR_MODE_LIST[:]  # 模式列表拷貝
        self._max_mode = 0                    # on_start 時設定
        self._last_vbtn = [1, 1]              # 初始假設放開（pull-up）
        self._vbtn_press_time = [0, 0]
        self._vbtn_long_triggered = [False, False]
        self._now_bus = None
        self._mp3 = None
        self._mp3_state = 0        # 0=未初始化, 1=等待中, 2=完成
        self._mp3_deadline = 0
        self._uart_rx_buf = bytearray()  # UART 接收累積 buffer
        self._circuit_bus = None         # 顯示 UART 對應的 CircuitBus(新架構:經 circuit 緩衝收發)
        self._uart_read_buf = bytearray(256)  # 從 CircuitBus 緩存讀取的持久緩衝,重複使用
        self._ack_pending = False        # 已送出 UART 請求、等 echo 確認(同值 echo 也要回面板)
        self._bright_ready = False       # 是否已收過面板第一筆指令:第一筆的 brightness 忽略
        self._motor_control_source = None  # None | "manual" | "reserved"
        self._motor_start_ms = 0         # 記錄馬達啟動時間，供 STOP log 計算持續時間
        self._position = None            # 當前實體位置: None | "top" | "bottom"
        self._startup_delay_ms = 10000   # 啟動延遲 (預設 10s)
        self._startup_phase = 0          # 0=normal, 1=startup_pre_delay, 2=startup_fall
        self._wtt_status_timer = 0       # 週期狀態計時(ticks_ms)

    def _is_motor_enabled(self):
        """讀取 bus.shared["_motor_enabled"]，0=禁用, 1=啟用 (預設 1)"""
        return bool(bus.shared.get("_motor_enabled", 1))

    def _motor_state_name(self):
        return _STATE_NAME.get(self._state, str(self._state))

    def _log_runtime_state(self, reason, level="immediate"):
        msg = "[State] {} mod={} bit={} bri={} time={} motor={}".format(
            reason,
            self._display_mode & MODE_VALUE,
            self._format_mode_bits(self._display_mode),
            self._display_brightness,
            self._display_time,
            self._motor_state_name(),
        )
        log = get_log()
        if level == "info":
            log.info(msg)
        else:
            log.immediate(msg)

    def _dispatch_stream_play_from_start(self, reason):
        # 透過本地 UART 發送 STREAM_PLAY (幀 0)，與 UART Display 幀共存
        if self._uart is None:
            get_log().warn("[STREAM][TX] UART not available — skip")
            return

        app = self.ctx.get("app") if isinstance(self.ctx, dict) else None
        if app is None:
            return

        cmd_play = app.store.get(0x300A)
        if not cmd_play:
            get_log().warn("[STREAM][TX] schema 0x300A missing")
            return
        try:
            payload = SchemaCodec.encode(cmd_play, {"start_frame": 0})
            self._uart_send(Proto.pack(0x300A, payload))
            get_log().immediate("[STREAM][TX] 0x300A start=0 reason={}".format(reason))
        except Exception as e:
            get_log().error("[STREAM][TX] 0x300A failed: {}".format(e))

    def _is_reserved_motor_control(self):
        return self._motor_control_source == "reserved"

    def on_start(self):
        print("[MOTOR] on_start enter")   # 最原始 print，不經過 log 系統
        super().on_start()

        try:
            self._rise_ms = _read_cfg("_motor_rise_ms", _DEFAULT_RISE_MS)
            self._wait_ms = _read_cfg("_motor_wait_ms", _DEFAULT_WAIT_MS)
            self._fall_ms = _read_cfg("_motor_fall_ms", _DEFAULT_FALL_MS)
            self._startup_delay_ms = _read_cfg("_motor_startup_delay_ms", 10000)

            self._max_mode = _MAX_MODE
            self._now_bus = bus.get_service("NowBus")
            vbtn0 = HW.get(HW.VBTN, 0)
            vbtn1 = HW.get(HW.VBTN, 1)
            self._last_vbtn[0] = 1 if vbtn0 is None else int(vbtn0)
            self._last_vbtn[1] = 1 if vbtn1 is None else int(vbtn1)
            self._vbtn_press_time = [0, 0]
            self._vbtn_long_triggered = [False, False]
            bus.shared[_ENC_DELTA_KEY] = 0
            bus.shared["_temp_brightness"] = self._temp_brightness
            bus.shared.setdefault("_motor_enabled", 1)
            bus.shared.setdefault("_display_cmd", None)

            self._m1   = _resolve_pin_or(("m1",), _MOTOR_DEFAULT_PINS["m1"])
            self._m2   = _resolve_pin_or(("m2",), _MOTOR_DEFAULT_PINS["m2"])
            self._m_en = None               # en 由硬體 pull-up，不控制
            self._startup_phase = 1
            self._enter(STATE_PRE_DELAY, self._startup_delay_ms)
            get_log().immediate("[Motor] 啟動 — 延遲等待 {}ms 後下降".format(self._startup_delay_ms))
            m1_cfg = _find_pin_cfg(("m1",))
            m2_cfg = _find_pin_cfg(("m2",))
            en_cfg = _find_pin_cfg(("m_en", "en"))
            get_log().info(
                "[Motor] pin-map m1={} m2={} en={}".format(
                    m1_cfg.get("GPIO") if m1_cfg else _MOTOR_DEFAULT_PINS["m1"],
                    m2_cfg.get("GPIO") if m2_cfg else _MOTOR_DEFAULT_PINS["m2"],
                    en_cfg.get("GPIO") if en_cfg else _MOTOR_DEFAULT_PINS["m_en"]))

            get_log().info(
                "[Motor] rise={} wait={} fall={}ms".format(
                    self._rise_ms, self._wait_ms, self._fall_ms))

            # 初始化 UART (從 bus.shared["UART"] 讀設定)
            self._init_uart()
            # 初始化 MP3-TF-16P (UART list[1], baud 9600)
            self._init_mp3()
        except Exception as e:
            print("[MOTOR] on_start ERROR: {}".format(e))

    # ═══ 階段切換 ═══

    def _enter(self, state, delay_ms=None):
        self._state = state
        now = time.ticks_ms()
        state_name = self._motor_state_name()

        if state == STATE_IDLE:
            self._motor_stop()
            self._deadline = 0
            self._motor_control_source = None
            get_log().immediate("[Motor] → 閒置")
        elif state == STATE_PRE_DELAY:
            self._motor_stop()
            self._deadline = time.ticks_add(now, delay_ms or 0)
            get_log().immediate("[Motor] → 延遲等待 {}ms".format(delay_ms or 0))
        elif state == STATE_RISE:
            if self._is_motor_enabled():
                self._motor_fwd()
            else:
                get_log().immediate("[Motor] 已禁用 — 跳過正轉")
            self._deadline = time.ticks_add(now, self._rise_ms)
            self._dispatch_stream_play_from_start("motor-rise")
            get_log().immediate("[Motor] → 上升 ({}ms)".format(self._rise_ms))
        elif state == STATE_WAIT:
            self._motor_stop()
            self._deadline = time.ticks_add(now, self._wait_ms)
            get_log().immediate("[Motor] → 等待 ({}ms)".format(self._wait_ms))
        elif state == STATE_FALL:
            if self._is_motor_enabled():
                self._motor_rev()
            else:
                get_log().immediate("[Motor] 已禁用 — 跳過反轉")
            self._deadline = time.ticks_add(now, self._fall_ms)
            get_log().immediate("[Motor] → 下降 ({}ms)".format(self._fall_ms))
        self._log_runtime_state("motor->{}".format(state_name))

    # ═══ 馬達控制 ═══

    def _motor_stop(self):
        elapsed = 0
        if self._motor_start_ms > 0:
            elapsed = time.ticks_diff(time.ticks_ms(), self._motor_start_ms)
            self._motor_start_ms = 0
        if self._m1: self._m1.value(0)
        if self._m2: self._m2.value(0)
        # en 不主動控制，由硬體 pull-up 決定
        get_log().immediate("[Motor] 停止  dur={}ms m1={} m2={} state→{}".format(
            elapsed, self._m1, self._m2, self._motor_state_name()))

    def _motor_fwd(self):
        if not self._is_motor_enabled():
            return
        self._motor_start_ms = time.ticks_ms()
        if self._m1: self._m1.value(0)
        if self._m2: self._m2.value(1)
        get_log().immediate("[Motor] 正轉  開始 @{}ms m1={} m2={}".format(
            self._motor_start_ms, self._m1, self._m2))

    def _motor_rev(self):
        if not self._is_motor_enabled():
            return
        self._motor_start_ms = time.ticks_ms()
        if self._m1: self._m1.value(1)
        if self._m2: self._m2.value(0)
        get_log().immediate("[Motor] 反轉  開始 @{}ms m1={} m2={}".format(
            self._motor_start_ms, self._m1, self._m2))

    # ═══ UART Display 協定 ═══

    def _init_uart(self):
        """從 boot/driver 註冊的 uart_list[0] 綁定顯示 UART"""
        uart_list = bus.get_service("uart_list")
        if not uart_list:
            get_log().warn("[UART] uart_list missing")
            return
        if len(uart_list) < 1:
            get_log().warn("[UART] uart_list[0] missing")
            return
        try:
            self._uart = uart_list[0]
            get_log().info("[UART] bind uart_list[0] for display")
        except Exception as e:
            get_log().error("[UART] bind failed: {}".format(e))
        self._try_bind_circuit_bus()

    def _try_bind_circuit_bus(self):
        """尋找顯示 UART 對應的 CircuitBus(circuit 任務已註冊時):
        以 cb.io is self._uart 匹配同一線路物件,綁定後 UART 收發統一走該
        CircuitBus(rx 讀緩存 / tx 走 write),不再直接碰 UART FIFO。
        找不到回 False,由 loop 內 lazily 重試(circuit 啟動比本任務晚時也會綁上)。"""
        if self._circuit_bus is not None or self._uart is None:
            return self._circuit_bus is not None
        try:
            all_list = bus.get_service("circuit_bus_all_list")
            if all_list:
                for cb in all_list:
                    if getattr(cb, "io", None) is self._uart:
                        self._circuit_bus = cb
                        get_log().info("[UART] bind CircuitBus '{}' for display".format(cb.label))
                        return True
        except Exception as e:
            get_log().error("[UART] bind CircuitBus failed: {}".format(e))
        return False

    def _uart_send(self, data):
        """統一發送:已綁定 CircuitBus → 走 bus.write(完整雙向通道);否則直接 uart.write。"""
        if self._circuit_bus is not None:
            try:
                return self._circuit_bus.write(data)
            except Exception as e:
                get_log().error("[UART] circuit bus send error: {}".format(e))
        if self._uart is not None:
            try:
                return self._uart.write(data)
            except Exception:
                return False
        return False

    def _build_uart_state_frame(self, mode=None, brightness=None, time_remaining=None):
        """建立 5-byte 幀: [0xB4, mode, brightness(0-31), time, 0xFF]"""
        if mode is None:
            mode = self._display_mode
        if brightness is None:
            brightness = self._display_brightness
        if time_remaining is None:
            time_remaining = self._display_time
        brightness = max(0, min(brightness, _UART_BRIGHTNESS_MAX))
        return bytes([
            _UART_SOF,
            mode & 0xFF,
            brightness,
            time_remaining & 0xFF,
            _UART_EOF,
        ])

    def _send_uart_state(self, mode=None, brightness=None, time_remaining=None):
        """發送 5-byte 幀: [0xB4, mode, brightness(0-31), time, 0xFF]"""
        if self._uart is None:
            return
        try:
            if mode is None:
                mode = self._display_mode
            if brightness is None:
                brightness = self._display_brightness
            if time_remaining is None:
                time_remaining = self._display_time
            data = self._build_uart_state_frame(mode=mode, brightness=brightness, time_remaining=time_remaining)
            self._uart_send(data)
            get_log().immediate("[UART][TX] frame={} mod={} bit={} bri={} time={} motor={}".format(
                self._format_frame_hex(data),
                mode & MODE_VALUE,
                self._format_mode_bits(mode),
                brightness,
                time_remaining,
                self._motor_state_name()))
        except Exception as e:
            get_log().error("[UART] send error: {}".format(e))

    def _handle_uart_receive(self):
        """輪詢 UART 接收，解析 5-byte 幀（累積 buffer，支援碎片）。
        已綁定 CircuitBus → 從其緩存(cache_hub)讀取(circuit 任務每輪倒資料進緩衝,
        本任務讀自家緩存,不碰 UART FIFO、不影響其他任務);未綁定 → fallback 直接讀 UART。"""
        if self._uart is None:
            return
        try:
            got = False
            if self._try_bind_circuit_bus():
                # 不依賴 any()，每圈讀完緩存全部 ready slot，避免漏收
                while True:
                    n = self._circuit_bus.read_into(self._uart_read_buf)
                    if n <= 0:
                        break
                    self._uart_rx_buf.extend(memoryview(self._uart_read_buf)[:n])
                    got = True
                    get_log().immediate("[UART][RX] raw={}".format(
                        " ".join("{:02X}".format(b) for b in memoryview(self._uart_read_buf)[:n])))
            else:
                # fallback:直接讀 UART(未啟動 circuit 時)
                chunk = self._uart.read()
                if chunk:
                    self._uart_rx_buf.extend(chunk)
                    got = True
                    get_log().immediate("[UART][RX] raw={}".format(
                        " ".join("{:02X}".format(b) for b in chunk)))

            if not got:
                return
            processed = 0
            i = 0
            while i + 4 < len(self._uart_rx_buf):
                if (self._uart_rx_buf[i] == _UART_SOF
                        and self._uart_rx_buf[i + 4] == _UART_EOF):
                    mode = self._uart_rx_buf[i + 1]
                    brightness = self._uart_rx_buf[i + 2] & _UART_BRIGHTNESS_MAX
                    time_remaining = self._uart_rx_buf[i + 3]
                    self._process_uart_cmd(mode, brightness, time_remaining)
                    processed = i + 5
                    break  # 只處理第一個幀
                i += 1

            if processed > 0:
                self._uart_rx_buf = self._uart_rx_buf[processed:]
            elif len(self._uart_rx_buf) > 256:
                # 防止垃圾資料無限累積，溢出時清空
                self._uart_rx_buf = bytearray()
        except Exception as e:
            get_log().error("[UART] recv error: {}".format(e))

    def _init_mp3(self):
        """初始化 MP3-TF-16P (從 boot/driver 註冊的 uart_list[1])"""
        uart_list = bus.get_service("uart_list")
        if not uart_list:
            get_log().warn("[MP3] uart_list missing")
            return
        if len(uart_list) < 2:
            get_log().warn("[MP3] uart_list[1] missing")
            return
        try:
            uart = uart_list[1]
            self._mp3 = MP3TF16P(uart)
            # 上電需等模組初始化完成，用非阻塞定時器
            self._mp3_state = 1
            self._mp3_deadline = time.ticks_add(time.ticks_ms(), 1500)
            get_log().info("[MP3] bind uart_list[1]")
        except Exception as e:
            get_log().error("[MP3] bind failed: {}".format(e))

    def _process_uart_cmd(self, mode, brightness, time_remaining):
        """處理收到的 UART 幀 — 更新內部狀態；echo 即為確認，通知面板"""
        prev_mode = self._display_mode
        self._temp_mode = mode
        self._temp_brightness = brightness

        mode_changed = self._temp_mode != prev_mode
        brightness_changed = self._display_brightness != brightness
        time_changed = self._display_time != time_remaining
        changed = mode_changed or brightness_changed or time_changed

        if mode_changed:
            self._display_mode = self._temp_mode
        if brightness_changed:
            self._display_brightness = brightness
        if time_changed:
            self._display_time = time_remaining

        if changed:
            # 同步到 bus.shared 供其他 task 讀取
            bus.shared["_display_mode"] = self._display_mode
            bus.shared["_temp_mode"] = self._temp_mode
            bus.shared["_display_brightness"] = self._display_brightness
            bus.shared["_temp_brightness"] = self._temp_brightness
            bus.shared["_display_time"] = self._display_time
            get_log().immediate("[UART] rx mod={} bit={} bri={} time={}".format(
                mode & MODE_VALUE,
                self._format_mode_bits(mode),
                brightness,
                time_remaining))
            self._log_runtime_state("uart-ack")

        # 回覆面板:模式/亮度有變, 或收到的是「待確認請求」的 echo。
        # 重點:即使 echo 值與現值完全相同(同模式重送/重複 echo), 也要廣播
        # 0x1502 讓面板結束 pending —— 這是同模式無法觸發回覆邏輯的根因。
        if mode_changed or brightness_changed or self._ack_pending:
            self._ack_pending = False
            get_log().immediate("[UART] ack mod={:02X} bit={} bri={} → 0x1502 回覆面板".format(
                self._display_mode & 0xFF,
                self._format_mode_bits(self._display_mode),
                self._display_brightness))
            self._notify_control_panel_ex_ic()

        # 只有收到確認模式與當前運行模式不同，才提交新模式並檢查電機/音頻
        if mode_changed:
            get_log().info("[Mode] switch mod={} bit={} bri={}".format(
                self._display_mode & MODE_VALUE,
                self._format_mode_bits(self._display_mode),
                self._display_brightness))
            self._check_mode_motor()
            self._check_mode_audio()

    def _format_mode_bits(self, mode):
        return "{:08b}".format(mode & 0xFF)

    def _format_frame_hex(self, data):
        return " ".join("{:02X}".format(b) for b in data)

    def _check_mode_motor(self):
        """
        MODE_RESERVED (Bit6): 1=目標頂部, 0=目標底部
        檢查當前位置與目標是否一致，不一致則移動。
        """
        reserved = bool(self._display_mode & MODE_RESERVED)
        if reserved:
            # Bit6=1 → 目標 top
            if self._position == "top":
                return  # 已在頂部，不動
            if self._state == STATE_IDLE:
                self._motor_control_source = "reserved"
                self._enter(STATE_PRE_DELAY, self._startup_delay_ms)
                get_log().immediate("[Motor] RESERVED=1 → 延遲等待後上升 (目標頂部)")
            elif self._state in (STATE_FALL, STATE_PRE_DELAY):
                self._enter(STATE_PRE_DELAY, self._startup_delay_ms)
                get_log().immediate("[Motor] RESERVED=1 → 延遲等待後切換上升")
        else:
            # Bit6=0 → 目標 bottom
            if self._position == "bottom":
                return  # 已在底部，不動
            if self._state == STATE_IDLE:
                self._motor_control_source = "reserved"
                self._enter(STATE_FALL)
                get_log().immediate("[Motor] RESERVED=0 → 下降 (目標底部)")
            elif self._state in (STATE_RISE, STATE_WAIT, STATE_PRE_DELAY):
                self._enter(STATE_FALL)
                get_log().immediate("[Motor] RESERVED=0 → 切換下降")

    def _check_mode_audio(self):
        """
        根據當前模式播放音效（每段播一次）。雙路徑路由（M5 貫通）：
          - bus 上有 audio_dac（新 WAV 音訊模組）→ 走 audio_cmd 播 WAV：
            映射 bus.shared["_mode_audio_map"] = {mode: <檔名 str>, ...}；
            int 值 = DFPlayer 曲目編號語意，新模組無法對應 → 警告並停。
          - 否則走舊 DFPlayer（MP3-TF-16P）路徑（track = 曲目編號，原語意）。
        """
        raw_mode = self._display_mode & MODE_VALUE
        audio_map = bus.shared.get("_mode_audio_map", None)
        if isinstance(audio_map, dict):
            track = audio_map.get(raw_mode)
        else:
            track = None if raw_mode == 0 else raw_mode   # mode 0 = standby 不播音

        # ── 新音訊模組路徑（WAV 串流）──
        if bus.get_service("audio_dac") is not None:
            if track is None:
                bus.shared["audio_cmd_stop"] = True
                return
            if isinstance(track, int):
                get_log().warn("[Audio] int track {} = DFPlayer 語意，新模組略過".format(track))
                bus.shared["audio_cmd_stop"] = True
                return
            bus.shared["audio_cmd_set"] = {
                "file_name": str(track), "play_mode": 0, "volume": 0}
            bus.shared["audio_cmd_play"] = {"start_ms": 0}
            get_log().info("[Audio] mode={} file={}".format(raw_mode, track))
            return

        # ── 舊 DFPlayer 路徑（原語意）──
        if self._mp3 is None or self._mp3_state != 2:
            return
        if track is None:
            self._mp3.stop()
            return
        self._mp3.stop()
        time.sleep_ms(30)
        self._mp3.play_track(track)
        get_log().info("[Audio] mode={} track={}".format(raw_mode, track))

    def _toggle_mode_flag(self, flag):
        self.set_display_state(mode=self._display_mode ^ flag)

    def _notify_control_panel_ex_ic(self):
        """模式確認後廣播 0x1502 WTT_STATUS(mode/brightness/time) 給面板裝置。
        on_status 寫 _display_* Global → LVGL 頁面顯示已確認。取代舊 0x1403。"""
        now_bus = self._now_bus or bus.get_service("NowBus")
        if now_bus is None:
            get_log().error("[WTT][TX][0x1502] NowBus 未註冊 — 回覆無法送出！"
                            "檢查 NetworkTask ESP-NOW 是否啟用(config Network.ESP_now.enable)")
            return
        self._now_bus = now_bus
        try:
            payload = bytes([
                self._display_mode & 0xFF,
                max(0, min(self._display_brightness, 255)),
                max(0, self._display_time & 0xFF),
            ])
            now_bus.broadcast(Proto.pack(CMD_WTT_STATUS, payload))
            get_log().immediate("[WTT][TX][0x1502] mod={:02X} bri={} time={}".format(
                self._display_mode & 0xFF,
                self._display_brightness,
                self._display_time))
        except Exception as e:
            get_log().error("[WTT] status send failed: {}".format(e))

    def _send_wtt_status_periodic(self, now):
        """每秒廣播一次 0x1502 狀態(帶 time,供面板倒數同步)。預設關閉,開則
        bus.shared["_wtt_periodic_status"] = 1。"""
        if not bus.shared.get("_wtt_periodic_status", 0):
            return
        if time.ticks_diff(now, self._wtt_status_timer) < 1000:
            return
        self._wtt_status_timer = now
        self._notify_control_panel_ex_ic()

    def _next_mode(self):
        max_mode = self._max_mode
        flags = self._display_mode & (MODE_SPECIAL | MODE_RESERVED)
        if max_mode < 0:
            return flags
        val = (self._display_mode & MODE_VALUE) + 1
        return flags | (val % (max_mode + 1))

    def _handle_vbtn_short(self, btn_id):
        if btn_id == 0:
            if self._state != STATE_IDLE:
                return  # 馬達運轉中，無視短按
            self._toggle_mode_flag(MODE_RESERVED)
        elif btn_id == 1:
            self.set_display_state(mode=self._next_mode())

    def _handle_vbtn_long(self, btn_id):
        if btn_id == 0:
            self._trigger_motor_action()
        elif btn_id == 1:
            self._toggle_mode_flag(MODE_SPECIAL)

    def _poll_vbtn(self, btn_id, now_btn):
        state = HW.get(HW.VBTN, btn_id)
        if state is None:
            state = 1

        if state == 0 and self._last_vbtn[btn_id] == 1:
            self._vbtn_press_time[btn_id] = now_btn
            self._vbtn_long_triggered[btn_id] = False
            self._last_vbtn[btn_id] = 0
        elif (state == 0
              and self._vbtn_press_time[btn_id] > 0
              and not self._vbtn_long_triggered[btn_id]
              and self._last_vbtn[btn_id] == 0):
            if time.ticks_diff(now_btn, self._vbtn_press_time[btn_id]) >= _LONG_PRESS_MS:
                self._vbtn_long_triggered[btn_id] = True
                self._handle_vbtn_long(btn_id)
        elif state == 1 and self._vbtn_press_time[btn_id] > 0 and self._last_vbtn[btn_id] == 0:
            if not self._vbtn_long_triggered[btn_id]:
                self._handle_vbtn_short(btn_id)
            self._last_vbtn[btn_id] = 1
            self._vbtn_press_time[btn_id] = 0
            self._vbtn_long_triggered[btn_id] = False

    def set_display_state(self, mode=None, brightness=None):
        """
        設定顯示狀態（外部呼叫用）
        - mode/brightness 有提供時一律發送 UART(即使與現值相同:
          同模式重送/echo 遺失重送也要走完整 request→ack 循環,否則面板收不到確認)
        - time 由 DisplayController 管理，不在此設定
        """
        target_mode = self._display_mode
        target_brightness = self._temp_brightness
        send = False
        if mode is not None:
            max_mode = self._max_mode
            flags = mode & (MODE_SPECIAL | MODE_RESERVED)
            val = mode & MODE_VALUE
            if max_mode > 0 and val > max_mode:
                mode = flags | (val % (max_mode + 1))
            target_mode = mode
            send = True
        if brightness is not None:
            target_brightness = max(0, min(brightness, _UART_BRIGHTNESS_MAX))
            send = True

        if send:
            self._temp_brightness = target_brightness
            bus.shared["_temp_brightness"] = self._temp_brightness
            self._ack_pending = True    # 送出的幀即為待確認請求
            self._send_uart_state(
                mode=target_mode,
                brightness=target_brightness,
                time_remaining=self._display_time,
            )
            # 本地只送 request，待 UART 回覆後才提交新模式
            if mode is not None:
                get_log().info("[Mode][REQ] mod={} bit={} bri={}".format(
                    target_mode & MODE_VALUE,
                    self._format_mode_bits(target_mode),
                    target_brightness))

    def _consume_encoder_delta(self):
        delta = int(bus.shared.get(_ENC_DELTA_KEY, 0) or 0)
        if delta == 0:
            return
        bus.shared[_ENC_DELTA_KEY] = 0
        target = self._temp_brightness + delta
        self.set_display_state(brightness=target)
        get_log().immediate("[ENC] delta={:+d} bri_req={}".format(
            delta,
            max(0, min(target, _UART_BRIGHTNESS_MAX))))

    def _consume_display_cmd(self):
        """消費 bus 指令 _display_cmd（waiting_to_trash_actions 翻譯進來，或 LVGL 頁面直寫）。
        格式: {"mode": 0-255, "brightness": 0-36}（欄位可選）。消費後清空。
        開機第一筆指令的 brightness 忽略:面板上線時預設亮度 0,執行板開機時
        已先收到外部處理器的狀態(對方已發送的亮度為準),直接轉發會第一下重設對方。
        mode 仍照常轉發(模式 0 是合法值)。"""
        cmd = bus.shared.get("_display_cmd")
        if not cmd:
            return
        bus.shared["_display_cmd"] = None
        try:
            if not self._bright_ready and "brightness" in cmd:
                # 第一筆:只轉發 mode,忽略 brightness(對方亮度為準)
                self._bright_ready = True
                get_log().immediate("[CMD] 首筆指令 — 忽略 brightness,亮度以執行板/對方為準")
                cmd = dict(cmd)
                cmd.pop("brightness", None)
            self._bright_ready = True
            self.set_display_state(
                mode=cmd.get("mode"),
                brightness=cmd.get("brightness"),
            )
        except Exception as e:
            get_log().error("[CMD] display cmd: {}".format(e))

    # ═══ 主迴圈 ═══

    def loop(self):
        if not self.running:
            return
        now = time.ticks_ms()

        # ── MP3 初始化後續 (非阻塞等 1.5s) ──
        if self._mp3_state == 1 and time.ticks_diff(self._mp3_deadline, now) <= 0:
            self._mp3.switch_drive(1)
            self._mp3.stop()
            self._mp3_state = 2

        # ── UART 接收 ──
        self._handle_uart_receive()
        self._consume_encoder_delta()
        self._consume_display_cmd()

        # ── 週期狀態廣播(選用:每秒同步 time 給面板) ──
        self._send_wtt_status_periodic(now)

        now_btn = time.ticks_ms()
        self._poll_vbtn(0, now_btn)
        self._poll_vbtn(1, now_btn)

        # ── 計時: 階段到期 → 下一階段 ──
        if self._state != STATE_IDLE and self._deadline > 0:
            if time.ticks_diff(now, self._deadline) >= 0:
                if self._state == STATE_PRE_DELAY:
                    if self._startup_phase == 1:
                        self._startup_phase = 2
                        self._enter(STATE_FALL)
                    else:
                        self._enter(STATE_RISE)
                elif self._state == STATE_RISE:
                    self._position = "top"
                    self._enter(STATE_WAIT)
                elif self._state == STATE_WAIT:
                    self._enter(STATE_FALL)
                elif self._state == STATE_FALL:
                    if self._startup_phase == 2:
                        self._startup_phase = 0
                    self._position = "bottom"
                    self._enter(STATE_IDLE)
                self.success += 1

    def _trigger_motor_action(self):
        """VBTN[0] 長按: 直接控制電機升降（無延遲）"""
        if not self._is_motor_enabled():
            get_log().immediate("[Motor] 已禁用 — 按鈕忽略")
            return
        if self._state == STATE_IDLE:
            self._motor_control_source = "manual"
            if self._position == "top":
                self._enter(STATE_FALL)  # 在頂部 → 下降
            else:
                self._enter(STATE_RISE)  # 直接上升，無延遲
        elif self._state == STATE_PRE_DELAY:
            if self._is_reserved_motor_control():
                self._enter(STATE_IDLE)
                get_log().info("[Motor] 長按取消 reserved 延遲 → 閒置")
            else:
                self._enter(STATE_FALL)
                get_log().immediate("[Motor] 長按取消延遲 → 下降")
        elif self._state in (STATE_RISE, STATE_WAIT):
            self._enter(STATE_FALL)   # 提早下降
        elif self._state == STATE_FALL:
            get_log().immediate("[Motor] 下降中忽略手動觸發")
        self.success += 1

    def on_stop(self):
        self._motor_stop()
        self._deadline = 0
        super().on_stop()
