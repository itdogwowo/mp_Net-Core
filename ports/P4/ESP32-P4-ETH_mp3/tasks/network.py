import time
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.net_bus import NetBus
from lib.sys.bus_sources import BusSources
from action.sys_actions import on_connect_request
from lib.sys.network_manager import NetworkManager
from lib.sys.log_service import get_log

class NetworkTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx['app']
        self.nm = None
        self.ctrl_bus = None
        self.discovery_bus = None
        self.tried_config_connect = False
        
        self.last_report = time.ticks_ms()
        self.now_bus = None
        self._last_discv_poll = 0

    def on_start(self):
        super().on_start()

        get_log().info("🌐 [NetworkTask] 開始初始化網路...")
        
        self.nm = bus.get_service("network_manager")
        if not self.nm:
            get_log().info("🌐 [NetworkTask] 建立 NetworkManager...")
            self.nm = NetworkManager(bus)
            self.nm.init_from_config()
            bus.register_service("network_manager", self.nm)

            for name, iface in self.nm.interfaces.items():
                ip = "?"
                try:
                    cfg = iface.ifconfig()
                    ip = cfg[0]
                except Exception:
                    pass
                get_log().info("🌐 {} 就緒 | IP: {}".format(name.upper(), ip))

            if self.nm.interfaces:
                get_log().info("🌐 [NetworkTask] 網路連線完成")
            else:
                get_log().warn("🌐 [NetworkTask] 沒有可用網路介面")
        
        # ── ESP-NOW ────────────────────────────────────
        #   wifi.enable=1  → ESP-NOW 跟隨 WiFi channel
        #   wifi.enable=0  → ESP-NOW 獨立啟動 STA，用 config 的 channel
        #   兩者共用同一射頻，AP 模式自然 channel 一致
        #   NowTask 也會建立 NowBus —— 這裡先查 service，有就直接重用，
        #   避免第二次 espnow.active(True) 撞 ESP_ERR_ESPNOW_EXIST。
        esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {})
        if esp_cfg.get('enable', 0):
            try:
                from lib.sys.now_bus import NowBus

                now = bus.get_service("NowBus")
                if now is not None:
                    get_log().info("ESP-NOW reuse existing NowBus, ch={}".format(now._channel()))
                else:
                    wifi_cfg = bus.shared.get('Network', {}).get('wifi', {})
                    wifi_enable = wifi_cfg.get('enable', 0)
                    esp_ch = esp_cfg.get('channel', 1)

                    now = NowBus(label="NOW-Bus")

                    if wifi_enable:
                        # WiFi 主控，ESP-NOW 不指定 channel
                        ok = now.init()
                        if not ok:
                            # WiFi 射頻可能尚未就緒，fallback 到 ESP_now channel
                            get_log().warn("ESP-NOW: WiFi radio not ready, fallback ch={}".format(esp_ch))
                            ok = now.init(channel=esp_ch)
                    else:
                        # WiFi 關閉，ESP-NOW 獨佔射頻
                        get_log().info("ESP-NOW: standalone mode, ch={}".format(esp_ch))
                        ok = now.init(channel=esp_ch)

                    if ok:
                        bus.register_service("NowBus", now)
                        get_log().info("ESP-NOW ready, ch={}".format(now._channel()))
                    else:
                        now = None
                        get_log().warn("ESP-NOW init failed")

                if now is not None:
                    self.now_bus = now
            except Exception as e:
                get_log().error("ESP-NOW init error: {}".format(e))

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
        if self.now_bus:
            sources.add(self.now_bus)

        get_log().info("🚀 [NetworkTask] Data Router Active")

    def _on_connect_wrapper(self, url):
        return on_connect_request(self.ctrl_bus, url)

    def loop(self):
        if not self.running:
            return

        now = time.ticks_ms()
        bus.shared["app_connected"] = self.ctrl_bus.connected or bus.shared.get("manual_keep_alive", False)

        # ═════════════════════════════════════════════════
        # P0 — ESP-NOW (最高優先，獨立於 WiFi/LAN)
        # ═════════════════════════════════════════════════
        if self.now_bus:
            self.now_bus.poll()
            self.success += 1

        # ═════════════════════════════════════════════════
        # P1 — 網路狀態檢查 + WiFi polling
        #       check_network() 內部已限頻 1Hz
        # ═════════════════════════════════════════════════
        network_ok = self.nm.check_network()

        if network_ok:
            bus_sys = bus.shared["System"]

            # ── 自動連線 (僅一次, 開機立即嘗試) ──
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

            # ── P2: WiFi polling ──
            ctx_extra = {
                "app": self.app,
                "ctrl_bus": self.ctrl_bus,
                "on_connect": self._on_connect_wrapper,
            }
            # 🔧 還原原設計: 無條件先 poll discovery_bus (UDP 敲門), 再視連線狀態
            #    poll ctrl_bus (WS)。之前誤改成「連線中只 poll ctrl / 離線才 poll
            #    discovery」→ 半開連線(對面靜默消失)時 slave 的 connected 卡在 True,
            #    永遠不讀 UDP, master 敲門叫不回。DISCOVER 收到後 on_connect_request
            #    會依 connected+peer 自行判斷是否要重連。
            if time.ticks_diff(now, self._last_discv_poll) > 250:
                self._last_discv_poll = now
                try:
                    self.discovery_bus.poll(**ctx_extra)
                    self.success += 1
                except Exception as e:
                    get_log().error("Discovery Poll Error: {}".format(e))

            if self.ctrl_bus.connected:
                try:
                    self.ctrl_bus.poll()
                    self.success += 1
                except Exception as e:
                    get_log().error("Ctrl Bus Poll Error: {}".format(e))

    def on_stop(self):
        super().on_stop()
        if self.ctrl_bus:
            self.ctrl_bus.disconnect()
        if self.now_bus:
            self.now_bus.deinit()
            self.now_bus = None
        get_log().info("NetworkTask Stopped")
