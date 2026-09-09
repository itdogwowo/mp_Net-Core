"""
lvgl_task.py — LVGL UI 任務（任務模式）

把 ui/lvgl 包成 TaskManager 可調度的 Task。
初始化與主迴圈解耦（對齊 board._setup / _loop_once）：
  on_start → board._setup()   一次性初始化(once-only,soft-reboot 安全)
  loop     → board._loop_once()  單幀 app.step()
  on_stop  → 保留 LVGL 狀態(soft-reboot 無法清 C 層,保留 bus reuse)

輸入透過 hw_manager 快照（HwSampleTask 統一採樣），本 task 不碰硬體 →
跨核心安全：採樣可跑在 Core0，LVGL 跑在 Core1，透過 bus.shared 共享。

LVGL 獨佔 LCD（原與 jpeg_player 互斥二選一，播放器已移除）。
"""

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


class LvglTask(Task):
    log_schema = ["lvgl_frame"]
    hw = ("lcd",)   # LVGL 獨佔 LCD/SPI1，只能跑 core0（TaskManager 會擋 core1）

    def __init__(self, name, ctx):
        super().__init__(name, ctx)

    def on_start(self):
        super().on_start()
        # 沒 LCD 直接跳過(不拖垮整個 TaskManager)
        if not bus.has_lcd():
            get_log().info("⏭ [LvglTask] skipped — no LCD on bus")
            self.running = False
            return
        from ui.lvgl import board
        board._setup()          # once-only 初始化(platform+字型+頁面+輸入)
        get_log().info("🖼 [LvglTask] UI online")

    def loop(self):
        if not self.running:
            return
        # 直接呼叫 app.step()(不經 board._loop_once,避免其 _sleep 阻塞 runner_loop)。
        # TaskManager 的 runner_loop 每輪結尾已 sleep_ms(0) 讓出,不需要 task 自己 sleep。
        from ui.lvgl import app, board
        try:
            app.step()
        except Exception as e:
            try:
                from lib.sys.log_service import get_log
                get_log().error("[LvglTask] loop err: {}".format(e))
            except Exception:
                print("[LvglTask] loop err:", e)
        self.touch += 1

    def on_stop(self):
        super().on_stop()
        # 不 deinit LVGL：soft-reboot 無法移除 C 層狀態，保留 bus reuse。
        # 重啟 task 時 _setup 的 _started 守護會跳過重複初始化。
        # 清 _ui_active:讓面板 ControlPanelTask 回到按鈕模式(廣播 vbtn)。
        try:
            bus.shared["_ui_active"] = False
        except Exception:
            pass
        get_log().info("[LvglTask] stopped (LVGL state retained)")
