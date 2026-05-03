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
        self._last_scan_progress = -1
        self._last_scan_current = ""

    def loop(self):
        if not self.running:
            return
        log = get_log()
        log.flush()
        self._report_scan_progress(log)
        self._report_fps(log)
        self._report_perf(log)

    def _report_scan_progress(self, log):
        from lib.sys_bus import bus
        total = bus.shared.get("fs_scan_total", 0)
        if total <= 0:
            return
        progress = bus.shared.get("fs_scan_progress", 0)
        current = bus.shared.get("fs_scan_current", "")
        log.state("fs_scan_total", total)
        log.state("fs_scan_progress", progress)
        log.state("fs_scan_current", current)

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
        from lib.sys_bus import bus
        if not bus.shared.get("fps_stats_enabled", True):
            return
        buf_svc = bus.get_service("dp_buffer")
        if not buf_svc:
            return
        fps_window = buf_svc.get("fps_window")
        fps_total = buf_svc.get("fps_total")
        if fps_window is None and fps_total is None:
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_fps_ms) < 1000:
            return
        self._last_fps_ms = now

        log.state("fps_window", fps_window)
        log.state("fps_total", fps_total)
        if fps_window is not None:
            log.immediate("FPS: {} / avg: {}".format(fps_window, fps_total if fps_total else "-"))

    def _report_perf(self, log):
        from lib.sys_bus import bus
        if not bus.shared.get("perf_enabled", True):
            return
        perf = bus.shared.get("perf")
        if not perf:
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_perf_ms) < 2000:
            return
        self._last_perf_ms = now

        for core in (0, 1):
            idle = perf.get(f"core{core}_idle_pct")
            tick = perf.get(f"core{core}_tick_us")
            hz = perf.get(f"core{core}_loops_per_sec")
            if idle is not None:
                log.state(f"core{core}_idle_pct", idle)
                log.state(f"core{core}_tick_us", tick)
                log.immediate("C{} idle={}% tick={}us ({}/s)".format(core, idle, tick, hz))
