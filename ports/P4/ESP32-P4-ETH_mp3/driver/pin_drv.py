"""
pin_drv.py — GPIO 腳位管理

設定來源: bus.shared["PIN"]  ({enable, list})
產物:    bus.register_service("pin_list", [Pin_obj, ...])
         bus.register_service("pin_by_label", {label: Pin_obj})
"""
from lib.sys.hw_manager import init_pins
from lib.sys.sys_bus import bus


def init_pin(sysbus=None):
    """讀 bus.shared['PIN'] → 建 Pin → 註冊 pin_list / pin_by_label"""
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PIN") or {}
    if not cfg.get("enable"):
        return []

    return init_pins(cfg.get("list", []))


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PIN") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for item in cfg.get("list", []):
        gpio = item.get("GPIO")
        if gpio is not None:
            result[gpio] = item.get("label", "pin_{}".format(gpio))
    return result
