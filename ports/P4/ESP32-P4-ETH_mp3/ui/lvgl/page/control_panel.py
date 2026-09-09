# ui/lvgl/page/control_panel.py — 模式/亮度/倒數計時頁(橫屏 320×240)
#
# 佈局:
#   左欄(x=6,w=104):lv_list 顯示模式清單。enc 選 list → confirm 進編輯 →
#                    enc 上下選模式 → confirm/exit 退出(選中即寫 bus)。
#   右欄(x=116,w=198):
#     上 亮度(slider)
#     中 倒數時間(arc 進度 + 中央時間文字)
#     下 Bit7/Bit6 旗標狀態 + 兩個切換按鈕
#
# 本機值 + 三態顏色(送出的即時回饋,不依賴 echo):
#   本頁維護 _local_mode/_local_bright:操作立刻更新本機值並顯示,同時送
#   bus.shared["_display_cmd"]。echo(_display_mode/_display_brightness)
#   是最終答案:
#     - 列表:選中=藍;送出後 pending=琥珀;echo 出現 → 回藍
#       (一致=成功停在該位置;不一致=超時/未成功,還原成 echo 的位置)。
#     - LED(拍攝/可動):灰=off;pending=琥珀(已送出);echo 確認=綠。
#   頁面不加超時:echo 不出現就一直琥珀等回覆,出現即定案。
#
# 協議對接(混搭環境:本頁是控制端也是被控制端):
#   指令(控制端): _send_cmd() → bus.shared["_display_cmd"] = {"mode":..,"brightness":..}
#                 → action_task_1._consume_display_cmd() → set_display_state() → UART 執行
#                 跨板時走 schema 0x1501(waiting_to_trash_actions.on_ctl 翻譯進同一欄位)
#   狀態(被控制端,顯示用): bus.shared["_display_mode/_brightness/_time"]
#                 action_task_1 執行後寫回,本頁 update() 讀同一位置顯示
#   control_panel dict 為本頁快取(is_running 等)
import time
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_SLIDER, ITEM_BUTTON

MODE_LABELS = ["模式 1", "模式 2", "模式 3", "模式 4", "模式 5"]
_TIME_MAX = 255   # time 欄位上限(1 byte),arc 滿圓 = 255
_BIT7 = 0x80      # 特殊模式旗標
_BIT6 = 0x40      # 保留旗標(目標頂部/底部)
_MODE_MASK = 0x3F

nav = Nav()
scr = None
_mode_list = None
_mode_btns = []
_bright_sl = _bright_lb = _time_lb = _time_arc = _run_lb = None
_bit7_led = _bit6_led = None
_last_txt = {}
# 倒數計時(本地非阻塞,不 sleep):每輪取樣 ticks_ms 與上次對比,
# 累積滿 1000ms 才 -1,餘數保留給下次 → frame 長短不齊也不漂移。
# 收到新幀(_display_time_seq 變更,即使值相同=重新計時)即重置採納新值。
_tick_last = 0        # 上次取樣 ticks_ms
_tick_carry = 0       # 累積毫秒(滿 1000 扣一次,餘數留用)
_tick_armed = False   # 已初始化(避免首輪以 0 當基準)
_tick_seq = 0         # 上次見到的 _display_time_seq(變更=外部送了新幀)
_tick_raw = 0         # 上次採納的 bus 原始值(bus 值改變 → 重新開始)
_tick_val = 0         # 目前倒數顯示值(本地遞減)
_tick_running = False # 是否倒數中
# 本機值(操作即時回饋,不依賴 echo)+ pending 標記
_local_mode = 0        # 本機最後送出/採納的 mode byte
_local_bright = 0      # 本機最後送出/採納的亮度
_pend_mode = False     # mode 已送出、等 echo 定案
_pend_bright = False   # brightness 已送出、等 echo 定案


@register(id="control_panel", title="控制面板", icon="sliders-horizontal",
          desc="模式·亮度·計時", order=1, accent=0xF9AB00)
