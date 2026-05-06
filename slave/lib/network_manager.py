import network
import time
import machine
try:
    import webrepl
except:
    webrepl = None
from lib.dispatch import dprint

# 定義 Active Mode 常量
MODE_OFF = 0
MODE_ALWAYS_ON = 1
MODE_BOOT_ONLY = 2

class NetworkManager:
    """
    統一網絡接口管理器
    職責:
    - 管理多個網絡接口 (LAN/WiFi)
    - 處理不同的 Active Mode (長期開啟/限時開啟)
    - 支持 RMII LAN 和 SPI LAN
    """
    def __init__(self, sys_bus):
        self.bus = sys_bus
        self.interfaces = {}  # {'lan': obj, 'wifi': obj}
        self.active_modes = {} # {'lan': 1, 'wifi': 2}
        self.boot_time = time.time()
        # 狀態追蹤
        self._state = {
            "connected_interfaces": set(), # 當前已連接的接口名稱集合
            "last_check": 0
        }

    def init_from_config(self):
        """從 bus.shared 讀取配置並初始化"""
        net_cfg = self.bus.shared.get('Network', {})
        
        # 1. 初始化 LAN
        lan_cfg = net_cfg.get('lan')
        if lan_cfg:
            self._init_lan(lan_cfg)
            
        # 2. 初始化 WiFi
        wifi_cfg = net_cfg.get('wifi')
        if wifi_cfg:
            self._init_wifi(wifi_cfg)
            
        # 3. 初始連接檢查
        self.check_network(force=True)

    def _init_lan(self, config):
        """初始化 LAN 接口 (支持 RMII 和 SPI)"""
        mode = config.get('active_mode', MODE_ALWAYS_ON)
        if not config.get('enable', False) or mode == MODE_OFF:
            return

        self.active_modes['lan'] = mode
        
        try:
            # 獲取 GPIO 配置
            gpio_cfg = config.get('GPIO', {})
            
            # 判斷驅動類型
            # 優先檢查 driver 字段，其次檢查 GPIO['spi'] 是否有效
            driver_type = config.get('driver', '').upper()
            spi_idx = gpio_cfg.get('spi', -1)
            
            is_spi = (driver_type == 'W5500' or driver_type == 'SPI') or (spi_idx >= 0 and driver_type != 'RMII')
            
            if is_spi:
                # SPI LAN (W5500)
                dprint("🔌 初始化 SPI LAN (W5500)...")
                spi_list = self.bus.get_service("spi_list")
                if not spi_list:
                    raise Exception("SPI service not available")
                
                if spi_idx < 0 or spi_idx >= len(spi_list):
                    raise Exception(f"Invalid SPI bus index: {spi_idx}")
                
                spi = spi_list[spi_idx]
                
                # 檢查 CS/RST 引腳 (從 GPIO 讀取)
                cs_pin_num = gpio_cfg.get('cs', -1)
                rst_pin_num = gpio_cfg.get('rst', -1)
                
                if cs_pin_num < 0 or rst_pin_num < 0:
                     raise Exception("Invalid CS/RST pin for SPI LAN")

                cs_pin = machine.Pin(cs_pin_num)
                rst_pin = machine.Pin(rst_pin_num)
                
                # 初始化 WIZNET5K
                lan = network.WIZNET5K(spi, cs_pin, rst_pin)
                lan.active(True)
                
                # 如果有靜態 IP 配置
                if config.get('static_ip'):
                    lan.ifconfig(tuple(config['static_ip']))
                    
                self.interfaces['lan'] = lan
                dprint("✓ SPI LAN 已初始化")
                
            else:
                # RMII LAN (原生 ETH)
                dprint("🔌 初始化 RMII LAN...")
                # 處理配置列表或單一配置
                eth_cfg = config
                if 'list' in config: # 兼容舊結構
                    eth_cfg = config['list'][0]
                
                # 構建參數
                phy_type = eth_cfg.get('phy_type', network.PHY_LAN8720)
                # 處理 phy_type 字符串轉常量 (如果是字符串)
                if isinstance(phy_type, str):
                    if "IP101" in phy_type: phy_type = network.PHY_IP101
                    else: phy_type = network.PHY_LAN8720
                
                lan = network.LAN(
                    mdc=machine.Pin(eth_cfg['GPIO']['mdc']),
                    mdio=machine.Pin(eth_cfg['GPIO']['mdio']),
                    ref_clk=machine.Pin(eth_cfg['GPIO']['ref_clk']),
                    phy_addr=eth_cfg['phy_addr'],
                    phy_type=phy_type
                )
                lan.active(True)
                self.interfaces['lan'] = lan
                dprint("✓ RMII LAN 已初始化")
                
        except Exception as e:
            dprint(f"✗ LAN 初始化失敗: {e}")

    def _init_wifi(self, config):
        """初始化 WiFi 接口 (STA -> Fail -> AP)"""
        if not hasattr(network, 'WLAN'):
            dprint("⚠️ 此固件/硬體不支持 WLAN，跳過 WiFi 初始化")
            return

        mode = config.get('active_mode', MODE_BOOT_ONLY)
        if not config.get('enable', False) or mode == MODE_OFF:
            return

        self.active_modes['wifi'] = mode
        # 讀取超時設定 (預設 300 秒)
        self.wifi_timeout = config.get('timeout', 300)
        
        try:
            dprint("📡 初始化 WiFi STA...")
            try:
                network.WLAN(network.AP_IF).active(False)
                time.sleep(0.5) # Wait for radio to fully power down
            except: pass
            
            
            wlan = network.WLAN(network.STA_IF)
            wlan.active(True)
            dprint("   等待 WiFi 射頻啟動 (3s)...")
            time.sleep(3.0) # Give it more time based on user feedback
            
            # Scan and list WiFi networks (with retry)
            try:
                dprint("🔍 掃描 WiFi 訊號中...")
                scan_res = []
                for i in range(3):
                    try:
                        scan_res = wlan.scan()
                    except Exception as e:
                        dprint(f"   (嘗試 {i+1}/3) 掃描錯誤: {e}")
                    
                    if scan_res: break
                    dprint(f"   (嘗試 {i+1}/3) 未找到訊號，等待 1.5s 重試...")
                    time.sleep(1.5)
                
                dprint(f"   找到 {len(scan_res)} 個基地台:")
                # Sort by RSSI
                scan_res.sort(key=lambda x: x[3], reverse=True)
                for info in scan_res:
                    # (ssid, bssid, channel, rssi, authmode, hidden)
                    try:
                        ssid = info[0].decode('utf-8')
                        if not ssid: ssid = "<Hidden>"
                    except: ssid = "<Unknown>"
                    rssi = info[3]
                    auth = "OPEN" if info[4] == 0 else "SECURE"
                    dprint(f"   - {ssid:<25} RSSI: {rssi} | {auth}",2)
            except Exception as scan_err:
                dprint(f"   ⚠️ 掃描失敗: {scan_err}")
            
            # 設置 mDNS 名稱 (如果支持)
            if hasattr(wlan, 'config') and 'mdns_name' in config:
                try: 
                    mdns_val = config['mdns_name']
                    # 如果配置中明確要求加後綴，或名稱以 '-' 結尾
                    if config.get('mdns_suffix', False) or mdns_val.endswith("-"):
                        if not mdns_val.endswith("-"): mdns_val += "-"
                        mdns_val += str(self.bus.slave_id)
                    wlan.config(mdns_name=mdns_val)
                    dprint(f"   mDNS configured: {mdns_val}.local")
                except: pass
            
            # 連接 STA
            ssid = config.get('ssid')
            password = config.get('password') or config.get('password_pw') or config.get('ssid_pw') 
            
            connected_success = False
            if ssid:
                if not wlan.isconnected():
                    dprint(f"   連接到: {ssid}")
                    try:
                        wlan.connect(ssid, password)
                        # 簡單的連接等待與重試邏輯
                        for _ in range(5):
                            if wlan.isconnected(): break
                            time.sleep(1)
                        
                        if not wlan.isconnected():
                            dprint("   ⚠️ WiFi 連接超時/失敗")
                        else:
                            dprint(f"   已連接到 WiFi")
                            connected_success = True
                    except Exception as connect_err:
                        dprint(f"   ⚠️ WiFi 連接過程異常: {connect_err}")
                else:
                    dprint(f"   已連接到 WiFi")
                    connected_success = True
            
            if connected_success:
                self.interfaces['wifi'] = wlan
                dprint("✓ WiFi STA 接口已就緒")
            else:
                # STA 失敗，切換到 AP 模式
                dprint("⚠️ STA 連接失敗，切換到 AP 模式...")
                wlan.active(False) # 關閉 STA
                self._start_ap_mode(config)

        except Exception as e:
            dprint(f"✗ WiFi 初始化失敗: {e}")

    def _start_ap_mode(self, config):
        """啟動 AP 模式並開啟 WebREPL"""
        try:
            ap = network.WLAN(network.AP_IF)
            
            # Reset AP state first
            ap.active(False)
            time.sleep(0.1)
            
            ap.active(True)
            
            # 讀取 AP 配置，如果沒有則使用默認值
            ap_ssid = config.get('ap_ssid', f"NetLight-{self.bus.slave_id}")
            ap_password = config.get('ap_password', '12345678')
            
            ap.config(essid=ap_ssid, password=ap_password, authmode=network.AUTH_WPA_WPA2_PSK)
            
            # 設置 AP mDNS 名稱
            if hasattr(ap, 'config') and 'mdns_name' in config:
                try: 
                    mdns_val = config['mdns_name']
                    # 如果配置中明確要求加後綴，或名稱以 '-' 結尾
                    if config.get('mdns_suffix', False) or mdns_val.endswith("-"):
                        if not mdns_val.endswith("-"): mdns_val += "-"
                        mdns_val += str(self.bus.slave_id)
                    
                    ap.config(mdns_name=mdns_val)
                    dprint(f"   mDNS configured: {mdns_val}.local")
                except: pass

            while not ap.active():
                time.sleep(0.1)
                
            dprint(f"📡 AP 模式已啟動: {ap_ssid} / {ap_password}")
            dprint(f"   IP: {ap.ifconfig()[0]}")
            
            self.interfaces['wifi'] = ap # 將 AP 註冊為 wifi 接口
            
            # 僅在 AP 模式下啟動 WebREPL
            if webrepl:
                try:
                    webrepl.start(password='12345678')
                    dprint("💻 WebREPL 服務已啟動 (AP Mode Only)")
                except Exception as we_err:
                    dprint(f"✗ WebREPL 啟動錯誤: {we_err}")
                    
        except Exception as e:
            dprint(f"✗ AP 模式啟動失敗: {e}")

    def disable_wifi(self):
        wifi_iface = self.interfaces.pop('wifi', None)
        if wifi_iface:
            try:
                wifi_iface.active(False)
            except Exception as e:
                dprint(f"⚠️ 關閉 WiFi 時出錯: {e}")
        self.bus.shared['Network']['wifi']['enable'] = 0
        self._state['connected_interfaces'].discard('wifi')
        dprint("📡 WiFi 已關閉")

    def enable_wifi(self):
        wifi_cfg = self.bus.shared.get('Network', {}).get('wifi', {})
        wifi_cfg['enable'] = 1
        self._init_wifi(wifi_cfg)

    def set_app_connected(self, state=True):
        """
        [Command Method] 手動設置應用層連接狀態
        用於 WebREPL 或其他非標準連接方式來保持 WiFi 接口開啟
        """
        self.bus.shared["manual_keep_alive"] = state
        dprint(f"🔒 Manual Keep-Alive set to: {state}")
        # 同步更新 app_connected 以立即生效 (雖然 Core0 會在下一輪循環覆蓋，但我們也修改 Core0)
        self.bus.shared["app_connected"] = state

    def check_network(self, force=False):
        """
        週期性檢查網絡狀態
        在主循環中調用
        """
        now = time.time()
        if not force and now - self._state['last_check'] < 1.0: # 限制檢查頻率 1Hz
            return bool(self._state['connected_interfaces'])
            
        self._state['last_check'] = now
        
        current_connected = set()
        
        # 1. 檢查所有接口
        for name, iface in self.interfaces.items():
            mode = self.active_modes.get(name, MODE_OFF)
            
            # 處理 MODE_BOOT_ONLY 的超時關閉
            if mode == MODE_BOOT_ONLY:
                # 獲取配置的超時時間，預設 300 秒 (5 分鐘)
                timeout = getattr(self, 'wifi_timeout', 300)
                if now - self.boot_time > timeout: 
                    # 檢查是否已連接，若已連接則豁免關閉
                    # 對於 WiFi，我們需要知道是否有應用層連接 (WS) 正在使用它
                    # 但 NetworkManager 屬於底層，不應直接依賴上層狀態
                    # 因此這裡我們透過 bus.shared 獲取一個標誌位 "app_connected"
                    # 這個標誌位應該由 Core0_worker 在 WS 連接成功時設置
                    
                    app_connected = self.bus.shared.get("app_connected", False)
                    
                    connected_now = False
                    try:
                        if hasattr(iface, 'isconnected'): connected_now = iface.isconnected()
                        elif hasattr(iface, 'status'): connected_now = (iface.status() == 2)
                    except: pass

                    # 如果底層沒連接，或者 (底層連接了 但 應用層沒連接)，則關閉
                    # 換句話說：只有當 (底層連接 AND 應用層連接) 時才豁免
                    # 但用戶原話是 "當有任何成功連接的時候就不需要關閉接口"
                    # "成功連接" 可能指底層 WiFi 連接，也可能指 WS 連接
                    # 用戶補充說明: "我是指這種連接,成功建立了一條ws"
                    # 所以必須檢查 app_connected
                    
                    should_keep = connected_now and app_connected
                    
                    if not should_keep:
                        if iface.active():
                            dprint(f"💤 {name.upper()} 達到運行時間限制 ({timeout}s) 且無活躍 WS 連接，關閉接口")
                            iface.active(False)
                        continue
            
            try:
                is_connected = False
                if hasattr(iface, 'isconnected'):
                    is_connected = iface.isconnected()
                elif hasattr(iface, 'status'): # W5500 sometimes uses status
                    is_connected = (iface.status() == 2) # LINK_UP
                
                if is_connected:
                    current_connected.add(name)
                    # 如果之前沒連接，現在連接了
                    if name not in self._state['connected_interfaces']:
                        self._on_interface_up(name, iface)
                else:
                    # 自動重連邏輯 (僅對 MODE_ALWAYS_ON)
                    if mode == MODE_ALWAYS_ON and name == 'wifi':
                        # WiFi 斷線重連通常由系統自動處理，但這裡可以加入額外邏輯
                        pass
                        
            except Exception as e:
                dprint(f"⚠ 檢查 {name} 狀態錯誤: {e}")

        # 更新狀態
        self._state['connected_interfaces'] = current_connected
        
        return bool(current_connected)

    def _on_interface_up(self, name, iface):
        """當接口連接成功時"""
        try:
            cfg = iface.ifconfig()
            dprint(f"🌐 {name.upper()} 連接成功 | IP: {cfg[0]}")
        except:
            dprint(f"🌐 {name.upper()} 連接成功")

    def get_active_interface(self):
        """獲取當前首選的活躍接口 (根據優先級)"""
        priority = self.bus.shared.get('Network', {}).get('priority', ['lan', 'wifi'])
        connected = self._state['connected_interfaces']
        
        for name in priority:
            if name in connected:
                return self.interfaces[name]
        
        # 如果優先級列表中的都不在，返迴任意一個
        if connected:
            return self.interfaces[list(connected)[0]]
            
        return None
