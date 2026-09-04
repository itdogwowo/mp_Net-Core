# ui/lvgl/board.py — 板上對接層（slave new bus 系統）
#
# 適配兩種模式(初始化與主迴圈解耦):
#   _setup()      初始化(platform+字型+頁面+輸入),once-only(_started 守護)
#   _loop_once()  單幀 app.step(),供調度器逐幀呼叫
#   run()         _setup + while _loop_once(手動快速啟動 / 核心模式輕量入口)
#
#   任務模式: LvglTask.on_start=_setup, loop=_loop_once (TaskManager 調度)
#   核心模式: Core_LVGL 自跑 run() 或 _setup+while _loop_once
#
# 硬體全部透過 bus 系統取得,本檔不自建任何硬體:
#   顯示   bus.get_service("lcd")   → lvgl_init.get_platform()(一次初始化 + reuse)
#   輸入   hw_manager 快照(HwSampleTask 統一採樣)→ bus.shared["_hw_inputs"]
#
# 啟動唯一前置條件:bus.has_lcd()(boot.py 的 init_tft 成功)。
# LVGL 獨佔 LCD（原與 jpeg player 共用互斥，播放器已移除）。
import sys
from lib.sys.sys_bus import bus
from ui.lvgl import app
from ui.lvgl import ui_common
from ui.lvgl import lvgl_init

# 資源在 ui/lvgl/src,加進 import 路徑(ui_common 的 from lv_icons/lv_ui_fx 由此找到)
_SRC = "/ui/lvgl/src"
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _make_inputs():
    """從 hw_manager 快照讀輸入(bus.shared['_hw_inputs'])。

    delta / 邊緣計算已由 HwSampleTask 統一做完,這裡只取快照值。
    → LVGL Core 不碰硬體,真正適配核心模式(採樣可跑在另一個 Core)。

    按鈕 label 預設 encC / btn,可在 config PIN 段改名。
    active-low;confirm/exit 為去抖後按壓邊緣(讀取即清除,按住不重複觸發)。"""
    from lib.sys.hw_manager import consume_input

    print("[board] inputs: via hw_manager snapshot (encC/btn)")
    return _make_snapshot_inputs(consume_input)


def _make_snapshot_inputs(consume_input):
    """快照後端:三個 callable 從 bus.shared['_hw_inputs'] 消費式取值。"""
    def enc_delta():
        # 累加 delta 讀取即清除(唯一消費者,快速轉動不掉格)
        return consume_input("enc", idx=0) or 0

    def confirm():
        # 去抖後按壓邊緣(active-low 0=按下):邊緣即清,按住不會每幀重複觸發
        return consume_input("pin", key="encC") != 0

    def exit_pressed():
        return consume_input("pin", key="btn") != 0

    return enc_delta, confirm, exit_pressed


def run():
    """啟動 LVGL UI 主迴圈。"""

    # ── LCD 存在閘門:沒有 LCD 就根本不能跑 LVGL,直接返回 ──
    if not bus.has_lcd():
        print("[board] no LCD on bus, LVGL UI not started")
        return

    try:
        from lib.sys.log_service import get_log
    except Exception:
        get_log = None
    if get_log:
        get_log().info("[board] starting LVGL UI")

    # _setup + 自跑主迴圈(快速啟動 / 手動入口)
    _setup()
    try:
        while True:
            _loop_once()
    except KeyboardInterrupt:
        print("[board] stopped")


# ── once-only 守護:同一 boot 週期內 _setup 只跑一次(soft-reboot 安全)──
_started = False


def _setup():
    """初始化(platform + 字型 + 頁面 + 輸入)。冪等:_started 後重入直接返回。
    任務模式(LvglTask.on_start)與核心模式(Core_LVGL)共用此函式。"""
    global _started
    if _started:
        return
    _started = True

    # LVGL display:一次初始化 + bus reuse(對齊 i80_drv/tft_drv)
    plat = lvgl_init.get_platform()
    ui_common.init(plat)        # 注入 W/H
    ui_common.init_fonts()

    # 註冊所有頁面 → 預建所有 screen
    try:
        import ui.lvgl.page  # noqa: F401
    except ImportError as e:
        print("[board] page import fail:", e)
    app.build_all()

    # 輸入:從 hw_manager 快照讀(由 HwSampleTask 統一採樣)
    enc_delta, confirm, exit_pressed = _make_inputs()

    app.init({
        "tick": plat.tick,
        "take": plat.take,
        "show": plat.show,
        "enc_delta": enc_delta,
        "confirm": confirm,
        "exit": exit_pressed,
    })
    # 標記 LVGL 在跑:面板 ControlPanelTask 據此切換兩模式分層
    # (LV 模式 → 不發 vbtn;按鈕模式 → 發 vbtn),lvgl_task.on_stop 清除。
    bus.shared["_ui_active"] = True
    app.go("launcher")
    print("[board] _setup done")


def _loop_once():
    """單幀處理 = app.step()。供任務模式(loop)/核心模式(主迴圈)逐幀呼叫。"""
    try:
        app.step()
    except Exception as e:
        try:
            from lib.sys.log_service import get_log
            get_log().error("[board] loop err: {}".format(e))
        except Exception:
            print("[board] loop err:", e)
    _sleep(5)


def _sleep(ms):
    try:
        import time
        time.sleep_ms(ms)
    except Exception:
        pass