def build():
    global scr, _mode_list, _mode_btns
    global _bright_sl, _bright_lb, _time_lb, _time_arc, _run_lb
    global _bit7_led, _bit6_led, _last_txt
    global _local_mode, _local_bright, _pend_mode, _pend_bright
    _last_txt = {}
    _tick_reset()
    nav.reset()
    _init_local()

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    # 左欄:模式清單(lv_list,字體放大用 F_NUM_M)
    lx, lw = 4, 96
    rx = lx + lw + 4
    rw = u.W - 4 - rx
    _mode_list, _mode_btns = u.mk_list(scr, lx, 4, lw, u.H - 8, MODE_LABELS,
                                       font=u.F_NUM_M)
    nav.add(_mode_list, ITEM_LIST, on_change=_sel_mode_delta)

    # 右欄上:亮度(無邊框卡片,省空間)
    c2 = _panel(scr, rx, 4, rw, 50)
    u.mk_label(c2, "亮度", 6, 4, u.TEXT2, u.ZH)
    _bright_lb = lv.label(c2)
    _bright_lb.align(lv.ALIGN.TOP_RIGHT, -6, 4)
    _bright_lb.set_style_text_font(u.F_NUM_M, 0)
    _bright_lb.set_style_text_color(u.C(u.PRIMARY), 0)
    _bright_lb.set_text("0")
    _bright_sl = u.mk_slider(c2, 6, 30, rw - 12, 0, 36, 0)
    nav.add(_bright_sl, ITEM_SLIDER, on_change=_adj_bright)

    # 右欄下:倒數時間 + 模式旗標 + 快捷(合併一區)
    #   上半:大 arc 置中 + 中央時間(F_NUM_XL)
    #   下排:狀態 / LED+切換 / 上一個下一個模式
    c3 = _panel(scr, rx, 58, rw, u.H - 4 - 58)
    # arc 置中上方
    arc_sz = 110
    arc_x = rw // 2 - arc_sz // 2
    arc_y = 6
    _time_arc = u.mk_arc(c3, arc_x, arc_y, arc_sz, u.PRIMARY, lo=0, hi=_TIME_MAX)
    _time_arc.set_value(_TIME_MAX)
    _time_lb = lv.label(c3)
    _time_lb.set_pos(arc_x + arc_sz // 2 - 34, arc_y + arc_sz // 2 - 14)
    _time_lb.set_style_text_font(u.F_NUM_XL, 0)
    _time_lb.set_style_text_color(u.C(u.TEXT), 0)
    _time_lb.set_text("00:00")
    # 狀態(arc 右上角)
    _run_lb = lv.label(c3)
    _run_lb.align(lv.ALIGN.TOP_RIGHT, -6, 8)
    _run_lb.set_style_text_font(u.F_NUM_M, 0)
    _run_lb.set_style_text_color(u.C(u.TEXT3), 0)
    _run_lb.set_text("待機")

    # 下排:左 LED+標籤按鈕(拍攝/可動,點擊切換)、右 模式快捷(▲▼)
    row_y = arc_y + arc_sz + 8
    # 拍攝(Bit7):LED + 按鈕(按鈕帶文字,點擊切換)
    _bit7_led = u.mk_led(c3, 8, row_y + 4, 12, on=False)
    btn7 = u.mk_btn(c3, "拍攝", 26, row_y, 56, 22, "secondary")
    nav.add(btn7, ITEM_BUTTON, on_change=_toggle_bit7)
    # 可動(Bit6)
    _bit6_led = u.mk_led(c3, 8, row_y + 30, 12, on=False)
    btn6 = u.mk_btn(c3, "可動", 26, row_y + 26, 56, 22, "secondary")
    nav.add(btn6, ITEM_BUTTON, on_change=_toggle_bit6)
    # 模式快捷(右側:上下排列,對齊 list 上下選的語意)
    btn_up = u.mk_btn(c3, "▲", rw - 40, row_y, 34, 22, "primary")
    nav.add(btn_up, ITEM_BUTTON, on_change=lambda: _sel_mode_delta(-1))
    btn_dn = u.mk_btn(c3, "▼", rw - 40, row_y + 26, 34, 22, "primary")
    nav.add(btn_dn, ITEM_BUTTON, on_change=lambda: _sel_mode_delta(1))

    u.fade_in(_mode_list, dy=5, time_ms=280, delay_ms=40)
    u.fade_in(c2, dy=5, time_ms=280, delay_ms=120)
    u.fade_in(c3, dy=5, time_ms=280, delay_ms=200)

    _sync_list()
    nav.paint()
    return scr


def _init_local():
    """以 bus 現值初始化本機值(啟動時 echo 即為實際狀態,無 pending)。"""
    global _local_mode, _local_bright, _pend_mode, _pend_bright
    from lib.sys.sys_bus import bus
    _local_mode = int(bus.shared.get("_display_mode", 0)) & 0xFF
    _local_bright = max(0, min(36, int(bus.shared.get("_display_brightness", 0))))
    _pend_mode = False
    _pend_bright = False


def _panel(parent, x, y, w, h):
    """輕量分區:無邊框、淡底色、pad 0(比 mk_card 省邊框空間)。"""
    c = lv.obj(parent)
    c.set_size(w, h)
    c.set_pos(x, y)
    c.set_style_bg_color(u.C(u.SURFACE), 0)
    c.set_style_radius(8, 0)
    c.set_style_border_width(0, 0)
    c.set_style_pad_all(0, 0)
    c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return c


# ═══ bus 讀寫(與 action_task_1 共享 _display_* 欄位) ═══

def _send_cmd(mode=None, brightness=None):
    """發指令給 action_task_1 via bus.shared['_display_cmd']。
    同板直寫；跨板時由 waiting_to_trash_actions.on_ctl 翻譯進同一欄位。
    action_task_1._consume_display_cmd() 統一消費 → set_display_state() → UART 執行。"""
    from lib.sys.sys_bus import bus
    cmd = {}
    if mode is not None:
        cmd["mode"] = int(mode) & 0xFF
    if brightness is not None:
        cmd["brightness"] = int(brightness)
    if cmd:
        bus.shared["_display_cmd"] = cmd


def _echo_mode():
    """讀 bus 的 _display_mode 回傳(被控制端/跨板 echo)。
    欄位不存在(尚未有回覆)→ 回 None,不得視為已確認。"""
    from lib.sys.sys_bus import bus
    if "_display_mode" not in bus.shared:
        return None
    return int(bus.shared["_display_mode"]) & 0xFF


def _mode_byte():
    """本機 mode byte(最後送出/採納值,不依賴 echo)。"""
    return _local_mode


def _set_mode_byte(v):
    """更新本機 mode + 送指令,標 pending(等 echo 定案後回藍/還原)。
    順手同步 list 高亮與 LED → 點擊當下立刻顯示琥珀。"""
    global _local_mode, _pend_mode
    _local_mode = int(v) & 0xFF
    _pend_mode = True
    _send_cmd(mode=_local_mode)
    _sync_list()
    _refresh_bits()


def _state():
    """本頁自用快取 dict(非協議欄位,如 is_running)。"""
    from lib.sys.sys_bus import bus
    s = bus.shared.get("control_panel")
    if not isinstance(s, dict):
        s = {}
        bus.shared["control_panel"] = s
    return s


def _sync_list():
    """依本機 mode byte 低 6 bit 同步 list 選中高亮。
    編輯態或 pending → 琥珀(已送出/移動中);其餘 → 主題藍(選中)。"""
    cur = _mode_byte() & _MODE_MASK
    cur = cur % len(MODE_LABELS)
    if nav.is_editing() or _pend_mode:
        color = u.WARNING
    else:
        color = u.PRIMARY
    u.list_select(_mode_btns, cur, color=color)


def _sel_mode_delta(dd):
    """編輯態 enc:上下移模式選擇(只改低 6 bit,保留旗標)。"""
    mb = _mode_byte()
    flags = mb & ~_MODE_MASK
    val = (mb & _MODE_MASK) + dd
    val = val % len(MODE_LABELS)
    _set_mode_byte(flags | val)
    _sync_list()


def _toggle_bit7():
    mb = _mode_byte()
    _set_mode_byte(mb ^ _BIT7)
    _refresh_bits()


def _toggle_bit6():
    mb = _mode_byte()
    _set_mode_byte(mb ^ _BIT6)
    _refresh_bits()


def _refresh_bits():
    """LED 三態:pending 時以本機送出值顯示(亮=琥珀等回覆);
    無 pending 以 echo 為準(亮=綠已確認;echo 尚未回 → 灰)。"""
    mb = _local_mode
    echo = _echo_mode()
    for led, flag in ((_bit7_led, _BIT7), (_bit6_led, _BIT6)):
        if _pend_mode:
            u.led_set(led, bool(mb & flag), on_color=u.WARNING)
        else:
            on = bool(echo & flag) if echo is not None else False
            u.led_set(led, on, on_color=u.SUCCESS)


def _adj_bright(dd):
    """編輯態 enc:調亮度。更新本機值+立即顯示,送指令標 pending 等回覆。"""
    global _local_bright, _pend_bright
    v = max(0, min(36, _local_bright + dd))
    _local_bright = v
    _pend_bright = True
    _bright_sl.set_value(v, 0)
    _bright_lb.set_text(str(v))
    _send_cmd(brightness=v)


# ====== 頁面接口(轉發給 nav) ======

def on_enter(): pass

def on_leave():
    if nav.is_editing():
        nav.exit()
        _sync_list()

def on_enc(d):
    nav.enc(d)
    if nav.current_kind() == ITEM_LIST:
        _sync_list()

def on_confirm():
    nav.confirm()
    _sync_list()
    return None

def on_exit():
    consumed = nav.exit()
    _sync_list()
    return consumed

def _tick_reset():
    """重設倒數追蹤(進入頁面時),下次 update 以 bus 現值重新開始。"""
    global _tick_last, _tick_carry, _tick_armed, _tick_seq, _tick_raw, _tick_val, _tick_running
    _tick_last = 0
    _tick_carry = 0
    _tick_armed = False
    _tick_seq = 0
    _tick_raw = 0
    _tick_val = 0
    _tick_running = False


def _tick_countdown(bus):
    """本地非阻塞倒數(不 sleep、不中斷主迴圈):
    每輪取樣 ticks_ms 與上次取樣對比,累積滿 1000ms 就 -1,
    餘數留在 _tick_carry 給下次 → 每幀長短不一也維持精準 1s/秒。
    _display_time_seq 一變(收到新幀,即使值相同 = 重新計時指令)即重置採納。"""
    global _tick_last, _tick_carry, _tick_armed, _tick_seq, _tick_raw, _tick_val, _tick_running
    raw = int(bus.shared.get("_display_time", 0))
    raw = max(0, min(_TIME_MAX, raw))
    seq = int(bus.shared.get("_display_time_seq", 0))
    now = time.ticks_ms()

    if not _tick_armed or seq != _tick_seq or raw != _tick_raw:
        # 收到新幀(seq 變)或值變:採納新值重新開始(同值重送也重置)
        _tick_last = now
        _tick_carry = 0
        _tick_armed = True
        _tick_seq = seq
        _tick_raw = raw
        _tick_val = raw
        _tick_running = bool(_state().get("is_running", raw > 0))
        return raw

    # 同值持續(本地倒數中,外部未再送新幀):累積時差扣減
    _tick_carry += time.ticks_diff(now, _tick_last)
    _tick_last = now
    if _tick_running and _tick_val > 0:
        while _tick_carry >= 1000:
            _tick_carry -= 1000
            _tick_val -= 1
            if _tick_val <= 0:
                _tick_carry = 0
                _tick_running = False
                break
    return max(0, _tick_val)


def update(run):
    if run % 10 != 0:
        return
    try:
        from lib.sys.sys_bus import bus
        # ── mode echo 採納:有 pending 等回覆,一致即確認轉綠;無 pending 才採納外部值 ──
        _sync_echo_mode(bus)
        _sync_echo_bright(bus)
        # 旗標
        _refresh_bits()
        # 倒數時間(arc + 文字):本地非阻塞倒數
        t = _tick_countdown(bus)
        mtxt = "{:02d}:{:02d}".format(*divmod(t, 60))
        if _last_txt.get("time") != mtxt:
            _last_txt["time"] = mtxt
            _time_lb.set_text(mtxt)
        try:
            _time_arc.set_value(t, 0)
        except Exception:
            pass
        # 計時狀態(與倒數同一狀態源)
        r = _tick_running
        rtxt = "計時中" if r else "待機"
        if _last_txt.get("run") != rtxt:
            _last_txt["run"] = rtxt
            _run_lb.set_text(rtxt)
            _run_lb.set_style_text_color(u.C(u.SUCCESS if r else u.TEXT3), 0)
    except Exception:
        pass


def _sync_echo_mode(bus):
    """mode echo 定案:echo 出現即為最終答案。
    一致 → 成功(pending 清,列表回藍停在該位置)。
    不一致 → 超時/未成功(採納 echo 還原,pending 清,列表回藍在原 echo 位置)。
    echo 欄位不存在 → 尚未有回覆,維持琥珀。"""
    global _local_mode, _pend_mode
    echo = _echo_mode()
    if echo is None:
        _refresh_bits()
        return
    _pend_mode = False
    if echo != _local_mode:
        _local_mode = echo
    _sync_list()
    _refresh_bits()


def _sync_echo_bright(bus):
    """亮度 echo 定案(同 mode):出現即為最終答案,一致保留、不一致還原;
    欄位不存在 → 維持琥珀。顯示一律以本機值為準。"""
    global _local_bright, _pend_bright
    if "_display_brightness" not in bus.shared:
        return
    echo = int(bus.shared["_display_brightness"])
    _pend_bright = False
    if echo != _local_bright and 0 <= echo <= 36:
        _local_bright = echo
    if _bright_sl.get_value() != _local_bright:
        _bright_sl.set_value(_local_bright, 0)
        _bright_lb.set_text(str(_local_bright))
