"""
統一硬體資源管理器

整合所有硬體資源的初始化查找與讀寫，取代各處分散的 cache 和直接存取。

使用方式:
  from lib.sys.hw_manager import HW
  HW.get(HW.PWM, 0)        # 讀取 pwm_list[0] 的 duty
  HW.set(HW.PWM, 0, 512)   # 寫入 pwm_list[0] 的 duty
  HW.get(HW.PIN, 8)        # 讀取 GPIO 8 的值（自動快取 Pin 物件）
  HW.set(HW.PIN, 8, 1)     # 寫入 GPIO 8
  HW.get(HW.VBTN, 1)       # 讀取虛擬按鈕 ID=1 的值 (0/1)
  HW.set(HW.VBTN, 1, 1)    # 寫入虛擬按鈕 ID=1
  HW.vbtn_buf()             # 取得虛擬按鈕原始 bytearray (32B, 供高效輪詢)
  HW.list_all()             # 列出所有已註冊的硬體資源

虛擬按鈕緩衝存放於 bus.shared["_vbtn"] (Global 區域)，可供所有任務存取。
"""

import time
from machine import Pin
from lib.sys.sys_bus import bus

# -- 設備類型常數 --
PIN  = 0
PWM  = 1
SPI  = 2
I2C  = 3
PIXEL = 4
LCD  = 5
SD   = 6
UART = 7
VBTN = 8  # 虛擬按鈕: ID 0-255, 值 0/1, 32-byte bitfield

# -- 虛擬按鈕緩衝大小 --
_VBTN_BYTES = 32

# -- Pin 快取 (單例) --
#   優先使用 boot 註冊的 pin_list（已設好 mode/pull/initial），
#   否則才自行建立 Pin(gpio, OUT)。
_PIN_CACHE = {}

_PIN_MODE_MAP = {
    "IN": Pin.IN,
    "OUT": Pin.OUT,
}

_PIN_PULL_MAP = {
    "UP": Pin.PULL_UP,
    "DOWN": Pin.PULL_DOWN,
}


def _get_pin(gpio_num):
    if gpio_num in _PIN_CACHE:
        return _PIN_CACHE[gpio_num]
    _PIN_CACHE[gpio_num] = Pin(gpio_num, Pin.OUT)
    return _PIN_CACHE[gpio_num]


def get_pin_configured(label):
    """從統一資源取得已配置的 Pin（安全版）。

    依序查 pin_by_label / pin_list / _PIN_CACHE，三處皆無就回 None，
    絕不自行 new Pin()。task 要讀按鈕/腳位一律走這裡，避免自行初始化
    踩到其他外設（如 WiFi SDMMC）佔用的 GPIO。
    """
    cfg = bus.shared.get("PIN") or {}
    lst = cfg.get("list") or []

    # 1. pin_by_label（boot 有 enable PIN 時註冊）
    pin_by_label = bus.get_service("pin_by_label")
    if isinstance(pin_by_label, dict) and label in pin_by_label:
        return pin_by_label[label]

    # 2. pin_list（boot 有 enable PIN 時註冊，與 list 同序）
    pin_list = bus.get_service("pin_list")
    if isinstance(pin_list, list):
        for i, item in enumerate(lst):
            if isinstance(item, dict) and item.get("label") == label and i < len(pin_list):
                return pin_list[i]

    # 3. _PIN_CACHE（boot 已初始化過的腳位）
    for item in lst:
        if isinstance(item, dict) and item.get("label") == label:
            gpio = int(item.get("GPIO", -1))
            if gpio in _PIN_CACHE:
                return _PIN_CACHE[gpio]
            return None
    return None


def get_pin_configured_gpio(label):
    """回傳 config 中 label 對應的 GPIO 編號（純查詢，不初始化），找不到回 None。"""
    cfg = bus.shared.get("PIN") or {}
    lst = cfg.get("list") or []
    for item in lst:
        if isinstance(item, dict) and item.get("label") == label:
            gpio = item.get("GPIO")
            return int(gpio) if gpio is not None else None
    return None


def resolve_pin(gpio_num):
    """相容舊介面：回傳指定 GPIO 的 Pin 物件。"""
    return _get_pin(gpio_num)


