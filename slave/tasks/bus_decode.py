from lib.task import Task
from lib.sys_bus import bus
import struct


class BusDecodeTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx["app"]
        self._buses = []
        self._parsers = {}

    def on_start(self):
        super().on_start()
        self._buses = []
        self._parsers = {}

    def loop(self):
        if not self.running:
            return
        if not self._buses:
            ctrl = bus.get_service("net_bus_ctrl")
            discv = bus.get_service("net_bus_discovery")
            if ctrl:
                self._buses.append(ctrl)
            if discv:
                self._buses.append(discv)
            if not self._buses:
                return

        buf_cfg = bus.shared.get("Buffer", {}) or {}
        max_slots = int(buf_cfg.get("decode_budget_slots", 32) or 0)
        if max_slots <= 0:
            max_slots = 1
        used = 0
        for b in self._buses:
            hub = getattr(b, "rx_hub", None)
            if hub is None:
                continue
            p = self._parsers.get(id(b))
            if p is None:
                p = self.app.create_parser()
                self._parsers[id(b)] = p
            ctx_extra = getattr(b, "_decode_ctx", None) or {}
            while True:
                if used >= max_slots:
                    return
                v = hub.get_read_view()
                if v is None:
                    break
                ln = struct.unpack_from("<H", v, 0)[0]
                if ln <= 0:
                    continue
                data = v[2:2 + ln]
                self.app.handle_stream(
                    p,
                    data,
                    transport_name=getattr(b, "label", "Bus"),
                    send_func=b.write,
                    **ctx_extra
                )
                used += 1

    def on_stop(self):
        super().on_stop()
        self._buses = []
        self._parsers = {}
