import time
from lib.task import Task
from lib.sys_bus import bus
from lib.buffer_hub import viper_copy, viper_copy_full

class RenderTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.st_LED = ctx['st_LED']
        self.fps = 40
        self.hub = None

        self._render_count = 0
        self.interval_us = 0
        self.next_tick_us = 0
        self.current_big_buffer = None
        self.buff_offset = 0
        self.frame_size = 0
        self.raw_view = None
        self._zero_buf = None

    def on_start(self):
        super().on_start()

        while self.hub is None:
            self.hub = bus.get_service("pixel_stream")
            if self.hub is None:
                time.sleep_ms(100)

        bus.register_provider("render_fps", lambda: self._render_count)

        bus_sys = bus.shared["System"]
        self.fps = bus_sys.get("local_fps", 40)
        self.interval_us = (1000 // self.fps) * 1000
        self.next_tick_us = time.ticks_us()

        self.frame_size = len(self.st_LED.big_buffer)
        self.raw_view = self.st_LED.big_buffer
        self.current_big_buffer = None
        self.buff_offset = 0
        self._zero_buf = bytearray(self.frame_size)

        print(f"🔥 [RenderTask] Engine Online | {self.fps} FPS")

    def loop(self):
        if not self.running:
            return

        if not bus.shared.get("is_streaming"):
            if bus.shared.get("is_ready") == False:
                self.raw_view[:] = self._zero_buf
                self.st_LED.show_all()

            if time.ticks_diff(time.ticks_us(), self.next_tick_us) < 0:
                return

            self.next_tick_us = time.ticks_add(time.ticks_us(), 100000)
            self._render_count = 0
            return

        if bus.shared.get("is_paused"):
            if time.ticks_diff(time.ticks_us(), self.next_tick_us) < 0:
                return
            self.next_tick_us = time.ticks_add(time.ticks_us(), 50000)
            self._render_count = 0
            return

        now = time.ticks_us()
        if time.ticks_diff(now, self.next_tick_us) > 200000:
             self.next_tick_us = now

        if time.ticks_diff(now, self.next_tick_us) >= 0:
            if self.current_big_buffer is None or self.buff_offset + self.frame_size > len(self.current_big_buffer):
                self.current_big_buffer = self.hub.get_read_view()
                self.buff_offset = 0

            if self.current_big_buffer:
                viper_copy(self.raw_view, self.current_big_buffer, self.buff_offset, self.frame_size)
                self.st_LED.show_all()
                self._render_count += 1
                self.buff_offset += self.frame_size

            self.next_tick_us += self.interval_us
        else:
            return

    def on_stop(self):
        super().on_stop()
        print("RenderTask Stopped")
