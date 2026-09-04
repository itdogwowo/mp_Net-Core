# lib/webrepl_ctl.py
# WebREPL 統一控制入口 — 預設開啟、與網絡狀態無關。
#
# webrepl.start() 綁 0.0.0.0:8266 (全介面), 一次啟動覆蓋 LAN / WiFi STA / AP 所有 IP;
# 不連線時僅一個 listen socket (RAM 數 KB、CPU 近零), 不觸發即不消耗資源。
# 密碼固定 '12345678' (與 AP 模式既有行為一致)。

PASSWORD = '12345678'

try:
    import webrepl
except ImportError:
    webrepl = None

_state = {"enabled": False}


def ensure():
    """開機呼叫: 確保 WebREPL 已啟動 (已啟動則不動)。回傳 bool 是否可用/已開。"""
    if webrepl is None:
        _state["enabled"] = False
        return False
    try:
        webrepl.start(password=PASSWORD)
        _state["enabled"] = True
        return True
    except Exception:
        _state["enabled"] = False
        return False


def start():
    """(重)啟動 WebREPL。"""
    return ensure()


def stop():
    """關閉 WebREPL。"""
    if webrepl is None:
        _state["enabled"] = False
        return False
    try:
        webrepl.stop()
        _state["enabled"] = False
        return True
    except Exception:
        return False


def status():
    """回傳 (enabled, info) 供 WREPL_RSP。"""
    if webrepl is None:
        return 0, "webrepl module not available"
    return (1 if _state["enabled"] else 0), ("listening 0.0.0.0:8266" if _state["enabled"] else "stopped")
