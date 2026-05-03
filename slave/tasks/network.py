import time, gc
from lib.task import Task
from lib.sys_bus import bus
from lib.net_bus import NetBus
from action.sys_actions import on_connect_request
from lib.network_manager import NetworkManager
from lib.fs_manager import fs
from action.stream_actions import handle_supply_chain
from action.heartbeat_actions import send_heartbeat
from action.status_actions import on_status_get

class NetworkTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx['app']
        self.nm = None
        self.ctrl_bus = None
        self.discovery_bus = None
        self.tried_config_connect = False
        
        # Supply chain state
        self.last_report = time.ticks_ms()
        self.s = {"f_local": None, "last_hb": time.ticks_ms()}
        self.hub = None

    def on_start(self):
        super().on_start()
        
        self.nm = bus.get_service("network_manager")
        if not self.nm:
            print("⚠️ NetworkManager not found in bus, creating new instance...")
            self.nm = NetworkManager(bus)
            self.nm.init_from_config()

        bus_sys = bus.shared["System"]

        self.ctrl_bus = NetBus(NetBus.TYPE_WS, label="CTRL-WS")
        self.discovery_bus = NetBus(NetBus.TYPE_UDP, label="UDP-DISCV")
        self.discovery_bus.connect(None, bus_sys["discovery_port"])
        bus.register_service("net_bus_ctrl", self.ctrl_bus)
        bus.register_service("net_bus_discovery", self.discovery_bus)
        
        self.hub = bus.get_service("pixel_stream")
        
        print("🚀 [NetworkTask] Data Router Active")

    def _on_connect_wrapper(self, url):
        return on_connect_request(self.ctrl_bus, url)

    def loop(self):
        if not self.running: return

        # 0. System Task: Flash Write (delegated from Core 1)
        if bus.shared.get("fs_scan_done"):
            fs.finalize_scan()

        # 1. Network Guardian
        # Sync ctrl_bus status to bus.shared for NetworkManager
        bus.shared["app_connected"] = self.ctrl_bus.connected or bus.shared.get("manual_keep_alive", False)
        
        network_ok = self.nm.check_network()
        if network_ok:
            bus_sys = bus.shared["System"]
            # Auto-connect logic on startup
            if not self.tried_config_connect and not self.ctrl_bus.connected:
                self.tried_config_connect = True
                m_ip = bus_sys.get("master_IP", "")
                m_port = bus_sys.get("master_port", 0)
                if m_ip and m_port:
                    print(f"🔄 Auto-Connecting to stored Master: {m_ip}:{m_port}")
                    full_url = f"ws://{m_ip}:{m_port}/ws/{bus.slave_id}"
                    if self._on_connect_wrapper(full_url):
                        print("✅ Auto-Connect Success!")
                    else:
                        print("⚠️ Auto-Connect Failed, waiting for discovery...")

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
                print(f"📡 Network Poll Error: {e}")
        
        # 2. Supply Chain Logic
        worker_ctx = {"app": self.app, "send": self.ctrl_bus.write}
        handle_supply_chain(self.hub, self.s, worker_ctx)

        # 3. System Maintenance
        bus_sys = bus.shared["System"] # Re-fetch in case it changed? Shared is dict ref.
        now = time.ticks_ms()
        if time.ticks_diff(now, self.s["last_hb"]) > bus_sys["heartbeat_interval"]:
            if bus.shared.get("is_streaming") and self.ctrl_bus.connected:
                send_heartbeat(worker_ctx)
                on_status_get(worker_ctx, {"query_type": 1})
            # gc.collect() is handled by TaskManager or Core loop
            self.s["last_hb"] = now
            self.last_report = now
            
        # Optional sleep is handled by TaskManager, but we can sleep small here if needed
        # time.sleep_ms(bus_sys.get("refresh_rate_ms", 1)) 
        # Actually TaskManager loop doesn't sleep much if tasks are active, so we should sleep here
        # or rely on TaskManager.
        # Core0_worker had: time.sleep_ms(bus_sys.get("refresh_rate_ms", 1))
        # User requested no wait time in loop, so we remove sleep.
        pass

    def on_stop(self):
        super().on_stop()
        if self.ctrl_bus:
            self.ctrl_bus.disconnect()
        print("NetworkTask Stopped")
