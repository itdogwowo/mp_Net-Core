# boot.py
# 硬體初始化 — config.json (扁平 {enable, list}) → driver init_xxx(bus) → bus service
#
# 流程:
#   Phase 0: LVGL soft-reboot 自我修復守門（見下）
#   Phase 1: 各 driver gpios() 回報腳位 → bus.gpio_claim → validate (衝突檢查)
#   Phase 2: 線性呼叫 init_xxx(bus) 建立硬體 Object 並註冊到 bus
#
# 要停用某 driver：註解 Phase 1 與 Phase 2 對應兩行即可。

import ubinascii, machine

# ══════════════════════════════════════════════════════════════════════════
#  Phase 0: LVGL soft-reboot 自我修復
# ══════════════════════════════════════════════════════════════════════════
#  軟重開機（Ctrl-D / mpremote 預設收尾）無法把 LVGL 的 C 層狀態清掉。
#  MicroPython 的 GC heap 整個重來，而 LVGL 的**每一筆配置**都在那個 heap 上
#  （ext_mod/lvgl/mem_core.c: lv_malloc_core → gc_alloc），
#  偏偏 binding 的 root pointer 是普通 C 全域、不會被 soft reset 清
#  （gen/lvgl_api_gen_mpy.py: `void *mp_lv_roots` + `static bool
#   mp_lv_roots_initialized`；實測位址 .bss 0x3fcaceec / 0x3fcacef4）。
#  ⇒ 軟重開機之後 LVGL 的每一棵樹都是**死指標**。
#
#  真機實測（軟重開機後攔在 REPL、main.py 未執行）：
#      display_get_default()          -> 上一個 session 的 display（★ 殘留判準）
#        └ 解析度                      -> 320 x 240   ← 還讀得到（舊 heap 沒被覆寫）
#      screen_active() 的子物件數       -> 14          ← 上一個 session 的 UI 樹
#      lv.obj() / lv.label() / lv.screen_load() / lv.deinit()
#                                     -> **直接打死板子**（USB-CDC 消失）
#      ★ `lv.is_initialized()` **不能**當判準：這個 binding 在 import lvgl 時
#        就會做掉 C 層初始化，乾淨開機時它也可能是 True（實際誤判過一次，
#        把自己的 UI 擋掉）。
#
#  所以「沿用同一個 display」是行不通的 —— 能沿用的不是堪用的物件，是一整棵
#  死指標樹。唯一保證可靠的路是**硬重置一次**：實測硬重置後
#  `display_get_default() is None`、`display_create()` 成功、UI 正常起來
#  （2026-10 真機：`(全新 init)` → `_setup done` → `UI online` → `Boot complete`）。
#
#  守門邏輯（靠一個標記檔）：
#      /lvgl_state  內容 owned=0|1,reset=0|1
#
#      1. 讀標記：若含 reset=1 → 上一輪已經為這件事重置過一次
#         → 清標記、往下走（**這一步必須在寫標記之前**，否則會蓋掉自己的訊號）
#      2. 寫下本輪的所有權標記 owned=1,reset=0
#      3. 探測 LVGL：`display_get_default() is not None` = 殘留
#         → 標記改成 reset=1 → machine.reset()
#         （用「探測」而不是靠「上一輪有沒有正常收尾」：軟重開機的觸發時機
#           不可控 —— Ctrl-C、mpremote、watchdog 都可能 —— 靠旗標會漏。）
#      4. UI 起來之後（board._setup 完成）→ 清掉標記
#
#  防無窮重置：reset=1 是「點數」。硬重置後重入會消耗掉它；降到 0 之後若又
#  偵測到殘留，會再重置一次（重新累積 1 點）—— 所以最壞情況是「每兩次開機
#  重置一次」，不會卡死，也不會無限快速重置。
#
#  代價：每次軟重開機多一次約 7 秒的硬重置 + USB 重新列舉。
#  真正的根治要改 C（把 mp_lv_roots_initialized 從 function-local static 換成
#  MP_STATE_VM，soft reset 才會重建 lv_global）→ 需重編韌體，
#  見 todo/08_lvgl_reinit.md §4。
# ══════════════════════════════════════════════════════════════════════════
_LVGL_MARK = "/lvgl_state"


def _mark_clear():
    import os
    try:
        os.remove(_LVGL_MARK)
    except Exception:
        pass


def _mark_write(txt):
    try:
        f = open(_LVGL_MARK, "w")
        f.write(txt)
        f.close()
    except Exception as e:
        print("[BOOT] LVGL guard: 標記寫入失敗:", e)


def _lvgl_residual():
    """C 層 LVGL 是否還留著上一個 session 的 display（＝軟重開機殘留）。"""
    try:
        import lvgl as lv
        return lv.display_get_default() is not None
    except Exception as e:
        print("[BOOT] LVGL guard: 探測失敗，跳過守門:", e)
        return None


def _lvgl_soft_reboot_guard():
    # 1) 先讀上一輪留下來的標記 —— **順序很重要**：
    #    必須在寫入本輪標記「之前」讀，否則會把自己剛寫的蓋掉（踩過一次）
    txt = ""
    try:
        with open(_LVGL_MARK) as f:
            txt = f.read()
    except Exception:
        pass

    # 2) 上一輪就是為了這件事重置回來的 → 清掉往下走（防無窮重置）
    if "reset=1" in txt:
        print("[BOOT] LVGL guard: 重置後重入，清標記繼續")
        _mark_clear()
        return

    # 3) 留下本輪的所有權標記
    _mark_write("owned=1,reset=0")

    # 4) 探測殘留 → 硬重置
    if _lvgl_residual():
        print("[BOOT] LVGL guard: 偵測到 soft-reboot 殘留的 LVGL 狀態 → hard reset")
        _mark_write("owned=1,reset=1")
        machine.reset()             # 不會回來


_lvgl_soft_reboot_guard()


from lib.sys.ConfigManager import *
from lib.sys.sys_bus import bus

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
