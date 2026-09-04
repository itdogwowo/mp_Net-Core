# lib/sys/watchdog.py
# WDT 看門狗：config 控制 + 自動關閉（Ctrl+C 時 ConfigManager 存 enable=0）
#            + 自動重新武裝（測試模式沉默逾時 → enable 存回 1 並重啟）
#
# 設計（穩定性優先 —— 無額外執行緒、無跨核心共享、無獨立任務）：
#   - WDT 只在主線程（core0 TaskManager runner_loop）直接 feed()：
#     同執行緒建立、同執行緒餵，完全沒有跨核心/跨執行緒存取。
#   - 測試模式 re-arm 檢查也是 runner_loop 大循環的一步（poll_rearm()，
#     每圈執行一次），不註冊獨立任務。
#   - 系統真卡死（runner 停止餵）→ ~timeout 後 WDT 重置（自動復原）。
#   - 使用者 Ctrl+C 強制暫停 → auto_disable_on_interrupt() 自動把
#     System.watchdog.enable 存成 0（ConfigManager 無損更新）並立即重啟一次
#     （硬食一次，可預測；不讓 WDT timeout 後偷襲打斷 REPL）。
#   - 自動重新武裝：測試模式（enable=0 且 auto_rearm_ms>0）下，開機/最後一次
#     通訊後連續 auto_rearm_ms 沒有任何有效指令封包 → 自動存 enable=1 並
#     machine.reset() → WDT 保護自己回來。「有人操作 = 收到封包」
#     （app.handle_stream → touch()，與 bus_speed 同源）。REPL 暫停期間
#     runner 不跑 → poll_rearm 不跑 → 不會誤重啟正在工作的 session。
#
# ESP32 限制（設計前提）：
#   - machine.WDT 一經建立就「無法手動停止」（無 deinit），soft reset 也不會清除
#     （斷電/硬體 reset 才清）。
#   - timeout 上限約 8388ms（本模組 clamp 到 8000）。
#
# 逃生門：
#   1. config System.watchdog.enable = 0（開發/單元測試：完全不建立 WDT）
#   2. 開機時按住 btn_bypass_gpio → 不建立 WDT（現場測試，不用改 config）
#   3. Ctrl+C → 自動存 enable=0 + 立即重啟一次（硬食一次），之後測試模式無狗
#   4. 恢復 WDT：REPL 執行 watchdog_set_enable(True)（下次開機生效）

import time

# ── 通訊活動追蹤（主線程 app.handle_stream 呼叫 touch()，runner 讀）──
_last_rx = 0   # 最近一次收到有效指令封包的 ticks_ms（0 = 從未）


def touch():
    """收到任何有效指令封包時呼叫（app.handle_stream，與 bus_speed_touch 同位置）。"""
    global _last_rx
    _last_rx = time.ticks_ms()


def idle_ms(now=None):
    """距離最後一次有效通訊的毫秒數；從未收到 → 超大值（視為沉默）。"""
    global _last_rx
    if now is None:
        now = time.ticks_ms()
    if not _last_rx:
        return 0x7FFFFFFF
    return time.ticks_diff(now, _last_rx)


def should_rearm(idle, boot_age, now, rearm_ms):
    """純決策：沉默逾時 → 重新武裝。

    idle     : idle_ms() 結果（毫秒）
    boot_age : 開機至今毫秒數（開機寬限：開機未滿 rearm_ms 不 re-arm，
               避免「開機後從未收到封包」在寬限期內就觸發）
    now      : 目前時間（保留參數，供未來擴充）
    rearm_ms : 沉默多久後重新武裝
    """
    if idle >= rearm_ms and boot_age >= rearm_ms:
        return True
    return False


# ── 測試模式 re-arm 狀態（主線程專用：init_watchdog 設、runner_loop 讀）──
_rearm_ms = 0   # 0 = 非測試模式 / 不啟用 re-arm
_boot_at = 0


