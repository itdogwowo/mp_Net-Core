from lib.task import Task
from lib.sys_bus import bus
from lib.fs_manager import fs


class FsScanTask(Task):
    def on_start(self):
        super().on_start()
        self._phase = 0  # 0=idle, 1=collect, 2=hash, 3=finalize, 4=shutdown

    def loop(self):
        if not self.running:
            return

        if self._phase == 0:
            if not bus.shared.get("fs_scan_requested"):
                self._shutdown()
                return
            fs.scan_init()
            self._phase = 1
            return

        if self._phase == 1:
            self._phase = 2
            return

        if self._phase == 2:
            fs.scan_step()
            if bus.shared.get("fs_scan_done"):
                self._phase = 3
            return

        if self._phase == 3:
            fs.finalize_scan()
            bus.shared["fs_scan_requested"] = False
            self._shutdown()
            return

    def _shutdown(self):
        self._phase = 4
        tm = bus.get_service("task_manager")
        if tm:
            tm.set_affinity("fs_scan", (0, 0))
