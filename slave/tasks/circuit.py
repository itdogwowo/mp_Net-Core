from lib.task import Task
from lib.sys_bus import bus
from lib.circuit_bus import CircuitBus
from lib.bus_sources import BusSources
from lib.log_service import get_log


class CircuitTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self._buses = []
        self._ctx_by_bus_id = {}

    def on_start(self):
        super().on_start()
        self._buses = []
        self._ctx_by_bus_id = {}

        selected = self._get_selected_sources()

        uart_cfg = bus.shared.get("UART", {}) or {}
        if not int(uart_cfg.get("enable", 0) or 0):
            bus.register_service("circuit_bus_all_list", [])
            bus.register_service("circuit_bus_all_by_id", {})
            bus.register_service("circuit_bus_list", [])
            bus.register_service("circuit_bus_by_id", {})
            return

        import machine

        all_buses = []
        all_by_id = {}
        buses = []
        by_id = {}
        lst = uart_cfg.get("list", []) or []
        for idx, item in enumerate(lst):
            uid = int(item.get("id", 1) or 1)
            baud = int(item.get("baudrate", 115200) or 115200)
            rxbuf = item.get("rxbuf", None)
            txbuf = item.get("txbuf", None)
            timeout = int(item.get("timeout", 0) or 0)
            timeout_char = int(item.get("timeout_char", 0) or 0)
            buf_cfg = bus.shared.get("Buffer", {}) or {}
            buf_size = int(buf_cfg.get("size", 4096) or 4096)
            if rxbuf is None:
                rxbuf = buf_size
            if txbuf is None:
                txbuf = buf_size
            gpio = item.get("GPIO", {}) or {}
            tx = gpio.get("tx", None)
            rx = gpio.get("rx", None)

            uart = None
            try:
                kwargs = {
                    "baudrate": baud,
                    "bits": 8,
                    "parity": None,
                    "stop": 1,
                    "tx": machine.Pin(tx) if tx is not None else None,
                    "rx": machine.Pin(rx) if rx is not None else None,
                    "timeout": timeout,
                    "timeout_char": timeout_char,
                }
                if rxbuf is not None:
                    kwargs["rxbuf"] = int(rxbuf)
                if txbuf is not None:
                    kwargs["txbuf"] = int(txbuf)
                uart = machine.UART(uid, **kwargs)
            except TypeError:
                try:
                    kwargs = {
                        "baudrate": baud,
                        "bits": 8,
                        "parity": None,
                        "stop": 1,
                        "tx": tx,
                        "rx": rx,
                        "timeout": timeout,
                        "timeout_char": timeout_char,
                    }
                    if rxbuf is not None:
                        kwargs["rxbuf"] = int(rxbuf)
                    if txbuf is not None:
                        kwargs["txbuf"] = int(txbuf)
                    uart = machine.UART(uid, **kwargs)
                except TypeError:
                    uart = machine.UART(
                        uid,
                        baudrate=baud,
                        bits=8,
                        parity=None,
                        stop=1,
                        tx=tx,
                        rx=rx,
                        timeout=timeout,
                        timeout_char=timeout_char,
                    )
            except Exception as e:
                get_log().error("❌ [CircuitTask] UART init failed (id={}): {}".format(uid, e))
                continue

            label = "CIRCUIT-UART{}".format(uid)
            cb = CircuitBus(uart, label=label)
            ctx_extra = self._build_link_ctx(uid, baud, tx, rx, item)
            svc = "circuit_bus_uart{}".format(uid)
            all_buses.append(cb)
            all_by_id[uid] = cb
            bus.register_service(svc, cb)

            if selected is None or ("uart", idx) in selected or svc in selected:
                buses.append(cb)
                by_id[uid] = cb
                self._ctx_by_bus_id[id(cb)] = ctx_extra

        self._buses = buses
        bus.register_service("circuit_bus_all_list", all_buses)
        bus.register_service("circuit_bus_all_by_id", all_by_id)
        bus.register_service("circuit_bus_list", buses)
        bus.register_service("circuit_bus_by_id", by_id)
        sources = bus.get_service("bus_sources")
        if not sources:
            sources = BusSources()
            bus.register_service("bus_sources", sources)
        for cb in buses:
            sources.add(cb)

        if buses:
            get_log().info("🔌 [CircuitTask] {} circuit bus(es) online".format(len(buses)))

    def _get_selected_sources(self):
        cfg = bus.shared.get("CircuitDecode", {}) or {}
        if not int(cfg.get("enable", 0) or 0):
            return None
        selected = set()

        lst = cfg.get("list", None)
        if lst is None:
            lst = cfg.get("sources", []) or []
        for it in (lst or []):
            if isinstance(it, str):
                selected.add(it)
                continue
            if not isinstance(it, dict):
                continue
            gpio = it.get("GPIO", {}) or {}
            if "uart" in gpio:
                try:
                    selected.add(("uart", int(gpio.get("uart"))))
                except Exception:
                    pass
            if "spi" in gpio:
                try:
                    selected.add(("spi", int(gpio.get("spi"))))
                except Exception:
                    pass
            if "i2c" in gpio:
                try:
                    selected.add(("i2c", int(gpio.get("i2c"))))
                except Exception:
                    pass
            if "i2c_target" in gpio:
                try:
                    selected.add(("i2c_target", int(gpio.get("i2c_target"))))
                except Exception:
                    pass
            if "can" in gpio:
                try:
                    selected.add(("can", int(gpio.get("can"))))
                except Exception:
                    pass
            svc = it.get("service", None)
            if svc:
                selected.add(svc)
        return selected

    def _build_link_ctx(self, uid, baud, tx, rx, item):
        ctx = {
            "transport": "circuit",
            "uart_id": uid,
            "uart_baudrate": baud,
            "uart_tx": tx if tx is not None else -1,
            "uart_rx": rx if rx is not None else -1,
        }
        link = item.get("link", None)
        if link:
            ctx["link"] = link
        return ctx

    def loop(self):
        if not self.running:
            return
        if not self._buses:
            return
        for b in self._buses:
            ctx_extra = self._ctx_by_bus_id.get(id(b), None)
            if ctx_extra is not None:
                b._decode_ctx = ctx_extra
            b.poll()
            self.success += 1

    def on_stop(self):
        super().on_stop()
        self._buses = []
        self._ctx_by_bus_id = {}
