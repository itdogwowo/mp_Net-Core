"""
i80_drv.py — 8080 並口總線管理

設定來源: bus.shared["I80"]  ({enable, data, wr, cs, freq})
產物:    bus.register_service("i80_bus", i80)
         bus.register_service("lcd_bus", [i80])
"""
from lib.sys.sys_bus import bus


def init_i80(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("I80") or {}
    if not cfg.get("enable"):
        return None

    import lcd_bus
    data = tuple(cfg["data"])
    wr = cfg["wr"]
    cs = cfg["cs"]
    freq = cfg["freq"]

    print("[i80_drv] data pins:", data)
    print("[i80_drv] wr={} cs={} freq={}".format(wr, cs, freq))

    # 建立 I80 Bus (lcd_bus 內部已處理 cleanup)
    i80 = lcd_bus.I80Bus(data=data, wr=wr, cs=cs, freq=freq)

    # 統一 lcd_bus 池
    lst = sysbus.get_service("lcd_bus") or []
    lst.append(i80)
    sysbus.register_service("lcd_bus", lst)
    sysbus.register_service("i80_bus", i80)
    return i80


def gpios(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("I80") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for i, d in enumerate(cfg.get("data", [])):
        result[d] = "i80_d{}".format(i)
    if cfg.get("wr") is not None:
        result[cfg["wr"]] = "i80_wr"
    if cfg.get("cs", -1) >= 0:
        result[cfg["cs"]] = "i80_cs"
    return result
