import time, gc
from lib.task import Task
from lib.sys_bus import bus
from lib.net_bus import NetBus
from lib.bus_sources import BusSources
from action.sys_actions import on_connect_request
from lib.network_manager import NetworkManager
from action.stream_actions import handle_supply_chain
from action.heartbeat_actions import send_heartbeat
from action.status_actions import on_status_get
from lib.log_service import get_log

class NetworkTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx['app']
        self.nm = None
        self.ctrl_bus = None
        self.discovery_bus = None
        self.tried_config_connect = False
        
        self.last_report = time.ticks_ms()
        self.s = {"f_local": None, "last_hb": time.ticks_ms()}
        self.hub = None

    def on_start(self):
        super().on_start()
        
        self.nm = bus.get_service("network_manager")
        if not self.nm:
            get_log().warn("⚠️ NetworkManager not found in bus, creating new instance...")
            self.nm = NetworkManager(bus)
            self.nm.init_from_config()

        bus_sys = bus.shared["System"]

        self.ctrl_bus = NetBus(NetBus.TYPE_WS, label="CTRL-WS")
        self.discovery_bus = NetBus(NetBus.TYPE_UDP, label="UDP-DISCV")
        self.discovery_bus.connect(None, bus_sys["discovery_port"])
        bus.register_service("net_bus_ctrl", self.ctrl_bus)
        bus.register_service("net_bus_discovery", self.discovery_bus)
        sources = bus.get_service("bus_sources")
        if not sources:
            sources = BusSources()
            bus.register_service("bus_sources", sources)
        sources.add(self.discovery_bus)
        sources.add(self.ctrl_bus)
        
        self.hub = bus.get_service("pixel_stream")
        
        get_log().info("🚀 [NetworkTask] Data Router Active")

    def _on_connect_wrapper(self, url):
        return on_connect_request(self.ctrl_bus, url)

    def loop(self):
        if not self.running: return

        bus.shared["app_connected"] = self.ctrl_bus.connected or bus.shared.get("manual_keep_alive", False)
        
        network_ok = self.nm.check_network()
        if network_ok:
            self.success += 1
            bus_sys = bus.shared["System"]
            if not self.tried_config_connect and not self.ctrl_bus.connected:
                self.tried_config_connect = True
                m_ip = bus_sys.get("master_IP", "")
                m_port = bus_sys.get("master_port", 0)
                if m_ip and m_port:
                    get_log().info("🔄 Auto-Connecting to stored Master: {}:{}".format(m_ip, m_port))
                    full_url = "ws://{}:{}/ws/{}".format(m_ip, m_port, bus.slave_id)
                    if self._on_connect_wrapper(full_url):
                        get_log().info("✅ Auto-Connect Success!")
                    else:
                        get_log().warn("⚠️ Auto-Connect Failed, waiting for discovery...")

            try:
                ctx_extra = {
                    "app": self.app, 
                    "ctrl_bus": self.ctrl_bus,
                    "on_connect": self._on_connect_wrapper
                }
                self.discovery_bus.poll(**ctx_extra)
                if self.ctrl_bus.connected: 
                    self.ctrl_bus.poll()
            except Exception as e:
                get_log().error("📡 Network Poll Error: {}".format(e))
        
        worker_ctx = {"app": self.app, "send": self.ctrl_bus.write}
        handle_supply_chain(self.hub, self.s, worker_ctx)

        bus_sys = bus.shared["System"]
        now = time.ticks_ms()
        if time.ticks_diff(now, self.s["last_hb"]) > bus_sys["heartbeat_interval"]:
            is_streaming = self.fcache_get("is_streaming")
            if is_streaming and self.ctrl_bus.connected:
                send_heartbeat(worker_ctx)
                on_status_get(worker_ctx, {"query_type": 1})
            self.s["last_hb"] = now
            self.last_report = now

    def on_stop(self):
        super().on_stop()
        if self.ctrl_bus:
            self.ctrl_bus.disconnect()
        get_log().info("NetworkTask Stopped")
