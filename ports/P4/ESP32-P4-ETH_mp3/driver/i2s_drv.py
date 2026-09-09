"""
i2s_drv.py — I2S 匯流排管理（TX/RX 雙模式）

設定來源: bus.shared["I2S"]  ({enable, list})
          list item: {"GPIO": {sck, ws, sd},
                      "config": {mode("tx"/"rx"), bits, format("stereo"/"mono"),
                                 rate, ibuf}}
產物:    bus.register_service("i2s_list", [I2S_obj, ...])

模組層（PCM5102 等）以 {"i2s": <index>} 引用本匯流排（同 PCA9685→{"i2c":0}、
APA102→{"spi":0} 慣例）；模組自己的腳位（如 PCM5102 的 XSMT）不歸本 driver 管。
ESP32-S3 只有一個 I2S 週邊（id=0），列表索引只是邏輯編號。
"""
from machine import Pin, I2S
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log

_MODE_MAP = {"tx": I2S.TX, "rx": I2S.RX}
_FMT_MAP = {"mono": I2S.MONO, "stereo": I2S.STEREO}


def init_i2s(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("I2S") or {}
    if not cfg.get("enable"):
        return []

    i2s_list = []
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        icfg = item.get("config", {})
        mode = _MODE_MAP.get(str(icfg.get("mode", "rx")).lower(), I2S.RX)
        fmt = _FMT_MAP.get(str(icfg.get("format", "stereo")).lower(), I2S.STEREO)
        rate = int(icfg.get("rate", 16000) or 16000)
        bits = int(icfg.get("bits", 16) or 16)
        ibuf = int(icfg.get("ibuf", 0) or rate * 4 * 2)
        try:
            audio_i2s = I2S(
                0,
                sck=Pin(gpio["sck"]),
                ws=Pin(gpio["ws"]),
                sd=Pin(gpio["sd"]),
                mode=mode,
                bits=bits,
                format=fmt,
                rate=rate,
                ibuf=ibuf,
            )
        except Exception as e:
            get_log().error("I2S init error: {}".format(e))
            continue
        i2s_list.append(audio_i2s)

    sysbus.register_service("i2s_list", i2s_list)
    get_log().info("I2S: {} device(s)".format(len(i2s_list)))
    return i2s_list


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("I2S") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        for name in ("sck", "ws", "sd"):
            pin = gpio.get(name)
            if pin is not None:
                result[pin] = "i2s_{}".format(name)
    return result
