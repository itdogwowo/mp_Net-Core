# lib/bus_speed.py
# 臨時提速狀態機 (協商式 + 超時回滾)
#
# 流程 (同步點 = SPEED_ACK 0x1404):
#   master 發 SPEED_SET → slave 記 old_baud/target/timeout_at → 回 SPEED_ACK(舊速)
#   → slave 送出 ACK 後立即切速 (同 handler); master 收 ACK 後立即切速
#   → master 用 STATUS_GET/IDENTIFY_REQ 敲門驗證
#   → 驗證 OK → SPEED_COMMIT 鎖定(取消回滾); 否則 timeout_at 到 → 自動回滾 old_baud
#   → 傳輸完成 → SPEED_REVERT 還原
#
# 設計要點:
#   - 唯一的「等待」是 timeout_ms (沒 COMMIT 就回滾的保險), 不是 apply delay。
#   - 「亂碼不回覆」是切速瞬間外部 bus 的自然現象, 本模組不偵測、不 auto-baud。
#   - 回滾 = 純時間檢查, 由 CircuitTask.loop 每輪呼叫 bus_speed_poll()。
#   - bus_type 沿用 hw_manager.HW 常數: UART=7 / SPI=2 / I2C=3。
#     第一階段僅實作 UART; SPI/I2C 介面預留。

import time
from lib.sys.sys_bus import bus

# 狀態
STATE_IDLE = 0
STATE_SYNCING = 1    # 已切速、待 COMMIT (回滾計時中)
STATE_COMMITTED = 2  # 已鎖定 (不回滾)

_STATE_KEY = "_bus_speed"


def _get_state():
    s = bus.shared.get(_STATE_KEY)
    if not isinstance(s, dict):
        s = {"state": STATE_IDLE}
        bus.shared[_STATE_KEY] = s
    return s


def _get_uart(bus_id):
    """依 bus_id 從 uart_list 取 UART 物件。找不到回 None。"""
    lst = bus.get_service("uart_list")
    if not lst:
        return None
    # uart_list 依 config UART.list 順序; bus_id 對應 config 的 id 欄位。
    # 這裡用 list 索引直接取 (driver 建立順序 = list 順序), 若需精確比對 id
    # 再由 caller 傳 index。為簡化, bus_id 視為 index。
    idx = int(bus_id)
    if 0 <= idx < len(lst):
        return lst[idx]
    return None


def _cur_baud(uart):
    """讀目前 baud。MicroPython UART 無 `baudrate` 屬性時回 0，由 caller 從 config 補。"""
    try:
        if hasattr(uart, "baudrate"):
            return int(uart.baudrate)
    except Exception:
        pass
    return 0


def _config_baud(bus_id):
    """從 config UART.list[bus_id].baudrate 讀舊速（MicroPython UART 無 baudrate 屬性的替代）。"""
    try:
        cfg = bus.shared.get("UART", {})
        lst = cfg.get("list", [])
        idx = int(bus_id)
        if 0 <= idx < len(lst):
            return int(lst[idx].get("baudrate", 115200))
    except Exception:
        pass
    return 0


def _config_item(bus_id):
    """取 config UART.list[bus_id] 整筆（rxbuf/txbuf 等）。"""
    try:
        cfg = bus.shared.get("UART", {})
        lst = cfg.get("list", [])
        idx = int(bus_id)
        if 0 <= idx < len(lst):
            return lst[idx]
    except Exception:
        pass
    return {}


def _reinit_uart(uart, bus_id, baud):
    """以目標 baud 重新 init UART，並保留 config 的 rxbuf/txbuf。

    關鍵：microPython 的 uart.init(baudrate=...) 不帶 rxbuf/txbuf 會把它們
    縮回預設(256)。所以切速時必須重新帶上 rxbuf/txbuf，否則大幀(4KB)收發溢位。"""
    item = _config_item(bus_id)
    kwargs = {"baudrate": baud}
    rxbuf = item.get("rxbuf", 16384)
    txbuf = item.get("txbuf", 16384)
    if rxbuf:
        kwargs["rxbuf"] = int(rxbuf)
    if txbuf:
        kwargs["txbuf"] = int(txbuf)
    try:
        uart.init(**kwargs)
        return True
    except Exception as e:
        print("❌ [BusSpeed] UART{} reinit {} failed: {}".format(bus_id, baud, e))
        return False


def bus_speed_set(bus_type, bus_id, speed, timeout_ms):
    """SPEED_SET: 記 old/target/timeout_at, 進 SYNCING（**不切速**）。
    回 (ok, cur_speed, target_speed)。SPI/I2C 尚未實作 → ok=0。

    同步點 = SPEED_ACK：slave 先回 ACK（舊速），master 收到後兩邊一起切速。
    所以這裡「只記狀態」，真正的 uart.init(target) 由 bus_speed_apply() 在 ACK 發出後做。"""
    if int(bus_type) != 7:  # 第一階段僅 UART
        return 0, 0, 0

    uart = _get_uart(bus_id)
    if uart is None:
        return 0, 0, 0

    old = _cur_baud(uart)
    if not old:                       # MicroPython UART 無 baudrate 屬性 → 從 config 補
        old = _config_baud(bus_id)
    target = int(speed)
    timeout_ms = int(timeout_ms or 0)

    s = _get_state()
    s["state"] = STATE_SYNCING
    s["bus_type"] = int(bus_type)
    s["bus_id"] = int(bus_id)
    s["old_baud"] = old
    s["target_baud"] = target
    s["timeout_at"] = time.ticks_add(time.ticks_ms(), timeout_ms) if timeout_ms > 0 else 0
    s["idle_timeout_ms"] = timeout_ms   # 進入 COMMITTED 後的 idle 上限（暫復用同一 timeout，見註）
    print("🔀 [BusSpeed] UART{} {} → {} (SYNCING, timeout {}ms)".format(bus_id, old, target, timeout_ms))
    return 1, old, target