def arm_rearm(rearm_ms):
    """測試模式（enable=0 且 auto_rearm_ms>0）時由 init_watchdog 呼叫：
    啟動 re-arm 倒數（開機寬限 = rearm_ms）。回傳 True = 已啟動。
    rearm_ms <= 0 → 不 arm（回 False），避免 max(1000, 0) 誤啟 1 秒倒數。"""
    global _rearm_ms, _boot_at
    try:
        rearm_ms = int(rearm_ms or 0)
    except (TypeError, ValueError):
        rearm_ms = 0
    if rearm_ms <= 0:
        _rearm_ms = 0
        return False
    _rearm_ms = max(1000, rearm_ms)
    _boot_at = time.ticks_ms()
    return True


def poll_rearm():
    """TaskManager.runner_loop(0) 每圈呼叫（大循環的一步，無獨立任務）。

    測試模式沉默逾時 → 存 enable=1 + machine.reset()（存檔成功才重啟，
    避免「存不進 → 重啟 → 又 enable=0」的無限重啟迴圈）；每個開機 session
    只觸發一次（觸發前先清 _rearm_ms）。回傳 True = 已觸發。"""
    global _rearm_ms
    if _rearm_ms <= 0:
        return False
    now = time.ticks_ms()
    if not should_rearm(idle_ms(now), time.ticks_diff(now, _boot_at), now, _rearm_ms):
        return False
    ms = _rearm_ms
    _rearm_ms = 0   # 只觸發一次
    try:
        from lib.sys.log_service import get_log
        get_log().immediate(
            "[WDT] {}ms 無通訊 → 自動重新武裝（config enable=1）…".format(ms))
        if watchdog_set_enable(True):
            import machine
            machine.reset()
        else:
            get_log().error("[WDT] 存 config 失敗 — 不重啟，下個週期再試")
    except Exception as e:
        try:
            from lib.sys.log_service import get_log
            get_log().error("[WDT] re-arm 觸發失敗: {}".format(e))
        except Exception:
            pass
    return True


def gpios(sysbus=None):
    """boot.py Phase 1 使用：回報 btn_bypass_gpio（有設定才 claim，進 GPIO 衝突檢查）。

    電位語意：接低電位（GND）= bypass（WDT 不建立）；浮空/高電位 = 正常
    （開機時設 PULL_UP，未接時被拉高）。
    btn_bypass_gpio 未設定/None/<=0（慣例 -1 = 不設定）→ 不 claim。
    """
    from lib.sys.sys_bus import bus as _bus
    sysbus = sysbus or _bus
    cfg = (sysbus.shared.get("System", {}) or {}).get("watchdog", {}) or {}
    gpio = cfg.get("btn_bypass_gpio")
    if gpio is None:
        return {}
    try:
        gpio = int(gpio)
    except (TypeError, ValueError):
        return {}
    if gpio <= 0:
        return {}
    return {gpio: "wdt_bypass"}


def init_watchdog():
    """依 config System.watchdog 建立 WDT（不啟動任何執行緒）。回傳 WDT 或 None。"""
    from lib.sys.sys_bus import bus
    from lib.sys.log_service import get_log

    cfg = (bus.shared.get("System", {}) or {}).get("watchdog", {}) or {}
    if not cfg.get("enable"):
        # 測試模式：不建立 WDT；auto_rearm_ms>0 → 啟動 re-arm 倒數
        # （由 TaskManager.runner_loop(0) 每圈 poll_rearm() 檢查）
        try:
            rearm = int(cfg.get("auto_rearm_ms", 0) or 0)
        except (TypeError, ValueError):
            rearm = 0
        if arm_rearm(rearm):
            get_log().info(
                "[WDT] 測試模式：{}ms 無通訊 → 自動重新武裝".format(_rearm_ms))
        else:
            get_log().info("[WDT] disabled (config enable=0)")
        return None

    # 逃生門 2：開機按住指定 GPIO → 不建立 WDT（None/<=0 = 不設定，跳過）
    gpio = cfg.get("btn_bypass_gpio")
    try:
        gpio = int(gpio) if gpio is not None else 0
    except (TypeError, ValueError):
        gpio = 0
    if gpio > 0:
        try:
            from machine import Pin
            if Pin(gpio, Pin.IN, Pin.PULL_UP).value() == 0:
                get_log().info("[WDT] bypass — GPIO{} held at boot".format(gpio))
                return None
        except Exception as e:
            get_log().warn("[WDT] bypass check fail: {}".format(e))

    try:
        timeout = int(cfg.get("timeout_ms", 8000) or 8000)
    except (TypeError, ValueError):
        timeout = 8000
    timeout = max(1000, min(timeout, 8000))   # ESP32 上限 ~8388ms

    from machine import WDT
    wdt = WDT(timeout=timeout)
    bus.register_service("wdt", wdt)
    bus.shared["wdt_timeout_ms"] = timeout
    get_log().info("[WDT] online timeout={}ms (feed: core0 runner loop)".format(timeout))
    return wdt


