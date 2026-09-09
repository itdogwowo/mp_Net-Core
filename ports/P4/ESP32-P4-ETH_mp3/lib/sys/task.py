import time
from lib.sys.log_service import _viper_write_i32
from lib.sys.sys_bus import bus

class Task:
    log_schema = []

    # 硬體歸屬(防呆): 子類可宣告 hw = ("lcd",) — 碰 lcd_bus/SPI 的 task
    # 只能跑 core0。TaskManager 的 register_task/set_affinity 會檢查,
    # 禁止把這類 task 排到 core1(否則 lcd_bus DMA queue 跨核會崩潰)。
    hw = ()

    def __init__(self, name, ctx):
        self.name = name
        self.ctx = ctx
        self.running = False
        self.run_once = False

        self.perf = {
            "loop_us": 0,
            "loop_avg_us": 0,
            "loop_max_us": 0,
            "loop_count": 0,
            "loop_total_us": 0,
        }

        self.touch = 0
        self.success = 0

        self._fcache = {}
        self._fcache_ts = 0

        self._lbuf = bytearray(20)
        self._lbuf_ex = None

    def on_start(self):
        self.running = True

    def loop(self):
        pass

    def on_stop(self):
        self.running = False

    def fcache_get(self, key, default=None, ttl_ms=500):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._fcache_ts) > ttl_ms:
            self._fcache.clear()
            self._fcache_ts = now
        if key not in self._fcache:
            self._fcache[key] = bus.shared.get(key, default)
        return self._fcache[key]

    def fcache_set(self, key, value):
        self._fcache[key] = value

    def fcache_flush(self):
        self._fcache.clear()
        self._fcache_ts = 0

    def _lwrite(self, v0, v1, v2, v3, v4):
        b = self._lbuf
        _viper_write_i32(b, 0, v0)
        _viper_write_i32(b, 4, v1)
        _viper_write_i32(b, 8, v2)
        _viper_write_i32(b, 12, v3)
        _viper_write_i32(b, 16, v4)

    def _lw_ex(self, idx, val):
        b = self._lbuf_ex
        if b:
            _viper_write_i32(b, idx * 4, val)
