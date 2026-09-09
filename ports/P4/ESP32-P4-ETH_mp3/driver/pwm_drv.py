"""
pwm_drv.py — PWM 管理

設定來源: bus.shared["PWM"]  ({enable, list})
產物:    bus.register_service("pwm_list", [PWM_obj, ...])
"""
from machine import Pin, PWM
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_pwm(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PWM") or {}
    if not cfg.get("enable"):
        return []

    pwm_list = []
    for item in cfg.get("list", []):
        gpio = item.get("GPIO")
        if gpio is None:
            continue
        pwm = PWM(Pin(gpio), freq=1000, duty=0)
        pwm_list.append(pwm)

    sysbus.register_service("pwm_list", pwm_list)
    get_log().info("PWM: {} channel(s)".format(len(pwm_list)))
    return pwm_list


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PWM") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for i, item in enumerate(cfg.get("list", [])):
        gpio = item.get("GPIO")
        if gpio is not None:
            result[gpio] = "pwm_{}".format(i)
    return result