def watchdog_set_enable(enabled):
    """改 config 的 System.watchdog.enable 並無損存檔（下次開機生效）。

    REPL / 指令層用：True = 下次開機啟用 WDT；False = 下次開機停用。
    本次 session 的 WDT 不受影響（無法手動停，timeout 後仍會觸發）。
    """
    from lib.sys.sys_bus import bus
    from lib.sys.log_service import get_log
    try:
        from lib.sys.ConfigManager import cfg_manager
    except Exception as e:
        get_log().error("[WDT] ConfigManager 不可用: {}".format(e))
        return False
    sys_cfg = bus.shared.get("System")
    if sys_cfg is None:
        sys_cfg = {}
        bus.shared["System"] = sys_cfg
    wd = sys_cfg.get("watchdog")
    if wd is None:
        wd = {}
        sys_cfg["watchdog"] = wd
    wd["enable"] = 1 if enabled else 0
    try:
        cfg_manager.save_from_bus(update_key="System.watchdog.enable")
        get_log().info("[WDT] config enable -> {}（下次開機生效）".format(1 if enabled else 0))
        return True
    except Exception as e:
        get_log().error("[WDT] 存 config 失敗: {}".format(e))
        return False


def auto_disable_on_interrupt():
    """使用者 Ctrl+C 強制暫停時呼叫（Core_Manager 的 KeyboardInterrupt 分支）。

    重點在 finally 的可預測操作：不讓 WDT 在 8 秒後「偷襲」重啟（那會在
    使用者想代碼時打斷他）——而是存完 config 後**立即** machine.reset() 一次
    （硬食一次，可預測），下次開機進入測試模式（enable=0，無 WDT）。

    只有 WDT 原本就是開啟狀態才動作：
      - WDT 開啟 → 存 enable=0 + 立即重啟（一次，之後測試模式無狗）
      - 測試模式（無 WDT）→ 不做任何事，REPL session 繼續，無限時間
    要恢復：REPL 執行 watchdog_set_enable(True)。
    """
    from lib.sys.sys_bus import bus
    from lib.sys.log_service import get_log
    try:
        wd = (bus.shared.get("System", {}) or {}).get("watchdog") or {}
        if wd.get("enable") and bus.get_service("wdt") is not None:
            ok = watchdog_set_enable(False)
            get_log().immediate("[WDT] 已自動停用（config enable=0，下次開機生效）")
            if ok:
                get_log().immediate("[WDT] 立即重啟一次（硬食一次）…")
                import machine
                machine.reset()
            else:
                get_log().immediate("[WDT] 存 config 失敗 — 不重啟，WDT 將在 timeout 後觸發")
            return ok
        # 測試模式（無 WDT）：倒數狀態改變 = WatchdogTask 隨 runner 暫停而凍結
        get_log().immediate(
            "[WDT] 測試模式：re-arm 倒數已暫停（REPL session）；"
            "重新運行/開機後重新開始倒數")
    except Exception:
        pass
    return False
