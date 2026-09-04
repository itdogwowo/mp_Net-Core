"""
sd_drv.py — SD 卡管理

設定來源: bus.shared["SDcard"]  ({enable, phat, config, GPIO})
產物:    bus.register_service("data_Phat", phat)
         bus.register_service("sd_raw", sd)

LDO 電源由 machine.SDCard 內部管理 (P4 預設取得 channel 4，
S3 無 LDO 硬體、ldo kwarg 不編譯進去)，driver 層不介入。
"""
import machine
import os
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_sd(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("SDcard") or {}
    if not cfg.get("enable"):
        sysbus.register_service("data_Phat", "")
        return ""

    phat = cfg.get("phat", "/sd")

    sd_cfg = cfg.get("config", {})
    slot = sd_cfg.get("slot", 0)
    try:
        if slot >= 2:
            _init_sd_spi(sysbus, cfg, phat)
        else:
            _init_sd_sdio(sysbus, cfg, phat)
        get_log().info("SD card mounted on {}".format(phat))
    except Exception as e:
        get_log().error("SD card init error: {}".format(e))
        if not _exists(phat):
            os.mkdir(phat)
        open(phat + "/local", "w").close()

    sysbus.register_service("data_Phat", phat)
    return phat


def _init_sd_spi(sysbus, cfg, phat):
    sd = machine.SDCard(
        slot=cfg["config"].get("slot", 2),
        sck=cfg["GPIO"]["sck"],
        mosi=cfg["GPIO"]["cmd"],
        miso=cfg["GPIO"]["data"][0],
        cs=cfg["GPIO"]["data"][3],
        freq=cfg["config"].get("freq", 20000000),
    )
    os.mount(sd, phat)
    sysbus.register_service("sd_raw", sd)


def _init_sd_sdio(sysbus, cfg, phat):
    sd = machine.SDCard(
        slot=cfg["config"]["slot"],
        width=cfg["config"]["width"],
        sck=cfg["GPIO"]["sck"],
        cmd=cfg["GPIO"]["cmd"],
        data=cfg["GPIO"]["data"],
        freq=cfg["config"]["freq"],
    )
    os.mount(sd, phat)
    sysbus.register_service("sd_raw", sd)


def _exists(path):
    try:
        os.stat(path)
    except OSError:
        return False
    return True


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("SDcard") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    gpio = cfg.get("GPIO", {})
    if gpio.get("sck") is not None:
        result[gpio["sck"]] = "sd_sck"
    if gpio.get("cmd") is not None:
        result[gpio["cmd"]] = "sd_cmd"
    for i, d in enumerate(gpio.get("data", [])):
        if d is not None and d >= 0:
            result[d] = "sd_d{}".format(i)
    return result
