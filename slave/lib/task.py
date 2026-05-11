import time

class Task:
    log_schema = []

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

        self._fcache = {}
        self._fcache_ts = 0

    def on_start(self):
        self.running = True

    def loop(self):
        pass

    def on_stop(self):
        self.running = False

    def perf_snapshot(self):
        d = dict(self.perf)
        d["loop_avg_us"] = self.perf["loop_total_us"] // max(self.perf["loop_count"], 1)
        return d

    def perf_reset(self):
        self.perf["loop_count"] = 0
        self.perf["loop_total_us"] = 0
        self.perf["loop_max_us"] = 0

    def fcache_get(self, key, default=None, ttl_ms=500):
        from lib.sys_bus import bus
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
