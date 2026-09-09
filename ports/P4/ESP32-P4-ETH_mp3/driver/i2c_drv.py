"""
i2c_drv.py — I2C 匯流排管理

設定來源: bus.shared["I2C"]  ({enable, list})
產物:    bus.register_service("i2c_list", [I2C_obj, ...])
"""
from machine import Pin, I2C
from lib.sys.sys_bus import bus


def init_i2c(sysbus=None):
    """讀 bus.shared['I2C'] → 建 I2C → 註冊 'i2c_list'"""
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("I2C") or {}
    if not cfg.get("enable"):
        return []

    i2c_list = []
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        i2c = I2C(
            item["id"],
            freq=item.get("freq"),
            scl=Pin(gpio["scl"]) if gpio.get("scl") is not None else None,
            sda=Pin(gpio["sda"]) if gpio.get("sda") is not None else None,
        )
        i2c_list.append(i2c)

    sysbus.register_service("i2c_list", i2c_list)
    return i2c_list


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("I2C") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        sid = item.get("id", "?")
        for name in ("scl", "sda"):
            pin = gpio.get(name)
            if pin is not None:
                result[pin] = "i2c{}_{}".format(sid, name)
    return result