def _vbtn_buf():
    """從 Global 區域 (bus.shared) 取得/初始化虛擬按鈕緩衝"""
    key = "_vbtn"
    if key not in bus.shared:
        # vbtn 採用 active-low 語意: 0=按下, 1=放開
        # 開機預設應為全放開，避免未同步前被誤判為長按
        bus.shared[key] = bytearray([0xFF] * _VBTN_BYTES)
    return bus.shared[key]


def _init_pin_from_list():
    """由 boot 呼叫，把 pin_list 中的 Pin 物件填入快取"""
    plist = bus.get_service("pin_list")
    if plist is None:
        return
    cfg = bus.shared.get("PIN", {}) or {}
    items = cfg.get("list", []) or []
    for i, entry in enumerate(items):
        gpio = entry.get("GPIO")
        if gpio is not None and i < len(plist):
            _PIN_CACHE[gpio] = plist[i]


def init_pins(config_list):
    """
    相容舊版 driver/pin_drv.py 介面。
    根據 CONFIG 建立 Pin 物件、註冊 `pin_list` / `pin_by_label`，
    並同步填入 `_PIN_CACHE`。
    """
    pin_cfg = bus.shared.get("PIN")
    if not isinstance(pin_cfg, dict):
        pin_cfg = {}
        bus.shared["PIN"] = pin_cfg
    pin_cfg["list"] = [dict(entry) for entry in (config_list or [])]

    pin_list = bus.get_service("pin_list")
    if pin_list is None:
        pin_list = []
        bus.register_service("pin_list", pin_list)

    pin_by_label = bus.get_service("pin_by_label")
    if pin_by_label is None:
        pin_by_label = {}
        bus.register_service("pin_by_label", pin_by_label)

    for entry in config_list or []:
        gpio = entry.get("GPIO")
        if gpio is None:
            continue

        mode_name = str(entry.get("mode", "OUT")).upper()
        mode = _PIN_MODE_MAP.get(mode_name, Pin.OUT)
        pull_name = entry.get("pull")
        pull = _PIN_PULL_MAP.get(str(pull_name).upper()) if pull_name else None
        initial = entry.get("initial")

        if pull is not None:
            pin = Pin(gpio, mode, pull)
        else:
            pin = Pin(gpio, mode)
        if mode == Pin.OUT and initial is not None:
            pin.value(1 if initial else 0)

        pin_list.append(pin)
        _PIN_CACHE[gpio] = pin
        label = entry.get("label")
        if label:
            pin_by_label[label] = pin
    return pin_list


def get(dev_type, dev_id=None):
    try:
        if dev_type == PIN:
            return _get_pin(dev_id).value()
        elif dev_type == PWM:
            lst = bus.get_service("pwm_list")
            if lst and 0 <= dev_id < len(lst):
                return lst[dev_id].duty()
        elif dev_type == SPI:
            lst = bus.get_service("spi_list")
            if lst and 0 <= dev_id < len(lst):
                return lst[dev_id]
        elif dev_type == I2C:
            lst = bus.get_service("i2c_list")
            if lst and 0 <= dev_id < len(lst):
                return lst[dev_id]
        elif dev_type == PIXEL:
            lst = bus.get_service("pixel_list")
            if lst and 0 <= dev_id < len(lst):
                return lst[dev_id]
        elif dev_type == LCD:
            return bus.get_service("lcd")
        elif dev_type == VBTN:
            if not (0 <= dev_id <= 255):
                return 0
            buf = _vbtn_buf()
            byte_idx = dev_id >> 3
            bit_idx = dev_id & 0x07
            return (buf[byte_idx] >> bit_idx) & 1
    except Exception:
        pass
    return None


def set(dev_type, dev_id, value):
    try:
        if dev_type == PIN:
            _get_pin(dev_id).value(1 if value else 0)
        elif dev_type == PWM:
            lst = bus.get_service("pwm_list")
            if lst and 0 <= dev_id < len(lst):
                lst[dev_id].duty(int(value))
        elif dev_type == VBTN:
            if not (0 <= dev_id <= 255):
                return
            buf = _vbtn_buf()
            byte_idx = dev_id >> 3
            bit_idx = dev_id & 0x07
            if value:
                buf[byte_idx] = buf[byte_idx] | (1 << bit_idx)
            else:
                buf[byte_idx] = buf[byte_idx] & ~(1 << bit_idx)
    except Exception:
        pass