def bus_speed_apply():
    """在 ACK 發出後呼叫：等 ACK 真正發完(舊速)再切到 target_baud。
    避免 ACK 尾部還卡在 shift register 就被切速而損壞。"""
    s = _get_state()
    uart = _get_uart(s.get("bus_id", 0))
    target = s.get("target_baud", 0)
    if uart is None or not target:
        return False
    # 等 ACK 發完：txdone() 表示 FIFO 空，再多等一個 byte 時間讓最後一個 byte 離開 shift register
    if hasattr(uart, "txdone"):
        try:
            t0 = time.ticks_ms()
            while not uart.txdone():
                if time.ticks_diff(time.ticks_ms(), t0) > 1000:
                    break
                time.sleep_ms(0)
        except Exception:
            pass
    time.sleep_ms(2)                    # 安全 margin（一個 byte @9600 ≈ 1ms）
    if not _reinit_uart(uart, s.get("bus_id"), target):
        _revert()
        return False
    print("🔀 [BusSpeed] UART{} switched → {} (SYNCING)".format(s.get("bus_id"), target))
    return True


def bus_speed_poll(now=None):
    """CircuitTask.loop 每輪呼叫。兩層超時：
    1) SYNCING：deadline(timeout_at) 到仍未 COMMIT → 回滾（設定階段敲門失敗）。
    2) COMMITTED：idle_timeout_at 到（進入通訊後 N 秒無通訊）→ 回滾（通訊層空閒超時）。
    純時間檢查，不依賴收到指令；即使新速下收不到有效幀也會回滾。"""
    s = bus.shared.get(_STATE_KEY)
    if not isinstance(s, dict):
        return
    if now is None:
        now = time.ticks_ms()
    st = s.get("state")
    if st == STATE_SYNCING:
        timeout_at = s.get("timeout_at", 0)
        if timeout_at and time.ticks_diff(now, timeout_at) >= 0:
            _revert()
    elif st == STATE_COMMITTED:
        idle_at = s.get("idle_timeout_at", 0)
        if idle_at and time.ticks_diff(now, idle_at) >= 0:
            print("⏰ [BusSpeed] idle timeout → revert")
            _revert()


def bus_speed_touch():
    """收到任何有效通訊時呼叫：刷新 COMMITTED 的 idle 倒數（通訊層空閒超時重置）。"""
    s = bus.shared.get(_STATE_KEY)
    if not isinstance(s, dict) or s.get("state") != STATE_COMMITTED:
        return
    idle = s.get("idle_timeout_ms", 0)
    if idle > 0:
        s["idle_timeout_at"] = time.ticks_add(time.ticks_ms(), idle)


def _revert():
    """還原 old_baud (config 舊速), 進 IDLE。"""
    s = _get_state()
    uart = _get_uart(s.get("bus_id", 0))
    old = s.get("old_baud", 0)
    if uart is not None and old:
        _reinit_uart(uart, s.get("bus_id", 0), old)
    print("↩️  [BusSpeed] revert UART{} → {} (IDLE)".format(s.get("bus_id"), old))
    s["state"] = STATE_IDLE


def bus_speed_commit(bus_type, bus_id):
    """SPEED_COMMIT: 鎖定新速、取消回滾，並啟動 COMMITTED 層的 idle 超時。回 ok。"""
    s = _get_state()
    if s.get("state") != STATE_SYNCING:
        return 0
    if int(bus_type) != s.get("bus_type") or int(bus_id) != s.get("bus_id"):
        return 0
    s["state"] = STATE_COMMITTED
    s["timeout_at"] = 0
    idle = s.get("idle_timeout_ms", 0)
    s["idle_timeout_at"] = time.ticks_add(time.ticks_ms(), idle) if idle > 0 else 0
    print("🔒 [BusSpeed] UART{} COMMITTED @ {} (idle {}ms)".format(
        bus_id, s.get("target_baud"), idle))
    return 1


def bus_speed_revert(bus_type, bus_id):
    """SPEED_REVERT: 還原 old_baud。回 ok。"""
    s = _get_state()
    if int(bus_type) != s.get("bus_type") or int(bus_id) != s.get("bus_id"):
        return 0
    _revert()
    return 1


def bus_speed_query(bus_type, bus_id):
    """SPEED_QUERY: 回 (state, bus_type, bus_id, cur_speed, target_speed, remain_ms)。"""
    s = _get_state()
    state = s.get("state", STATE_IDLE)
    uart = _get_uart(bus_id)
    cur = _cur_baud(uart) if uart is not None else 0
    if not cur:
        cur = _config_baud(bus_id)
    target = s.get("target_baud", cur)
    remain = 0
    if state == STATE_SYNCING and s.get("timeout_at", 0):
        remain = max(0, time.ticks_diff(s.get("timeout_at"), time.ticks_ms()))
    return state, int(bus_type), int(bus_id), cur, target, remain
