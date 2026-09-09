import time
from lib.sys.task import Task
from lib.sys.sys_bus import bus


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
        self._src_ts = 0
        buf_cfg = bus.shared.get("Buffer") or {}
        self._max_slots = int(buf_cfg.get("decode_budget_slots", 32) or 0)
        if self._max_slots <= 0:
            self._max_slots = 1

    def _refresh_sources(self):
        sources = bus.get_service("bus_sources")
        if sources:
            self._buses = list(sources.list() or [])
            return
        self._buses = []
        ctrl = bus.get_service("net_bus_ctrl")
        discv = bus.get_service("net_bus_discovery")
        if ctrl:
            self._buses.append(ctrl)
        if discv:
            self._buses.append(discv)
        circuit_list = bus.get_service("circuit_bus_list")
        if circuit_list:
            for cb in circuit_list:
                self._buses.append(cb)

    def loop(self):
        if not self.running:
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._src_ts) > 100:
            self._src_ts = now
            self._refresh_sources()
        if not self._buses:
            return

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
                if used >= self._max_slots:
                    return
                # view 模式: 直接讀 slot 的 memoryview, 省掉 read_into 的 target[:] 複製。
                # handle_stream -> parser.feed 是「立即複製進 parser._buf」, 不持有 view,
                # 所以 finally 裡 release_read() 安全。
                view = hub.get_read_view()
                if view is None:
                    break
                try:
                    ln = view[0] | (view[1] << 8)
                    if ln > 0:
                        data = view[2:2 + ln]
                        self.app.handle_stream(
                            p,
                            data,
                            getattr(b, "label", "Bus"),
                            b.write,
                            ctx_extra,
                        )
                finally:
                    hub.release_read()
                self.success += 1
                used += 1

    def on_stop(self):
        super().on_stop()
        self._buses = []
        self._parsers = {}
