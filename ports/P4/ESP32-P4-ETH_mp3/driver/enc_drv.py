"""
enc_drv.py — 硬體編碼器管理

設定來源: bus.shared["ENC"]  ({enable, list:[{id, GPIO:{a, b}}]})
產物:    bus.register_service("enc_list", [Encoder_obj, ...])
         bus.register_service("enc_by_id", {id: Encoder_obj})

說明:ESP32 machine.Encoder 是硬體周邊(需專屬 Pin),
      不能放 PIN 段(會被 init_pins 建成普通 GPIO Pin 跟硬體 encoder 衝突)。
      故獨立 driver,跟 spi/pwm 同風格。
"""
from machine import Pin
try:
    from machine import Encoder
except Exception:
    Encoder = None
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_enc(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("ENC") or {}
    if not cfg.get("enable"):
        return []
    if Encoder is None:
        get_log().warn("ENC: machine.Encoder not available, skipped")
        return []

    enc_list = []
    enc_by_id = {}
    for item in cfg.get("list", []):
        eid = item.get("id", len(enc_list))
        gpio = item.get("GPIO") or {}
        a = gpio.get("a")
        b = gpio.get("b")
        if a is None or b is None:
            continue
        enc = Encoder(int(eid), Pin(int(a), Pin.IN, Pin.PULL_UP),
                      Pin(int(b), Pin.IN, Pin.PULL_UP))
        enc_list.append(enc)
        enc_by_id[int(eid)] = enc

    sysbus.register_service("enc_list", enc_list)
    sysbus.register_service("enc_by_id", enc_by_id)
    get_log().info("ENC: {} encoder(s)".format(len(enc_list)))
    return enc_list


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("ENC") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for i, item in enumerate(cfg.get("list", [])):
        gpio = item.get("GPIO") or {}
        eid = item.get("id", i)
        for k in ("a", "b"):
            g = gpio.get(k)
            if g is not None:
                result[int(g)] = "enc{}_{}".format(eid, k)
    return result