def vbtn_buf():
    """回傳 bus.shared["_vbtn"] 原始 bytearray，供高效 byte-level diff 輪詢"""
    return _vbtn_buf()


def list_all():
    rows = []
    for name in ("pin_list", "pwm_list", "spi_list", "i2c_list",
                 "pixel_list", "ws2812_list", "apa1022_list", "pca9685_list",
                 "lcd", "data_Phat", "circuit_bus_list", "st_pixel"):
        svc = bus.get_service(name)
        if svc is not None:
            rows.append(name)
    rows.append("_PIN_CACHE ({})".format(len(_PIN_CACHE)))
    buf = bus.shared.get("_vbtn")
    if buf is not None:
        rows.append("_vbtn ({}B, Global)".format(len(buf)))
    return rows


# ══════════════════════════════════════════════════════
# 統一輸入採樣 (HwSampleTask 用)
#
# 沿用 VBTN「快照進 bus」模式，把 Encoder + IN Pin 也補上同層快照。
# 由 HwSampleTask 每 loop 週期呼叫 sample_inputs() 採樣一次，
# 消費者（LVGL / action task 等）讀 get_input() 快照，不直接碰 GPIO。
#
# bus.shared["_hw_inputs"]:
#   {"enc": [delta0, delta1, ...],   # encoder 未消費累加 delta(集中累加)
#    "pin": {"encC": 0, "btn": 1},   # IN 腳當前值(按 config label,原始未去抖)
#    "pin_edge": {"encC": 0, "btn": 0},  # 去抖後按壓邊緣累加(active-low,消費端清除)
#    "pin_stable": {...},            # 去抖後接受值(內部用)
#    "_pin_state": {...},            # 去抖候選狀態 + 起始 ticks_ms(內部用)
#    "_enc_last": [...]}             # 上次 encoder 原值(內部用)
# enc[i] / pin_edge[label] 為「累加」語意:消費端用 consume_input 讀取即清除;
# get_input 讀到的是尚未被消費的累加值(enc)或當前電平(pin)。
# ══════════════════════════════════════════════════════
_HW_INPUTS = "_hw_inputs"

# IN Pin 去抖時間(ms):狀態需連續穩定超過此時間才接受(對齊舊 ControlPanelTask 30ms)
_PIN_DEBOUNCE_MS = 30


