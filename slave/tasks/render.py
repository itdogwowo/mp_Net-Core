import time
from lib.task import Task
from lib.sys_bus import bus
from lib.fs_manager import fs

class RenderTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.st_LED = ctx['st_LED']
        self.fps = 40 # Default, will update from bus
        self.hub = None
        
        # State
        self._render_count = 0
        self.interval_us = 0
        self.next_tick_us = 0
        self.current_big_buffer = None
        self.buff_offset = 0
        self.frame_size = 0
        self.raw_view = None

    def on_start(self):
        super().on_start()
        
        # Wait for hub
        while self.hub is None:
            self.hub = bus.get_service("pixel_stream")
            if self.hub is None:
                time.sleep_ms(100)
        
        # Register FPS provider
        bus.register_provider("render_fps", lambda: self._render_count)
        
        bus_sys = bus.shared["System"]
        self.fps = bus_sys.get("local_fps", 40)
        self.interval_us = (1000 // self.fps) * 1000
        self.next_tick_us = time.ticks_us()
        
        # Pre-cache
        self.frame_size = len(self.st_LED.big_buffer)
        self.raw_view = self.st_LED.big_buffer
        self.current_big_buffer = None
        self.buff_offset = 0
        
        print(f"🔥 [RenderTask] Engine Online | {self.fps} FPS")

    def loop(self):
        if not self.running: return

        # 0. System Task: Flash Scan (Priority)
        if bus.shared.get("fs_scan_requested"):
            fs.perform_scan()
            bus.shared["fs_scan_requested"] = False
            self.next_tick_us = time.ticks_us() # Reset timing
            
        # Stop Mode
        if not bus.shared.get("is_streaming"):
            if bus.shared.get("is_ready") == False:
                # Clear buffer
                # Optimization: Create bytearray once? Or just fill with 0
                # Using existing buffer clear might be faster
                for i in range(len(self.st_LED.big_buffer)):
                    self.st_LED.big_buffer[i] = 0
                self.st_LED.show_all()
            
            # Non-blocking throttle for Stop mode
            if time.ticks_diff(time.ticks_us(), self.next_tick_us) < 0:
                return
            
            self.next_tick_us = time.ticks_add(time.ticks_us(), 100000) # 100ms
            self._render_count = 0
            return

        # Pause Mode
        if bus.shared.get("is_paused"):
            if time.ticks_diff(time.ticks_us(), self.next_tick_us) < 0:
                return
            self.next_tick_us = time.ticks_add(time.ticks_us(), 50000) # 50ms
            self._render_count = 0
            return

        # Play Mode
        now = time.ticks_us()
        # Initialize next_tick_us if it's way off (e.g. after mode switch)
        if time.ticks_diff(now, self.next_tick_us) > 200000: # 200ms lag
             self.next_tick_us = now
        
        if time.ticks_diff(now, self.next_tick_us) >= 0:
            # Check buffer availability
            if self.current_big_buffer is None or self.buff_offset + self.frame_size > len(self.current_big_buffer):
                self.current_big_buffer = self.hub.get_read_view()
                self.buff_offset = 0
                
            if self.current_big_buffer:
                # Fast copy
                # Use memoryview assignment for speed
                end = self.buff_offset + self.frame_size
                self.raw_view[:] = self.current_big_buffer[self.buff_offset : end]
                
                self.st_LED.show_all()
                self._render_count += 1
                self.buff_offset += self.frame_size
            
            self.next_tick_us += self.interval_us
        else:
            # Yield (no sleep, just return to let TaskManager run other tasks)
            return

    def on_stop(self):
        super().on_stop()
        # Turn off LEDs? Or keep state?
        # Usually when stopping render engine we might want to clear LEDs
        # But for hot-swapping, maybe we want to keep last state?
        # Let's keep state for now.
        print("RenderTask Stopped")
