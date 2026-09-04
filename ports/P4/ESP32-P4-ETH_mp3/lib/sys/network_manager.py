import network
import time
import machine
try:
    import webrepl
except:
    webrepl = None
from lib.sys.dispatch import dprint

# 定義 Active Mode 常量
MODE_OFF = 0
MODE_ALWAYS_ON = 1
MODE_BOOT_ONLY = 2

CRED_MAX = 10
CRED_BTREE_KEY = b"wifi_credentials"


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
        self.interfaces = {}  # {'lan': obj, 'wifi': STA obj, 'wifi_ap': AP obj}
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

    def _wait_ip(self, iface, timeout_s=6):
        """等待 DHCP 取得有效 IP(非空、非 0.0.0.0),回傳 IP 字串或 None。"""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                ip = iface.ifconfig()[0]
            except Exception:
                ip = None
            if ip and ip not in ("0.0.0.0", ""):
                return ip
            time.sleep(0.2)
        return None

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

            # 等待 DHCP 取得有效 IP(有線/WiFi 各自獨立,可同時存在)
            # 成功後 IP 由 check_network() -> _on_interface_up() 統一列印
            lan = self.interfaces.get('lan')
            if lan is not None and not self._wait_ip(lan):
                dprint("⚠️ LAN 已初始化但尚未取得 IP")
                
        except Exception as e:
            dprint(f"✗ LAN 初始化失敗: {e}")

    def _cur_ssid(self, wlan):
        """回傳目前連上的 SSID (未連或失敗回空字串)。"""
        try:
            s = wlan.config("ssid")
            if s:
                return s
        except Exception:
            pass
        return ""

    def _disable_pm(self, wlan):
        """關閉 WiFi 省電模式。

        ESP32 預設開省電時, 射頻會在 DTIM beacon 之間休眠, 導致 ARP/單播
        回應延遲數百 ms 甚至掉包 — PC 端 TCP connect 常因此 ETIMEDOUT,
        但只要先 ping 一下 (強制 ARP 解析 + 喚醒) 就又能連。關掉省電可根治。"""
        try:
            pm = getattr(wlan, "PM_PERFORMANCE", None)
            if pm is None:
                pm = getattr(wlan, "PM_NONE", None)
            if pm is not None:
                wlan.config(pm=pm)
                dprint("   WiFi 省電: 已關閉")
        except Exception:
            pass

    def _init_wifi(self, config):
        """初始化 WiFi 接口 (STA -> Fail -> AP)

        原則: 已連到「指定 SSID」就直接沿用, 不重連、不掃描;
        只有未連線、或連到非指定 SSID 時才斷開重連。"""
        if not hasattr(network, 'WLAN'):
            dprint("⚠️ 此固件/硬體不支持 WLAN，跳過 WiFi 初始化")
            return

        mode = config.get('active_mode', MODE_BOOT_ONLY)
        if not config.get('enable', False) or mode == MODE_OFF:
            return

        self.active_modes['wifi'] = mode
        # 讀取超時設定 (預設 300 秒)
        self.wifi_timeout = config.get('timeout', 300)

        # 指定 SSID / 密碼 (下面判斷「是否已連到指定 SSID」要用)
        default_ssid = config.get("ssid") or ""
        default_pw = (
            config.get("password")
            or config.get("password_pw")
            or config.get("ssid_pw")
            or ""
        )
        # 密碼已被 ConfigManager 從 config 清掉時, 從 secrets.db 補 (入庫的 ssid_pw)
        if not default_pw and default_ssid:
            try:
                db = self._get_db()
                if db is not None:
                    import ujson as json
                    raw_pw = db.get(b"Network.wifi.ssid_pw")
                    if raw_pw is not None:
                        default_pw = json.loads(raw_pw.decode()) or ""
            except Exception:
                pass

        try:
            wlan = network.WLAN(network.STA_IF)

            # ── 快速路徑: 已連到指定 SSID → 沿用, 不重連、不掃描 ──
            if wlan.active() and wlan.isconnected():
                cur = self._cur_ssid(wlan)
                if not default_ssid or cur == default_ssid:
                    dprint("✓ 已連接到指定 WiFi: {} (沿用, 不重連)".format(cur or default_ssid))
                    self.interfaces['wifi'] = wlan
                    self._disable_pm(wlan)
                    return

            dprint("📡 初始化 WiFi STA...")
            try:
                network.WLAN(network.AP_IF).active(False)
                time.sleep(0.5) # Wait for radio to fully power down
            except: pass

            wlan.active(True)

            # 啟動後若 firmware 自動重連到指定 SSID, 直接沿用 (不掃描、不重連)
            if wlan.isconnected():
                cur = self._cur_ssid(wlan)
                if not default_ssid or cur == default_ssid:
                    dprint("✓ 已連接到指定 WiFi: {} (自動重連, 不掃描)".format(cur))
                    self.interfaces['wifi'] = wlan
                    self._disable_pm(wlan)
                    return
                dprint("   已連到非指定 SSID ({}), 斷開重連...".format(cur))
                wlan.disconnect()
                time.sleep(0.5)

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
            
            # ─ 連接 STA (優先走儲存的憑證列表) ─
            connected_ssid = None
            connected_success = False

            creds = self._load_credentials()

            # 建立可用 SSID 集合 (從 scan result)
            known_ssids = set()
            try:
                for info in (scan_res or []):
                    try:
                        known_ssids.add(info[0].decode("utf-8"))
                    except Exception:
                        pass
            except Exception:
                pass

            # 逐筆試連接 (前面已確認: 未連線, 或已斷開非指定 SSID)
            if not wlan.isconnected():
                # 先試儲存的憑證
                for c in creds:
                    ssid = c.get("ssid", "")
                    pw = c.get("pw", "")
                    if ssid and ssid in known_ssids:
                        dprint("   憑證連線: {}".format(ssid))
                        connected_ssid = self._try_connect(wlan, ssid, pw)
                        if connected_ssid:
                            connected_success = True
                            self.save_wifi_credential(ssid, pw)
                            break

                # 再試預設 ssid — 不管在不在掃描結果都要連 (hidden 網路掃不到 SSID)
                if not connected_success and default_ssid:
                    if default_ssid in known_ssids:
                        dprint("   預設連線: {}".format(default_ssid))
                    else:
                        dprint("   預設連線 (hidden): {}".format(default_ssid))
                    connected_ssid = self._try_connect(wlan, default_ssid, default_pw)
                    if connected_ssid:
                        connected_success = True
                        self.save_wifi_credential(default_ssid, default_pw)

                # 最後試任何已知 SSID (就算不在 scan 中)
                if not connected_success:
                    for c in creds:
                        ssid = c.get("ssid", "")
                        pw = c.get("pw", "")
                        if ssid:
                            dprint("   盲連: {}".format(ssid))
                            connected_ssid = self._try_connect(wlan, ssid, pw)
                            if connected_ssid:
                                connected_success = True
                                self.save_wifi_credential(ssid, pw)
                                break

            if connected_success:
                self.interfaces['wifi'] = wlan
                self._disable_pm(wlan)
                dprint("✓ WiFi STA 接口已就緒")
            else:
                # 🔧 修復: STA 失敗時「保留 STA + 並存開 AP」, 而不是關掉 STA 讓 AP 取代。
                #    AP 註冊為 'wifi_ap' (不再覆蓋 'wifi' 槽位), check_network 才能持續
                #    重試 STA — 之前 AP 佔了 'wifi' 後 isconnected() 恆 True, STA 永不重連。
                self.interfaces['wifi'] = wlan
                dprint("⚠️ STA 連接失敗, 啟動 AP 並持續重試 STA...")
                self._start_ap_mode(config)

        except Exception as e:
            dprint(f"✗ WiFi 初始化失敗: {e}")

    def _start_ap_mode(self, config):
        """啟動 AP 模式並開啟 WebREPL"""
        try:
            ap = network.WLAN(network.AP_IF)
            slave_id = getattr(self.bus, "slave_id", "") or ""
            if slave_id in ("", "UNKNOWN"):
                try:
                    import machine
                    slave_id = "".join("{:02X}".format(b) for b in machine.unique_id())
                    self.bus.slave_id = slave_id
                except Exception:
                    slave_id = "UNKNOWN"
            
            # Reset AP state first
            ap.active(False)
            time.sleep(0.1)
            
            ap.active(True)
            
            # 讀取 AP 配置，如果沒有則使用默認值
            ap_ssid = config.get('ap_ssid', f"NetCore-{slave_id}")
            ap_password = config.get('ap_password', '12345678')
            
            ap.config(essid=ap_ssid, password=ap_password, authmode=network.AUTH_WPA_WPA2_PSK)
            
            # 設置 AP mDNS 名稱
            if hasattr(ap, 'config') and 'mdns_name' in config:
                try: 
                    mdns_val = config['mdns_name']
                    # 如果配置中明確要求加後綴，或名稱以 '-' 結尾
                    if config.get('mdns_suffix', False) or mdns_val.endswith("-"):
                        if not mdns_val.endswith("-"): mdns_val += "-"
                        mdns_val += str(slave_id)
                    
                    ap.config(mdns_name=mdns_val)
                    dprint(f"   mDNS configured: {mdns_val}.local")
                except: pass

            while not ap.active():
                time.sleep(0.1)
                
            dprint(f"📡 AP 模式已啟動: {ap_ssid} / {ap_password}")
            dprint(f"   IP: {ap.ifconfig()[0]}")
            
            self.interfaces['wifi_ap'] = ap  # 🔧 AP 用獨立槽位, 不覆蓋 'wifi' (STA)
            
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
        for key in ('wifi', 'wifi_ap'):
            iface = self.interfaces.pop(key, None)
            if iface:
                try:
                    iface.active(False)
                except Exception as e:
                    dprint(f"⚠️ 關閉 WiFi 時出錯: {e}")
        self.bus.shared['Network']['wifi']['enable'] = 0
        self._state['connected_interfaces'].discard('wifi')
        self._state['connected_interfaces'].discard('wifi_ap')
        dprint("📡 WiFi 已關閉")

    def enable_wifi(self):
        wifi_cfg = self.bus.shared.get('Network', {}).get('wifi', {})
        wifi_cfg['enable'] = 1
        try:
            self.boot_time = time.time()
        except Exception:
            self.boot_time = 0
        self._init_wifi(wifi_cfg)

    def enable_lan(self):
        """[NET_START] 強制啟動有線 LAN (依 config 的 lan 參數)。

        與 enable_wifi 對稱: force enable=1 再 _init_lan。若已在跑則跳過,
        避免在同腳位重複建立 network.LAN。"""
        lan_cfg = self.bus.shared.get('Network', {}).get('lan') or {}
        if 'lan' in self.interfaces:
            dprint("   LAN 已初始化, 略過")
            return True
        lan_cfg['enable'] = 1
        try:
            self.boot_time = time.time()
        except Exception:
            self.boot_time = 0
        self._init_lan(lan_cfg)
        return 'lan' in self.interfaces

    def enable_ap(self):
        """[NET_START] 強制啟動 AP 模式 (依 config 的 wifi 參數, 走 _start_ap_mode)。"""
        wifi_cfg = self.bus.shared.get('Network', {}).get('wifi') or {}
        self._start_ap_mode(wifi_cfg)
        return 'wifi_ap' in self.interfaces

    def get_ips(self):
        """回傳多介面 IP 清單 {"lan":…, "wifi":…, "ap":…}; 無 IP 的介面省略。

        同時上線 LAN + WiFi STA (或 AP + 有線) 會各拿一個 IP, WebREPL 都能連。"""
        out = {}
        try:
            import network
            sta = network.WLAN(network.STA_IF)
            ap = network.WLAN(network.AP_IF)
        except Exception:
            sta = ap = None

        lan = self.interfaces.get('lan')
        if lan is not None:
            try:
                ip = lan.ifconfig()[0]
                if ip and ip not in ("0.0.0.0", ""):
                    out["lan"] = ip
            except Exception:
                pass

        # AP: 獨立槽位 'wifi_ap' (或直接辨識 AP_IF), 與 STA 並存各自回報 IP
        try:
            if ap is not None and ap.active():
                ip = ap.ifconfig()[0]
                if ip and ip not in ("0.0.0.0", ""):
                    out["ap"] = ip
        except Exception:
            pass

        # STA: 'wifi' 槽位現在永遠是 STA (AP 不再覆蓋)
        wifi = self.interfaces.get('wifi')
        if wifi is not None:
            try:
                ip = wifi.ifconfig()[0]
                if ip and ip not in ("0.0.0.0", ""):
                    out["wifi"] = ip
            except Exception:
                pass

        return out

    def set_app_connected(self, state=True):
        """
        [Command Method] 手動設置應用層連接狀態
        用於 WebREPL 或其他非標準連接方式來保持 WiFi 接口開啟
        """
        self.bus.shared["manual_keep_alive"] = state
        dprint(f"🔒 Manual Keep-Alive set to: {state}")
        # 同步更新 app_connected 以立即生效 (雖然 Core0 會在下一輪循環覆蓋，但我們也修改 Core0)
        self.bus.shared["app_connected"] = state

    # ── Wi-Fi 憑證管理 (最多 10 組，以加入順序取代) ──

    def _get_db(self):
        """從 ConfigManager 取得 btree db"""
        try:
            from lib.sys.ConfigManager import cfg_manager
            return cfg_manager._db if cfg_manager else None
        except Exception:
            return None

    def _load_credentials(self):
        """從 secrets.db (btree) 載入憑證列表"""
        creds = []
        try:
            db = self._get_db()
            if db is not None:
                import ujson as json
                raw = db.get(CRED_BTREE_KEY)
                if raw is not None:
                    creds = json.loads(raw.decode())
                    if not isinstance(creds, list):
                        creds = []
        except Exception:
            pass
        # 補上 config 的 ssid + secrets.db 存的 ssid_pw (hidden 網路也在內)
        try:
            db = self._get_db()
            if db is not None:
                raw_pw = db.get(b"Network.wifi.ssid_pw")
                if raw_pw is not None:
                    import ujson as json
                    pw = json.loads(raw_pw.decode())
                    ssid = self.bus.shared.get("Network", {}).get("wifi", {}).get("ssid", "")
                    if ssid and pw and not any(c.get("ssid") == ssid for c in creds):
                        creds.append({"ssid": ssid, "pw": pw})
        except Exception:
            pass
        if creds:
            return creds
        cfg = self.bus.shared.get("Network", {}).get("wifi", {}) or {}
        return cfg.get("credentials", []) or []

    def _save_credentials(self, creds):
        """寫入憑證到 secrets.db (btree)"""
        try:
            db = self._get_db()
            if db is not None:
                import ujson as json
                db[CRED_BTREE_KEY] = json.dumps(creds).encode()
                db.flush()
        except Exception:
            pass

    def save_wifi_credential(self, ssid, password):
        """新增/更新 WiFi 憑證 (自動維護最多 10 組)

        規則:
          - 已存在 → 移至最末 (最近使用)
          - 不存在 + 未滿 10 → 附加到最末
          - 不存在 + 已滿 10 → 移除最舊 (index 0)，附加到最末
        """
        creds = self._load_credentials()

        # 查找是否已存在
        for i, c in enumerate(creds):
            if c.get("ssid") == ssid:
                c["pw"] = password
                creds.pop(i)
                creds.append(c)
                self._save_credentials(creds)
                return

        # 滿了就移除最舊的
        if len(creds) >= CRED_MAX:
            creds.pop(0)

        creds.append({"ssid": ssid, "pw": password})
        self._save_credentials(creds)

    def _try_connect(self, wlan, ssid, password):
        """嘗試連接到指定 SSID，回傳成功連上的 SSID 或 None

        hidden 網路掃描不到 SSID、關聯較慢，等待時間給足 15s；
        超時也不主動 disconnect，讓韌體持續嘗試。"""
        try:
            wlan.connect(ssid, password)
            for _ in range(15):
                if wlan.isconnected():
                    dprint("   ✓ 已連接到 {}".format(ssid))
                    return ssid
                time.sleep(1)
        except Exception as e:
            dprint("   ✗ {}: {}".format(ssid, e))
        return None

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
            # 🔧 'wifi' (STA) 豁免: 自動上線設計需要 STA 常開 — master 敲門 (UDP)
            #    與 WS 重連都必須設備在網路上; 若讓逾時關閉 wifi, 就會跟重連邏輯
            #    打架 (關了又開, 無限迴圈)。LAN 維持原逾時省電行為。
            if mode == MODE_BOOT_ONLY and name != 'wifi':
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

                    ap_mode = False
                    if name == 'wifi':
                        try:
                            ap = network.WLAN(network.AP_IF)
                            ap_mode = ap.active() and (iface is ap)
                        except:
                            ap_mode = False

                    # 如果底層沒連接，或者 (底層連接了 但 應用層沒連接)，則關閉
                    # 換句話說：只有當 (底層連接 AND 應用層連接) 時才豁免
                    # 但用戶原話是 "當有任何成功連接的時候就不需要關閉接口"
                    # "成功連接" 可能指底層 WiFi 連接，也可能指 WS 連接
                    # 用戶補充說明: "我是指這種連接,成功建立了一條ws"
                    # 所以必須檢查 app_connected
                    
                    # 🔧 只有「已連上 (STA/AP) 但沒有 WS 在用」才關閉 (省電);
                    #    從未連上 (正在重試 STA) 不關, 讓它繼續找網路
                    should_keep = app_connected or not (connected_now or ap_mode)
                    
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
                    # 如果之前沒連接，現在連接了 (只在首次連上時關省電, 避免每輪重設刷屏)
                    if name not in self._state['connected_interfaces']:
                        if name == 'wifi':
                            # 連上時確保省電已關 (boot 路徑可能漏掉; 省電會讓 PC 要 ping 才連得上)
                            self._disable_pm(iface)
                        self._on_interface_up(name, iface)

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
        # 🔧 STA 優先: STA 連上時關閉 AP (不同時並存; AP 只當 STA 掉線時的備援)
        if name == 'wifi':
            ap = self.interfaces.pop('wifi_ap', None)
            if ap is not None:
                try:
                    ap.active(False)
                    self._state['connected_interfaces'].discard('wifi_ap')
                    dprint("🔄 STA 已連上, 關閉 AP (STA 優先)")
                except Exception as e:
                    dprint(f"⚠️ 關閉 AP 時出錯: {e}")

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
