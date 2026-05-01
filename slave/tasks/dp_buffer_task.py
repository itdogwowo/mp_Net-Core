import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, pack_out_header


class DpBufferTask(Task):
    def on_start(self):
        super().on_start()
        self._svc = ensure_dp_buffer_service(bus)

    def loop(self):
        if not self.running:
            return

        self._svc = bus.get_service("dp_buffer") or self._svc
        if not self._svc or not self._svc.get("enable", True):
            return

        pending = self._svc.get("pending")
        if not pending:
            return

        hub = self._svc.get("out_hub")
        if hub is None:
            return

        wv = hub.get_write_view()
        if wv is None:
            return

        payload_len = int(pending.get("payload_len", 0) or 0)
        if payload_len <= 0:
            self._svc["pending"] = None
            return

        payload = wv[HDR_OUT : HDR_OUT + payload_len]

        hook = self._svc.get("hook", None)
        if bool(self._svc.get("hook_enable", False)) and hook is not None:
            info = dict(pending)
            try:
                res = hook(payload, info)
                if res is not None:
                    if int(len(res)) != payload_len:
                        raise ValueError("hook payload length mismatch")
                    payload[:] = memoryview(res)[:payload_len]
            except Exception as e:
                self._svc["last_err"] = str(e)
                self._svc["last_ms"] = time.ticks_ms()
                self._svc["pending"] = None
                return

        pack_out_header(
            wv,
            payload_len,
            seq=int(pending.get("seq", 0) or 0),
            label_id=int(pending.get("label_id", 0) or 0),
            x=int(pending.get("x", 0) or 0),
            y=int(pending.get("y", 0) or 0),
            w=int(pending.get("w", 0) or 0),
            h=int(pending.get("h", 0) or 0),
            flags=3,
            fmt_code=int(pending.get("fmt_code", 0) or 0),
        )
        hub.commit()

        self._svc["pending"] = None
        self._svc["frames"] = int(self._svc.get("frames", 0) or 0) + 1
        self._svc["last_done"] = {"seq": int(pending.get("seq", 0) or 0), "ms": time.ticks_ms()}
        self._svc["last_err"] = ""
        self._svc["last_ms"] = time.ticks_ms()

