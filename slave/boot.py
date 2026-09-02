# boot.py
# 硬體初始化 — config.json (扁平 {enable, list}) → driver init_xxx(bus) → bus service
#
# 流程:
#   Phase 1: 各 driver gpios() 回報腳位 → bus.gpio_claim → validate (衝突檢查)
#   Phase 2: 線性呼叫 init_xxx(bus) 建立硬體 Object 並註冊到 bus
#
# 要停用某 driver：註解 Phase 1 與 Phase 2 對應兩行即可。

from lib.sys.ConfigManager import *
from lib.sys.sys_bus import bus
import ubinascii, machine

try:
    bus.slave_id = ubinascii.hexlify(machine.unique_id()).decode().upper()
except Exception:
    try:
        bus.slave_id = "".join("{:02X}".format(b) for b in machine.unique_id())
    except Exception:
        bus.slave_id = "UNKNOWN"


# ── WebREPL: 預設開啟 (與網絡狀態無關; 綁 0.0.0.0:8266 全介面, 不連線不消耗) ──
try:
    from lib.sys import webrepl_ctl
    webrepl_ctl.ensure()
except Exception as _we:
    print("[BOOT] WebREPL ensure error: {}".format(_we))


# ── 調試等級: 由 config.json 的 System.debug_level 決定 (0/1/2) ──
#   ConfigManager 已在 import 時載入 config 到 bus.shared, 這裡套用到 dprint。
try:
    from lib.sys.dispatch import Dispatcher
    Dispatcher.configure(bus.shared.get("System", {}).get("debug_level", 1))
except Exception:
    pass


from driver.spi_drv      import init_spi,      gpios as g_spi
from driver.pin_drv      import init_pin,      gpios as g_pin
from driver.i2c_drv      import init_i2c,      gpios as g_i2c
from driver.uart_drv     import init_uart,     gpios as g_uart
from driver.pwm_drv      import init_pwm,      gpios as g_pwm
from driver.i2s_drv      import init_i2s,      gpios as g_i2s
from driver.pcm5102_drv  import init_pcm5102,  gpios as g_pcm5102
from driver.sd_drv       import init_sd,       gpios as g_sd
from driver.tft_drv      import init_tft,      gpios as g_tft
from driver.enc_drv      import init_enc,      gpios as g_enc
from driver.network_drv  import init_network
from driver.ws2812_drv   import init_ws2812
from driver.apa102_drv   import init_apa102
from driver.pca9685_drv  import init_pca9685
from driver.motor_drv    import init_motor
from driver.pixel_drv    import init_pixel
from lib.sys.watchdog    import gpios as g_wdt


# ══════════════════════════════════════════════════════
# Phase 1: GPIO claim + 衝突檢查
#   (name, gpios_fn) — driver 透過 gpios(bus) 回報自己用的腳位
# ══════════════════════════════════════════════════════
DRIVERS = [
    ("spi",  g_spi),
    ("pin",  g_pin),
    ("i2c",  g_i2c),
    ("uart", g_uart),
    ("pwm",  g_pwm),
    ("i2s",  g_i2s),
    ("pcm5102",  g_pcm5102),
    ("sd",   g_sd),
    ("tft",  g_tft),
    ("enc",  g_enc),
    ("wdt",  g_wdt),
]

for name, gpios_fn in DRIVERS:
    for gpio, label in gpios_fn(bus).items():
        bus.gpio_claim(gpio, name, label)

if not bus.gpio_validate():
    raise SystemExit("[BOOT] GPIO 衝突 — 修正 config.json 後重開機")
bus.gpio_dump()


# ══════════════════════════════════════════════════════
# Phase 2: 線性硬體初始化
#   順序: 匯流排 (spi/pin/i2c/uart) → sd → tft → network
#   driver 內部會檢查 bus.shared["XXX"]["enable"]，未啟用即跳過
#
#   每個 driver 獨立 try/except：單一失敗不會中斷後續 driver 與 fs 服務。
#   失敗訊息用 immediate() (boot 期間 log_task_ready 未設 → 直接 print)，
#   結尾再印一份 ok/FAIL 摘要 + flush() 排出 driver 內部累積的 info/warn。
#
#   開關 System.boot_strict:
#     0 (預設) = best-effort，繼續開機
#     1        = 任一 driver 失敗即 raise 中止整個 boot
# ══════════════════════════════════════════════════════
from lib.sys.log_service import get_log

_strict = bool(bus.shared.get("System", {}).get("boot_strict", 0))
_boot_status = []   # [(name, "ok"/"FAIL", err_str)]


def _init(name, fn):
    try:
        fn(bus)
        _boot_status.append((name, "ok", ""))
    except Exception as e:
        get_log().immediate("[boot] {} FAIL: {}".format(name, e))
        _boot_status.append((name, "FAIL", str(e)))
        if _strict:
            raise


_init("spi",     init_spi)
_init("pin",     init_pin)
_init("i2c",     init_i2c)
_init("uart",    init_uart)
# _init("pwm",   init_pwm)
_init("i2s",     init_i2s)
_init("pcm5102", init_pcm5102)
_init("sd",      init_sd)
_init("tft",     init_tft)
_init("enc",     init_enc)
_init("network", init_network)
_init("ws2812",  init_ws2812)
_init("apa102",  init_apa102)
_init("pca9685", init_pca9685)
_init("motor",   init_motor)
_init("pixel",   init_pixel)

_oks   = [n for n, s, _ in _boot_status if s == "ok"]
_fails = [(n, e) for n, s, e in _boot_status if s == "FAIL"]
print("[BOOT] ok  : {}".format(", ".join(_oks) if _oks else "(none)"))
if _fails:
    print("[BOOT] FAIL: {}".format(
        ", ".join("{} ({})".format(n, e) for n, e in _fails)))
get_log().flush()   # 排出 driver 內部 get_log().info/warn/error 累積的訊息


# ── 統一資料層 (fs)：永遠確保 /sd 存在 + 註冊成 service "data" ──
import os as _os
try:
    _os.stat("/sd")
except Exception:
    try:
        _os.mkdir("/sd")
    except Exception:
        pass
from lib.sys.fs_manager import fs
bus.register_service("data", fs)
