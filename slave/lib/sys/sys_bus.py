# gpio_validate 錯誤訊息用: driver key → 顯示名稱
_DRIVER_LABELS = {
    "spi": "SPI",
    "pin": "PIN",
    "i2c": "I2C",
    "uart": "UART",
    "pwm": "PWM",
    "i2s": "I2S",
    "sd": "SD 卡",
    "tft": "TFT",
    "enc": "編碼器 ENC",
    "wdt": "WDT",
}


class SysBus:
    def __init__(self):
        self._services = {}
        self._providers = {}
        self.shared = {}
        self.slave_id = "UNKNOWN"
        self.cid = 0xFFFF        # 協議定址短身份 (uint16); 由 ConfigManager 於 T0 推動
        self.master_cid = 0xFFFF # 回應定址目標 (uint16); 0xFFFF=廣播(未設定), 由 SET_MASTER/IDENTIFY 設定, 僅內存
        self._gpio_claims = {}

    # 會影響 Router 介面表的服務名（= 真的是一條「通道」的那些）。
    # 其餘服務（uart_list / pin_list / st_pixel…）註冊時不必驚動 Router。
    _ROUTER_IFACE_SVCS = (
        "NowBus", "net_bus_ctrl", "net_bus_discovery",
        "circuit_bus_all_list", "bus_sources", "circuit_bus_list",
    )

    def register_service(self, name, obj):
        if name in self._services:
            return False
        self._services[name] = obj
        # ── 通道上線 → 立刻通知 Router（事件驅動，不靠輪詢）──────────
        #   為什麼掛在這裡：這是**所有通道註冊的唯一入口**，所以在這裡通知
        #   是完備的（不會漏），而且在「真的有東西上線」的那一刻才做事
        #   （沒有東西上線就完全不做事）。
        #
        #   原本的做法是 BusDecodeTask 每 100ms 呼叫 router.sync_ifaces()，
        #   理由是「各 Task 上線順序不定，錯過了要等下一輪」——
        #   但那是**症狀的解法**：真正的原因是 BusDecodeTask.on_start 跑在
        #   NowTask/NetworkTask 之前，第一輪 sync 時 NowBus 還不存在。
        #   改成事件驅動後，那個輪詢就不需要了。
        #
        #   注意：Router 是 BusDecodeTask 建立後才註冊成服務，所以這裡要
        #   容錯「Router 還沒出生」的情況（回 None 就跳過）。
        if name in self._ROUTER_IFACE_SVCS:
            r = self._services.get("signal_router")
            if r is not None:
                try:
                    r.sync_ifaces(self)
                except Exception:
                    pass
        return True

    def get_service(self, name):
        return self._services.get(name)

    def has_lcd(self):
        """LCD 是否存在於 bus 上(boot.py 的 init_tft 成功才有)。
        用來 gate 依賴 LCD 的模組(lvgl)。
        沒有 LCD 時這些模組會 import 失敗或無法運作,因此整段 import/註冊都跳過。"""
        return self.get_service("lcd") is not None

    def register_provider(self, key, func):
        if key in self._providers:
            return False
        self._providers[key] = func
        return True

    def get_metrics(self):
        res = {k: f() for k, f in self._providers.items()}
        res["slave_id"] = self.slave_id
        return res

    def gpio_claim(self, gpio, driver, label=""):
        label = label or "{}:{}".format(driver, gpio)
        if gpio not in self._gpio_claims:
            self._gpio_claims[gpio] = []
        self._gpio_claims[gpio].append({"driver": driver, "label": label})

    def gpio_validate(self):
        """檢查 GPIO 衝突。衝突 → 印出明細（哪隻腳/哪個外設對撞）並回 False；
        無衝突 → 回 True。不 raise——訊息由本函式正確輸出（永遠顯示，
        不受 debug_level 影響——衝突是致命設定錯誤），呼叫端收到 False
        自行決定中止或繼續。"""
        conflicts = {}
        for gpio, claims in self._gpio_claims.items():
            drivers = set(c["driver"] for c in claims)
            if len(drivers) > 1:
                conflicts[gpio] = claims
        if not conflicts:
            return True
        lines = ["GPIO CONFLICT: 同一腳位被多個外設同時使用，請檢查 config.json 的 GPIO 設定:"]
        for gpio, claims in sorted(conflicts.items()):
            descs = ["{} ({})".format(c["label"], _DRIVER_LABELS.get(c["driver"], c["driver"])) for c in claims]
            if len(descs) == 2:
                lines.append("  GPIO {}: {} 與 {} 衝突".format(gpio, descs[0], descs[1]))
            else:
                lines.append("  GPIO {}: {} 互相衝突".format(gpio, "、".join(descs)))
        for line in lines:
            print(line)
        return False

    def gpio_dump(self):
        """正常 GPIO 清單（例行資訊，level 2——debug_level>=2 才顯示，減噪音）。"""
        try:
            from lib.sys.dispatch import dprint
        except Exception:
            dprint = print
        if not self._gpio_claims:
            dprint("[GPIO] (none claimed)", level=2)
            return
        dprint("[GPIO] claimed pins:", level=2)
        for gpio in sorted(self._gpio_claims.keys()):
            for c in self._gpio_claims[gpio]:
                dprint("  {:>3}  {:<16} {}".format(gpio, c["driver"], c["label"]), level=2)


bus = SysBus()
