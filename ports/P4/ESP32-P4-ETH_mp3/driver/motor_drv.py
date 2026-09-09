"""
motor_drv.py — UART 電機控制器 (UART-412) 初始化

設定來源: bus.shared["uartMotor"]  ({enable, list})
         list item: {"GPIO": {"uart": <uart_list index>}, "address": ["51"],
                     "version": 1, "dStay": 2048}
         dStay = 中性值（對齊舊專案 PWM 的 dArc 概念）：停止/熄燈/暫停時
         電機回到的值（12-bit 0-4095，>>4 = 8-bit 速度 byte；2048 = 0x80 死區停）。
產物:    bus.register_service("motor_list", [UartMotor, ...])

UartMotor 具備 pixel 相容介面（pixel_type="uartMotor1"），可作為
PixelStreamer 的 controller：從 big_buffer 提取 W 通道 → 一次過組 UART frame 發射。
"""
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


def init_motor(sysbus=None):
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("uartMotor") or {}
    if not cfg.get("enable"):
        return []

    from lib.hw.uart_motor import UartMotor
    uart_list = sysbus.get_service("uart_list") or []
    motor_list = []
    for item in cfg.get("list", []):
        uart_idx = item.get("GPIO", {}).get("uart", 0)
        if uart_idx < 0 or uart_idx >= len(uart_list):
            get_log().error("uartMotor: uart index {} not found".format(uart_idx))
            continue
        addrs = item.get("address", [])
        if not addrs:
            get_log().error("uartMotor: 需要 address（我控制的全部台）")
            continue
        try:
            addresses = [int(a) for a in addrs]
            motor = UartMotor({
                "version": item.get("version", 1),
                "addresses": addresses,
                "uart": uart_list[uart_idx],
                "dStay": item.get("dStay", 2048),   # 預設 2048 = 0x80 死區停
            })
            motor_list.append(motor)
            get_log().info("uartMotor: {} (addr {}) on uart[{}]".format(
                item.get("version", 1), addresses, uart_idx))
        except Exception as e:
            get_log().error("uartMotor@{} error: {}".format(addrs, e))
    sysbus.register_service("motor_list", motor_list)
    get_log().info("uartMotor: {} device(s)".format(len(motor_list)))
    return motor_list


def gpios(sysbus=None):
    # motor 走 UART，無獨立 GPIO
    return {}