def sample_inputs():
    """統一採樣所有輸入硬體當前值 → 快照進 bus.shared["_hw_inputs"]。
    由 HwSampleTask 每 loop 呼叫一次。消費者讀 get_input() 快照,不碰硬體。

    Encoder: 累加 delta(與上次差值累加,消費端讀取即清) ← 邊緣計算集中在此
    IN Pin:  讀 value,放 pin[label]            ← 按 config PIN 段的 label
    VBTN:    已有 _vbtn,不重複(維持現狀)
    """
    snap = bus.shared.get(_HW_INPUTS)
    if snap is None:
        snap = {"enc": [], "pin": {}, "pin_edge": {}, "pin_stable": {},
                "_pin_state": {}, "_enc_last": []}
        bus.shared[_HW_INPUTS] = snap

    # ── Encoder delta(集中邊緣計算;累加供消費端一次取走,不因採樣覆寫掉格)──
    enc_list = bus.get_service("enc_list") or []
    n_enc = len(enc_list)
    if len(snap["_enc_last"]) != n_enc:
        # encoder 數量變動或首次:重建基準,delta 歸零
        snap["_enc_last"] = [e.value() for e in enc_list]
        snap["enc"] = [0] * n_enc
    else:
        for i in range(n_enc):
            v = enc_list[i].value()
            d = v - snap["_enc_last"][i]
            snap["_enc_last"][i] = v
            if d:
                snap["enc"][i] += d
                # 平行累加進 _enc_delta(motor 調亮度通道,與 hw_actions 的
                # 跨板累加同 key、同語意;消費者 ActionTask1 讀取即清)。
                # 注意:若日後重啟 ControlPanelTask(它也寫 _enc_delta),
                # 會變成雙生產者,需二選一。
                cur = int(bus.shared.get("_enc_delta", 0) or 0)
                bus.shared["_enc_delta"] = cur + d

    # ── IN Pin(只採 mode=IN 的,按 label;OUT 腳是輸出不需快照)──
    # 電平照樣快照進 pin;另做去抖 + 按壓邊緣累加進 pin_edge。
    # 消費端(如 LVGL confirm/exit)用 consume_input("pin") 讀取即清,
    # 避免「按住按鈕被每幀重複觸發」的雙擊/抖動問題。
    snap.setdefault("pin_edge", {})
    snap.setdefault("pin_stable", {})
    snap.setdefault("_pin_state", {})
    pin_by_label = bus.get_service("pin_by_label") or {}
    pin_cfg = (bus.shared.get("PIN") or {}).get("list") or []
    for item in pin_cfg:
        if str(item.get("mode", "")).upper() != "IN":
            continue
        label = item.get("label")
        if label and label in pin_by_label:
            v = pin_by_label[label].value()
            snap["pin"][label] = v
            if label not in snap["pin_stable"]:
                # 首次:直接接受當前值,不產生邊緣
                snap["pin_stable"][label] = v
                snap["_pin_state"][label] = [v, time.ticks_ms()]
                continue
            st = snap["_pin_state"][label]
            if st[0] != v:
                # 狀態跳變:重啟去抖候選(彈跳期間不斷重置,穩定了才開始計時)
                snap["_pin_state"][label] = [v, time.ticks_ms()]
            elif v != snap["pin_stable"][label] and \
                    time.ticks_diff(time.ticks_ms(), st[1]) >= _PIN_DEBOUNCE_MS:
                # 新狀態穩定超過去抖時間:接受;按下(active-low v==0)記一次邊緣
                snap["pin_stable"][label] = v
                if v == 0:
                    snap["pin_edge"][label] = snap["pin_edge"].get(label, 0) + 1

    return snap


def get_input(kind, key=None, idx=None):
    """消費者讀快照(不碰硬體)。
       get_input("enc", idx=0)        → encoder 0 的 delta(不存在回 0)
       get_input("pin", key="encC")   → encC 腳當前值(不存在回 None)
    """
    snap = bus.shared.get(_HW_INPUTS)
    if snap is None:
        return 0 if kind == "enc" else None
    if kind == "enc":
        lst = snap.get("enc", [])
        if idx is not None and 0 <= idx < len(lst):
            return lst[idx]
        return 0
    if kind == "pin":
        return snap.get("pin", {}).get(key)
    return None


def consume_input(kind, key=None, idx=None):
    """消費型讀取(讀取即清除)。目前 enc / pin 皆有累加語意:
       consume_input("enc", idx=0)      → 讀 encoder 0 的累加 delta 並歸零
       consume_input("pin", key="encC") → 讀 encC 的去抖按壓邊緣次數並歸零
       (非累加型別回退到 get_input,不消費)。
    跨核 race:採樣端(core1)「+=」與消費端(core0)「讀取→歸零」之間有極小
    窗口,偶發少 1 次,肉眼不可見;與既有快照模式同級。"""
    snap = bus.shared.get(_HW_INPUTS)
    if snap is None:
        return 0
    if kind == "enc":
        lst = snap.get("enc", [])
        if idx is not None and 0 <= idx < len(lst):
            v = lst[idx]
            lst[idx] = 0
            return v
        return 0
    if kind == "pin":
        edges = snap.get("pin_edge", {})
        v = edges.get(key, 0)
        if v:
            edges[key] = 0
        return v
    return get_input(kind, key=key, idx=idx)


# -- 單例物件 --
HW = type("HW", (), {
    "PIN": PIN, "PWM": PWM, "SPI": SPI, "I2C": I2C,
    "PIXEL": PIXEL, "LCD": LCD, "SD": SD, "UART": UART, "VBTN": VBTN,
    "get": staticmethod(get),
    "set": staticmethod(set),
    "resolve_pin": staticmethod(resolve_pin),
    "vbtn_buf": staticmethod(vbtn_buf),
    "list_all": staticmethod(list_all),
    "sample_inputs": staticmethod(sample_inputs),
    "get_input": staticmethod(get_input),
    "consume_input": staticmethod(consume_input),
})
