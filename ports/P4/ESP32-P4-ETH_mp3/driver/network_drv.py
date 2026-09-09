"""
network_drv.py — 網路管理 (無獨立 GPIO)

產物: bus.register_service("network_manager", nm)
"""
from lib.sys.sys_bus import bus
from lib.sys.network_manager import NetworkManager


def init_network(sysbus=None):
    sysbus = sysbus or bus
    nm = sysbus.get_service("network_manager")
    if nm is not None:
        return nm
    nm = NetworkManager(sysbus)
    sysbus.register_service("network_manager", nm)
    # 照 config 建立 LAN / WiFi / AP 介面並嘗試連線,
    # 連上後由 check_network() -> _on_interface_up() 列印各介面 IP。
    nm.init_from_config()
    return nm
