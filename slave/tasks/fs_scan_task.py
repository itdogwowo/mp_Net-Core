from lib.task import Task
from lib.sys_bus import bus
from lib.fs_manager import fs


class FsScanTask(Task):
    def on_start(self):
        super().on_start()
        self._phase = 0  # 0=idle, 1=collect, 2=hash, 3=done

    def loop(self):
        if not self.running:
            return

        if self._phase == 0:
            if not bus.shared.get("fs_scan_requested"):
                self._phase = 3
                return
            fs.scan_init()
            self._phase = 1
            return

        if self._phase == 1:
            bus.shared["fs_scan_requested"] = False
            self._phase = 2
            return

        if self._phase == 2:
            fs.scan_step()
            if bus.shared.get("fs_scan_done"):
                self._phase = 3
            return
