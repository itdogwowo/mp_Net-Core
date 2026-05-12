import time
from lib.task import Task
from lib.sys_bus import bus
from lib.log_service import get_log

class RenderTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.st_LED = ctx['st_LED']
        self.fps = 40
        self.hub = None

        self._render_count = 0
        self.interval_us = 0
        self.next_tick_us = 0

    def on_start(self):
        super().on_start()

        while self.hub is None:
            self.hub = bus.get_service("pixel_stream")
            if self.hub is None:
                time.sleep_ms(5)

        bus.register_provider("render_fps", lambda: self._render_count)

        bus_sys = bus.shared["System"]
        self.fps = bus_sys.get("local_fps", 40)
        self.interval_us = (1000 // self.fps) * 1000
        self.next_tick_us = time.ticks_us()

        get_log().info("🔥 [RenderTask] Engine Online | {} FPS".format(self.fps))

    def loop(self):
        if not self.running: return

        is_streaming = self.fcache_get("is_streaming")
        if not is_streaming:
            is_ready = self.fcache_get("is_ready")
            if is_ready == False:
                for i in range(len(self.st_LED.big_buffer)):
                    self.st_LED.big_buffer[i] = 0
                self.st_LED.show_all()

            if time.ticks_diff(time.ticks_us(), self.next_tick_us) < 0:
                return

            self.next_tick_us = time.ticks_add(time.ticks_us(), 100000)
            self._render_count = 0
            return

        is_paused = self.fcache_get("is_paused")
        if is_paused:
            if time.ticks_diff(time.ticks_us(), self.next_tick_us) < 0:
                return
            self.next_tick_us = time.ticks_add(time.ticks_us(), 50000)
            self._render_count = 0
            return

        now = time.ticks_us()
        if time.ticks_diff(now, self.next_tick_us) > 200000:
             self.next_tick_us = now

        if time.ticks_diff(now, self.next_tick_us) >= 0:
            if self.hub.read_into(self.st_LED.big_buffer):
                self.st_LED.show_all()
                self._render_count += 1
                self.success += 1

            self.next_tick_us += self.interval_us
        else:
            return

    def on_stop(self):
        super().on_stop()
        get_log().info("RenderTask Stopped")
