"""
hw_sample_task.py — 統一硬體輸入採樣任務

職責：每個 loop 週期採樣所有輸入硬體（Encoder + IN Pin）當前值，
     快照進 bus.shared["_hw_inputs"]。

設計動機：
  原本各消費者（control_panel、board._make_inputs、action_task）各自
  在自己的 loop 裡呼叫 enc.value() / pin.value()，散落各處、各自維護
  last 狀態算 delta。現在集中由本任務採樣一次，其他 Task / Core 直接
  讀 bus 快照（HW.get_input），不碰硬體。

  → 跨核心安全：採樣可跑在 Core0，消費者（如 LVGL）跑在 Core1，
    透過 bus.shared 共享，輸入硬體只需被一個核心實際讀取。

VBTN 不在此採樣（已有 bus.shared["_vbtn"] 快照機制，維持現狀）。
"""

from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log
from lib.sys.hw_manager import sample_inputs


class HwSampleTask(Task):
    log_schema = ["hw_enc0", "hw_pin_btn"]

    def __init__(self, name, ctx):
        super().__init__(name, ctx)

    def on_start(self):
        super().on_start()
        # 首次採樣：建立 encoder 基準值（_enc_last），delta 歸零
        sample_inputs()
        get_log().info("📡 [HwSample] input sampling online")

    def loop(self):
        if not self.running:
            return
        sample_inputs()
        self.touch += 1
