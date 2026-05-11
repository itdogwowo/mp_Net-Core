import time
from lib.task import Task
from lib.log_service import get_log


class LogTask(Task):
    def on_start(self):
        super().on_start()
        from lib.sys_bus import bus
        bus.shared["log_task_ready"] = True
        self._last_scan_ms = 0
        self._last_perf_ms = 0
        self._last_fps_ms = 0
        self._last_task_perf_ms = 0
        self._last_scan_progress = -1
        self._last_scan_current = ""
        self._task_names_cache_ms = 0
        self._task_names = []

    def loop(self):
        if not self.running:
            return
        log = get_log()
        log.flush()
        self._report_scan_progress(log)
        self._report_fps(log)
        self._report_perf(log)
        self._report_task_perf(log)

    def _report_scan_progress(self, log):
        total = log.get_metric("fs_scan_total")
        if total <= 0:
            return
        progress = log.get_metric("fs_scan_progress")
        from lib.sys_bus import bus
        current = bus.shared.get("fs_scan_current", "")

        if progress == self._last_scan_progress and current == self._last_scan_current:
            return
        self._last_scan_progress = progress
        self._last_scan_current = current

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_scan_ms) < 1000:
            return
        self._last_scan_ms = now

        pct = progress * 100 // total if total else 0
        if current:
            fname = current.rsplit("/", 1)[-1]
            log.info("FS Scan: {}/{} ({}%) | {}".format(progress, total, pct, fname))
        else:
            log.info("FS Scan: {}/{} files ({}%)".format(progress, total, pct))

    def _report_fps(self, log):
        if not self.fcache_get("fps_stats_enabled", True, ttl_ms=5000):
            return
        fps_window = log.get_metric("fps_window", -1)
        fps_total = log.get_metric("fps_total", -1)
        if fps_window < 0 and fps_total < 0:
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_fps_ms) < 1000:
            return
        self._last_fps_ms = now

        if fps_window >= 0:
            log.state("fps_window", fps_window)
        if fps_total >= 0:
            log.state("fps_total", fps_total)
        if fps_window >= 0:
            log.immediate("FPS: {} / avg: {}".format(fps_window, fps_total if fps_total >= 0 else "-"))

    def _report_perf(self, log):
        if not self.fcache_get("perf_enabled", True, ttl_ms=5000):
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_perf_ms) < 2000:
            return
        self._last_perf_ms = now

        for core in (0, 1):
            idle = log.get_metric("core{}_idle_pct".format(core), -1)
            if idle < 0:
                continue
            tick = log.get_metric("core{}_tick_us".format(core))
            hz = log.get_metric("core{}_loops_per_sec".format(core))
            log.state("core{}_idle_pct".format(core), idle)
            log.state("core{}_tick_us".format(core), tick)
            log.immediate("C{} idle={}% tick={}us ({}/s)".format(core, idle, tick, hz))

    def _report_task_perf(self, log):
        if not self.fcache_get("perf_enabled", True, ttl_ms=5000):
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_task_perf_ms) < 2000:
            return
        self._last_task_perf_ms = now

        if time.ticks_diff(now, self._task_names_cache_ms) > 5000:
            from lib.sys_bus import bus
            tm = bus.get_service("task_manager")
            if tm:
                self._task_names = tm.get_registered_task_names()
            self._task_names_cache_ms = now

        for name in self._task_names:
            avg = log.get_metric("t_{}_avg_us".format(name), -1)
            if avg < 0:
                continue
            max_us = log.get_metric("t_{}_max_us".format(name))
            count = log.get_metric("t_{}_count".format(name))
            log.state("t_{}_avg_us".format(name), avg)
            log.state("t_{}_max_us".format(name), max_us)
            log.state("t_{}_count".format(name), count)
            log.immediate("Task[{}] avg={}us max={}us n={}".format(name, avg, max_us, count))
