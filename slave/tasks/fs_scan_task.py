from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log
from lib.sys.fs_manager import fs


class FsScanTask(Task):
    log_schema = ["fs_scan_total", "fs_scan_progress", "fs_scan_done"]
    def on_start(self):
        super().on_start()
        self._phase = 0  # 0=idle, 1=collect, 2=hash, 3=finalize, 4=shutdown

    def loop(self):
        if not self.running:
            return

        if self._phase == 0:
            if not self.fcache_get("fs_scan_requested"):
                self._shutdown()
                return
            fs.scan_init()
            self._phase = 1
            return

        if self._phase == 1:
            self._phase = 2
            return

        if self._phase == 2:
            done = fs.scan_step()
            total = bus.shared.get("fs_scan_total", 0)
            progress = bus.shared.get("fs_scan_progress", 0)
            if done:
                get_log().info("FS Scan:完成 {} 個檔案".format(total))
                self._phase = 3
            elif progress > 0 and progress % max(1, total // 10) == 0:
                pct = progress * 100 // total if total > 0 else 0
                get_log().info("FS Scan: {}/{} ({})".format(progress, total, pct))
            return

        if self._phase == 3:
            fs.finalize_scan()
            self.fcache_flush()
            bus.shared["fs_scan_requested"] = False
            # 🔧 掃完先「叫佢自己關閉」(one-shot); 下次 0x200B 由 scan_all()
            #    重新武裝 affinity 再叫醒。
            self._shutdown()
            return

    def _shutdown(self):
        self._phase = 4
        tm = bus.get_service("task_manager")
        if tm:
            tm.set_affinity("fs_scan", (0, 0))

