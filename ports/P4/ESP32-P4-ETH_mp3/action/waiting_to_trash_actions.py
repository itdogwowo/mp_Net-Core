# waiting_to_trash_actions.py
# 過渡期顯示控制協議（mode/brightness/time）。
#
# 0x1501 WTT_CTL   = 設定指令:要求對方把 mode/brightness 改成某值(255=不改該欄位)。
# 0x1502 WTT_STATUS= 狀態回覆:對方回報 mode/brightness/time —— 收到就寫 Global
#                    (bus._display_mode/_display_brightness/_display_time),本板不用
#                    自己寫表記每個模式的時間,倒數 time 直接來自對方。
#
# 跨板指令走 schema dispatch（ESP-NOW/UART/WebSocket），
# 翻譯成 bus.shared["_display_cmd"]，action_task_1 消費執行。
#
# 命名為 waiting_to_trash：這組 cmd 碼/欄位待日後重整協議時清理。
# 混搭環境：
#   - 面板裝置 LVGL/按鈕 → ESP-NOW(0x1501) → 執行裝置 dispatch → on_ctl
#     → bus.shared["_display_cmd"] → ActionTask1 消費 → UART 執行。
#   - 執行裝置 UART echo / 週期狀態 → ESP-NOW(0x1502) → 面板裝置 on_status
#     → 寫 _display_* Global → LVGL 頁面顯示(已確認/倒數)。
#   - 同板 LVGL 頁面 → 直寫 bus.shared["_display_cmd"]（不過 dispatch，同板）。
# 兩條路殊途同歸，action_task_1._consume_display_cmd() 統一消費。

from lib.sys.sys_bus import bus

_NO_CHANGE = 0xFF   # u8 約定：255 = 不改該欄位


def on_ctl(ctx, args):
    """0x1501 — 設定 mode/brightness（跨板指令）。
    mode/brightness = 255 表示不改該欄位(0 是合法值,不能用 or 當預設)。"""
    mode = args.get("mode", _NO_CHANGE)
    brightness = args.get("brightness", _NO_CHANGE)
    if mode is None:
        mode = _NO_CHANGE
    if brightness is None:
        brightness = _NO_CHANGE
    mode = int(mode)
    brightness = int(brightness)
    cmd = {}
    if mode != _NO_CHANGE:
        cmd["mode"] = mode & 0xFF
    if brightness != _NO_CHANGE:
        cmd["brightness"] = max(0, min(brightness, 36))
    if cmd:
        bus.shared["_display_cmd"] = cmd
    print("[WTT] ctl mode={} bri={}".format(
        "skip" if mode == _NO_CHANGE else mode,
        "skip" if brightness == _NO_CHANGE else brightness))


def on_status(ctx, args):
    """0x1502 — 狀態回覆:對方回報 mode/brightness/time,寫 Global 供各核心讀取。
    收到即為「執行裝置已確認」的 echo,LVGL 頁面據此顯示已確認/倒數時間。"""
    mode = int(args.get("mode", 0) or 0)
    brightness = int(args.get("brightness", 0) or 0)
    t = int(args.get("time", 0) or 0)
    bus.shared["_display_mode"] = mode & 0xFF
    bus.shared["_display_brightness"] = max(0, min(brightness, 36))
    bus.shared["_display_time"] = max(0, t)
    # seq 遞增:即使時間值與上次相同(重新計時 240→240),也要讓
    # LVGL 頁面知道「收到新幀」而重置本地倒數(光看值無法分辨同值重送)。
    bus.shared["_display_time_seq"] = (bus.shared.get("_display_time_seq", 0) + 1) & 0xFF
    print("[WTT] status mod={:02X} bri={} time={}".format(mode & 0xFF, brightness, t))


def register(app):
    app.disp.on(0x1501, on_ctl)
    app.disp.on(0x1502, on_status)
    print("[Action] waiting_to_trash actions registered")
