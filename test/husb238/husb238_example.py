# husb238_example.py — HUSB238 USB PD Sink 控制器 使用範例
#
# 對照 Adafruit HUSB238 (I2C addr 0x08)。
# 此範例示範兩種用法:
#   (A) 純驅動 — 不依賴 sys_bus，直接給 I2C 物件即可 (便於移植/測試)
#   (B) 系統整合 — 走 driver/husb238_drv.py，與 config.json / bus 整合

import machine
import time

# ════════════════════════════════════════════════════════
# (A) 純驅動用法
# ════════════════════════════════════════════════════════
def demo_standalone():
    # 請依實際腳位修改;HUSB238 模組通常 SDA/SCL 已固定
    i2c = machine.I2C(0, scl=machine.Pin(9), sda=machine.Pin(8), freq=400000)
    print("I2C scan:", [hex(a) for a in i2c.scan()])  # 應見 0x08

    from lib.husb238 import HUSB238
    pd = HUSB238(i2c)          # addr 預設 0x08

    # ── 讀取 ──
    print("Attached:", pd.is_attached())
    print("CC direction:", "CC2" if pd.get_cc_direction() else "CC1")
    print("Available:", pd.available_voltages())
    print("Available caps:", pd.available_capabilities())  # [(5, 3.0), (9, 3.0), ...]
    print("Now V=", pd.voltage, " I=", pd.current, "A")

    # ── 請求 9V ──
    if pd.request_voltage(9):
        print("9V OK! now V=", pd.voltage, "I=", pd.current, "A")
    else:
        print("9V FAIL:", pd.get_response_str())

    # ── 一次讀全部狀態 (6 次 I2C 讀濃縮成 dict) ──
    print("Status:", pd.status())

    # ── Hard Reset 回 5V ──
    # pd.reset()
    # time.sleep_ms(500)


# ════════════════════════════════════════════════════════
# (B) 系統整合用法 (需先 boot.py 初始化 I2C)
#
#   config.json:
#     "I2C":     { "enable": 1, "list": [{ "id": 0, "freq": 400000,
#                                           "GPIO": {"scl":9,"sda":8} }] },
#     "HUSB238": { "enable": 1, "GPIO": {"i2c": 0},
#                  "default_voltage": 9 }   ← 可選, 開機即請求
#
#   boot.py 末尾加一行:
#     from driver.husb238_drv import init_husb238
#     init_husb238(bus)
# ════════════════════════════════════════════════════════
def demo_sysbus():
    from driver.husb238_drv import refresh, request_voltage
    from lib.sys.sys_bus import bus

    # 隨時查 PD 狀態 (從 bus 快取, 不碰硬體)
    st = bus.shared.get("pd")
    print("PD status:", st)

    # 動態切換電壓
    request_voltage(12)
    time.sleep_ms(20)
    print("After 12V:", refresh())   # 重讀 + 更新快照

    # 或直接拿驅動物件操作
    dev = bus.get_service("husb238")
    if dev:
        print("caps:", dev.available_capabilities())


if __name__ == "__main__":
    demo_standalone()
    # demo_sysbus()
