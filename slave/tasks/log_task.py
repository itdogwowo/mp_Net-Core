import time
from lib.task import Task
from lib.log_service import get_log


class LogTask(Task):
    def on_start(self):
        super().on_start()
        from lib.sys_bus import bus
        bus.shared["log_task_ready"] = True
        self._last_scan_report_ms = 0
        self._last_scan_progress = -1
        self._last_scan_current = ""

    def loop(self):
        if not self.running:
            return
        log = get_log()
        log.flush()
        self._report_scan_progress(log)

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
        if time.ticks_diff(now, self._last_scan_report_ms) < 1000:
            return
        self._last_scan_report_ms = now

        pct = progress * 100 // total if total else 0
        if current:
            # show basename only, not full path
            fname = current.rsplit("/", 1)[-1]
            log.info("FS Scan: {}/{} ({}%) | {}".format(progress, total, pct, fname))
        else:
            log.info("FS Scan: {}/{} files ({}%)".format(progress, total, pct))
