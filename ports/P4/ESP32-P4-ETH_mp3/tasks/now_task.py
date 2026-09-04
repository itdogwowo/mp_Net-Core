from lib.sys.task import Task
from lib.sys.sys_bus import bus


class NowTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx["app"]
        self.now_bus = None

    def on_start(self):
        super().on_start()

        esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {})
        if not esp_cfg.get('enable', 0):
            return

        self.now_bus = bus.get_service("NowBus")
        if self.now_bus is None:
            try:
                from lib.sys.now_bus import NowBus
                wifi_cfg = bus.shared.get('Network', {}).get('wifi', {})
                wifi_enable = wifi_cfg.get('enable', 0)
                channel = esp_cfg.get('channel', 1)

                self.now_bus = NowBus()
                if wifi_enable:
                    ok = self.now_bus.init()
                else:
                    ok = self.now_bus.init(channel=channel)

                if ok:
                    bus.register_service("NowBus", self.now_bus)
                    print("[NowTask] ESP-NOW active, ch={}".format(self.now_bus._channel()))
                else:
                    self.now_bus = None
                    print("[NowTask] init failed")
                    return
            except Exception as e:
                print("[NowTask] init err: {}".format(e))
                self.now_bus = None
                return

        sources = bus.get_service("bus_sources")
        if sources and self.now_bus:
            sources.add(self.now_bus)

    def loop(self):
        if not self.running:
            return
        if self.now_bus and self.now_bus.connected:
            self.now_bus.poll()
            self.success += 1

    def on_stop(self):
        super().on_stop()
        if self.now_bus:
            self.now_bus.deinit()
            self.now_bus = None
