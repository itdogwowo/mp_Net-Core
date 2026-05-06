# lib/sys_bus.py

class SysBus:
    def __init__(self):
        self._services = {}    # 大型對象 (如 BufferHub)
        self._providers = {}   # 動態狀態提供者 (lambda)
        self.shared = {}       # App 間讀寫空間
        self.slave_id = "UNKNOWN"

    # --- Service: 解決核心/模組間的功能共享 ---
    def register_service(self, name, obj):
        if name in self._services:
            return False
        self._services[name] = obj
        return True

    def get_service(self, name):
        return self._services.get(name)

    # --- Provider: 解決 App 問的資訊匯報 ---
    def register_provider(self, key, func):
        if key in self._providers:
            return False
        self._providers[key] = func
        return True

    def get_metrics(self):
        """抓取目前所有 App 註冊的實時數據"""
        res = {k: f() for k, f in self._providers.items()}
        res["slave_id"] = self.slave_id
        return res

# 全域單例
bus = SysBus()