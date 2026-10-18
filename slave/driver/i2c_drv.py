"""
i2c_drv.py — I2C 匯流排管理

設定來源: bus.shared["I2C"]  ({enable, list})
產物:    bus.register_service("i2c_list", [I2C_obj, ...])
         bus.shared["_i2c_devices"] = {bus_idx: [addr, ...]}   ← 建好就掃一次
"""
from machine import Pin, I2C
from lib.sys.sys_bus import bus


def scan_devices(sysbus=None):
    """掃所有已啟用的 I2C 匯流排 → bus.shared["_i2c_devices"] = {bus_idx: [addr]}。

    單一匯流排掃壞記 []、不影響其他條；回傳同一份 dict。
    """
    sysbus = sysbus or bus
    out = {}
    for i, io in enumerate(sysbus.get_service("i2c_list") or []):
        try:
            out[i] = io.scan()
        except Exception as e:
            print("[I2C] bus {} scan error: {}".format(i, e))
            out[i] = []
    sysbus.shared["_i2c_devices"] = out
    return out


def init_i2c(sysbus=None):
    """讀 bus.shared['I2C'] → 建 I2C → 註冊 'i2c_list' → 掃一次存快取"""
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

    # 建好就掃一次：之後 UI 直接讀那一格，pca9685_drv 也讀它（不再自己掃）
    d = scan_devices(sysbus)
    print("[I2C] devices: {}".format(
        {k: [hex(a) for a in v] for k, v in d.items()}))
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
