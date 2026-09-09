"""
xl9555_drv.py — XL9555 IO Expander (走 I2C)

設定來源: bus.shared["XL9555"]  ({enable, i2c, addr, list})
         list item: {"IO":0, "label":"...", "mode":"OUT", "initial":1}
產物:    bus.register_service("xl9555", xl)
         (pins 追加進既有 pin_list / pin_by_label)
"""
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_xl9555(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("XL9555") or {}
    if not cfg.get("enable"):
        return None

    i2c_list = sysbus.get_service("i2c_list") or []
    i2c_idx = cfg.get("GPIO", {}).get("i2c", 0)
    if i2c_idx >= len(i2c_list):
        get_log().error("xl9555: i2c index {} not available".format(i2c_idx))
        return None
    i2c = i2c_list[i2c_idx]

    dev_addr = cfg.get("addr", 0x20)
    if isinstance(dev_addr, str):
        dev_addr = int(dev_addr, 16)

    from lib.hw.xl9555 import XL9555, PIN_OUT
    xl = XL9555(i2c, dev_addr)

    # 初始化 IO
    for item in cfg.get("list", []):
        pin = item["IO"]
        mode = item.get("mode", "IN")
        xl.pin[pin].init(PIN_OUT if mode == "OUT" else 0)
        if mode == "OUT":
            xl.pin[pin].value(item.get("initial", 0))

    sysbus.register_service("xl9555", xl)

    # 將 xl9555 pins 追加到 bus 統一池
    pin_list = sysbus.get_service("pin_list") or []
    pin_by_label = sysbus.get_service("pin_by_label") or {}
    for item in cfg.get("list", []):
        p = xl.pin[item["IO"]]
        pin_list.append(p)
        if item.get("label"):
            pin_by_label[item["label"]] = p
    sysbus.register_service("pin_list", pin_list)
    sysbus.register_service("pin_by_label", pin_by_label)

    get_log().info("xl9555: OK (addr=0x{:02X})".format(dev_addr))
    return xl


def gpios(sysbus=None):
    # XL9555 走 I2C，IO 屬於擴展晶片不佔主控 GPIO
    return {}
