from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.circuit_bus import CircuitBus
from lib.sys.bus_sources import BusSources
from lib.sys.log_service import get_log
from lib.sys import bus_speed


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

        # 新架構:線路物件統一由 boot/driver 建立(uart_drv.init_uart → uart_list),
        # circuit 不再自行初始化硬體,只負責把線路包成 CircuitBus 並輪詢進緩衝。
        uart_list = bus.get_service("uart_list")
        if not uart_list:
            get_log().warn("[CircuitTask] uart_list missing — skip circuit buses")
            bus.register_service("circuit_bus_all_list", [])
            bus.register_service("circuit_bus_all_by_id", {})
            bus.register_service("circuit_bus_list", [])
            bus.register_service("circuit_bus_by_id", {})
            return

        all_buses = []
        all_by_id = {}
        buses = []
        by_id = {}
        lst = uart_cfg.get("list", []) or []
        for idx, item in enumerate(lst):
            uid = int(item.get("id", 1) or 1)
            baud = int(item.get("baudrate", 115200) or 115200)
            gpio = item.get("GPIO", {}) or {}
            tx = gpio.get("tx", None)
            rx = gpio.get("rx", None)

            if idx >= len(uart_list):
                get_log().warn("[CircuitTask] uart_list[{}] missing for cfg id={} — skip".format(idx, uid))
                continue
            uart = uart_list[idx]

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

        self._buses = all_buses   # 全部線路:每輪都 poll 進各自的緩衝線
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
        # 臨時提速超時回滾: SYNCING 中 deadline 到 → 自動還原 config 舊速。
        # 純時間檢查, 不依賴收到指令; 即使新速下收不到有效幀也會回滾。
        bus_speed.bus_speed_poll()
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
