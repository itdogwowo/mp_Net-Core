"""
husb238_drv.py — HUSB238 USB PD Sink 控制器 (走 I2C)

設定來源: bus.shared["HUSB238"]  ({enable, i2c, addr, default_voltage})
產物:    bus.register_service("husb238", dev)
         bus.shared["pd"] = {...}  (PD 狀態摘要，供其他任務查詢)

addr 為選用，HUSB238 固定 0x08；啟用時會掃描確認。
"""
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_husb238(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("HUSB238") or {}
    if not cfg.get("enable"):
        return None

    i2c_list = sysbus.get_service("i2c_list") or []
    i2c_idx = cfg.get("GPIO", {}).get("i2c", 0)
    if i2c_idx < 0 or i2c_idx >= len(i2c_list):
        get_log().error("husb238: i2c index {} not available".format(i2c_idx))
        return None
    i2c = i2c_list[i2c_idx]

    # HUSB238 位址固定 0x08，但允許設定覆寫
    addr = cfg.get("addr", 0x08)
    if isinstance(addr, str):
        addr = int(addr, 16)

    try:
        found = i2c.scan()
    except Exception as e:
        get_log().error("husb238: i2c scan error {}".format(e))
        return None

    if addr not in found:
        get_log().error("husb238: addr 0x{:02X} not found in scan {}".format(
            addr, [hex(a) for a in found]))
        return None

    from lib.hw.husb238 import HUSB238
    dev = HUSB238(i2c, addr)

    # 若設定預設電壓，開機即嘗試協商
    dv = cfg.get("default_voltage")
    if dv:
        try:
            ok = dev.request_voltage(int(dv))
            get_log().info("husb238: request {}V {}".format(
                dv, "OK" if ok else "FAIL ({})".format(dev.get_response_str())))
        except Exception as e:
            get_log().error("husb238: default_voltage error {}".format(e))

    sysbus.register_service("husb238", dev)
    # 同步一份狀態摘要到 bus.shared["pd"]
    _sync_pd_status(sysbus, dev)
    get_log().info("husb238: OK (addr=0x{:02X})".format(addr))
    return dev


def _sync_pd_status(sysbus, dev):
    """把常用 PD 狀態寫進 bus.shared['pd']，供其他任務快速查詢"""
    st = dev.status()
    sysbus.shared["pd"] = st
    return st


def refresh():
    """便利函數: 重新讀取 PD 狀態 → 更新 bus.shared['pd']，並回傳"""
    dev = bus.get_service("husb238")
    if dev is None:
        return None
    return _sync_pd_status(bus, dev)


def request_voltage(voltage):
    """便利函數: 從 bus 取得 husb238 並請求電壓。回傳 True/False。"""
    dev = bus.get_service("husb238")
    if dev is None:
        return False
    ok = dev.request_voltage(voltage)
    _sync_pd_status(bus, dev)
    return ok


def gpios(sysbus=None):
    # HUSB238 走 I2C，無獨立 GPIO
    return {}
