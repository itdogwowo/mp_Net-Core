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

import time

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log


class LvglTask(Task):
    log_schema = ["lvgl_frame"]
    hw = ("lcd",)   # LVGL 獨佔 LCD/SPI1，只能跑 core0（TaskManager 會擋 core1）

    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._fps_n = 0
        self._fps_t0 = time.ticks_ms()
        self._fps_us = 0
        self._fps_max = 0
        # 幀率由全域 System.frame_interval_ms 決定（預設 20ms = 50fps）。
        self._frame_us = 20000
        self._next_us = 0

    def on_start(self):
        super().on_start()
        # 沒 LCD 直接跳過(不拖垮整個 TaskManager)
        if not bus.has_lcd():
            get_log().info("⏭ [LvglTask] skipped — no LCD on bus")
            self.running = False
            return
        from ui.lvgl import board
        board._setup()          # once-only 初始化(platform+字型+頁面+輸入)
        # 全域幀率：System.frame_interval_ms；不再用代碼硬寫 30fps。
        try:
            iv = int((bus.shared.get("System") or {}).get("frame_interval_ms", 20) or 20)
        except Exception:
            iv = 20
        self._frame_us = (iv if iv > 0 else 20) * 1000
        self._next_us = time.ticks_us()
        get_log().info("🖼 [LvglTask] UI online | {}ms/幀".format(
            self._frame_us // 1000))

    def loop(self):
        if not self.running:
            return
        now = time.ticks_us()
        if time.ticks_diff(now, self._next_us) < 0:
            return
        self._next_us = time.ticks_add(self._next_us, self._frame_us)
        # 直接呼叫 app.step()(不經 board._loop_once,避免其 _sleep 阻塞 runner_loop)。
        # TaskManager 的 runner_loop 每輪結尾已 sleep_ms(0) 讓出,不需要 task 自己 sleep。
        from ui.lvgl import app, board
        t0 = time.ticks_us()
        try:
            app.step()
        except Exception as e:
            try:
                get_log().error("[LvglTask] loop err: {}".format(e))
            except Exception:
                print("[LvglTask] loop err:", e)
        dt = time.ticks_diff(time.ticks_us(), t0)
        self._fps_n += 1
        self._fps_us += dt
        if dt > self._fps_max:
            self._fps_max = dt
        now = time.ticks_ms()
        if time.ticks_diff(now, self._fps_t0) >= 1000:
            el = time.ticks_diff(now, self._fps_t0)
            n = self._fps_n
            get_log().info("[FPS] lvgl {:.1f}/s step avg {}us max {}us".format(
                n * 1000.0 / el, self._fps_us // max(n, 1), self._fps_max))
            self._fps_t0 = now
            self._fps_n = 0
            self._fps_us = 0
            self._fps_max = 0
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
        # 標記檔**不在這裡清**：軟重開機的觸發時機不可控（Ctrl-C / mpremote /
        # watchdog），靠「有沒有正常收尾」會漏。lvgl_init 的復原一律自己探測
        # LVGL 有沒有殘留，那個才是判準。
        get_log().info("[LvglTask] stopped (LVGL state retained)")
