"""
gt1151q_drv.py — GT1151Q 觸控驅動 (走 I2C)

設定來源: bus.shared["GT1151Q"]  ({enable, i2c, addr, int_label})
產物:    bus.register_service("touch", tp)
"""
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_gt1151q(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("GT1151Q") or {}
    if not cfg.get("enable"):
        return None

    i2c_list = sysbus.get_service("i2c_list") or []
    i2c_idx = cfg.get("GPIO", {}).get("i2c", 0)
    if i2c_idx >= len(i2c_list):
        get_log().error("gt1151q: i2c index {} not available".format(i2c_idx))
        return None
    i2c = i2c_list[i2c_idx]

    # 從 pin_by_label 拿到 INT pin
    pin_by_label = sysbus.get_service("pin_by_label") or {}
    int_label = cfg.get("GPIO", {}).get("int", "touch_int")
    int_pin = pin_by_label.get(int_label)

    from lib.hw.gt1151q import GT1151Q

    addr = cfg.get("addr", 0x5D)
    if isinstance(addr, str):
        addr = int(addr, 16)
    found = i2c.scan()
    if addr not in found:
        get_log().info("gt1151q: addr 0x{:02X} not in scan {}, trying auto".format(addr, [hex(a) for a in found]))
        for try_addr in (0x5D, 0x14, 0x38, 0x5D >> 1):
            if try_addr in found:
                addr = try_addr
                break
        else:
            get_log().error("gt1151q: touch IC not found on i2c[{}]".format(i2c_idx))
            return None

    tp = GT1151Q(i2c, addr, int_pin)
    if not tp.init():
        return None

    sysbus.register_service("touch", tp)
    sysbus.shared["touch_vendor"] = "GT1151Q"
    get_log().info("gt1151q: OK (addr=0x{:02X})".format(addr))
    return tp


def gpios(sysbus=None):
    # GT1151Q 不佔主控 GPIO (INT 在 pin_drv 註冊)
    return {}


def read_touch():
    """便利函數: 從 bus 取得 touch 並讀取"""
    tp = bus.get_service("touch")
    if tp is None or not tp.available():
        return []
    return tp.read_points()
