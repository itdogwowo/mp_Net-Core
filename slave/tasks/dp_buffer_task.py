import time

from lib.task import Task
from lib.sys_bus import bus
from lib.log_service import get_log
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service


class DpBufferTask(Task):
    def on_start(self):
        super().on_start()
        self._svc = ensure_dp_buffer_service(bus)
        self._disabled = False

        pf = str(self._svc.get("pixel_format") or "")
        if pf.startswith("RGB888"):
            tm = bus.get_service("task_manager")
            if tm:
                tm.set_affinity("dp_buffer", (0, 0))
            self._disabled = True
            return

    def loop(self):
        if not self.running:
            return

        self._svc = bus.get_service("dp_buffer") or self._svc
        if not self._svc or not self._svc.get("enable", True):
            return
        pf = str(self._svc.get("pixel_format") or "")
        if pf.startswith("RGB888"):
            if not self._disabled:
                tm = bus.get_service("task_manager")
                if tm:
                    tm.set_affinity("dp_buffer", (0, 0))
                self._disabled = True
            return

        jpeg_out = self._svc.get("jpeg_out")
        if jpeg_out is None:
            return

        out_hub = self._svc.get("out_hub")
        if out_hub is None:
            return

        buf = bytearray(HDR_OUT + int(self._svc.get("max_frame_bytes", 0) or 0))
        if not jpeg_out.read_into(buf):
            return

        wv = out_hub.get_write_view()
        if wv is None:
            return
        if int(len(wv)) < len(buf):
            self._svc["last_err"] = "out buffer too small"
            self._svc["last_ms"] = time.ticks_ms()
            return

        wv[:len(buf)] = buf
        out_hub.commit()

        self._svc["frames"] = int(self._svc.get("frames", 0) or 0) + 1
        self._svc["last_done"] = {"ms": time.ticks_ms()}
        self._svc["last_err"] = ""
        self._svc["last_ms"] = time.ticks_ms()

        self.success += 1
