# ui/lvgl/page/pixel_controller.py — Pixel 模式控制器頁(橫屏 320×240)
#
# 佈局(複製 control_panel 左列表/右控制區的骨架,聚焦「模式設置」):
#   左欄(x=4,w=96):動態模式清單(從 gmode.mode_pool() 載入)。
#                   enc 選 list → confirm 進編輯 → enc 上下選 → 選中即送
#                   gmode.set_mode(id)。
#   右欄(x=104):
#     上  「獲取列表」按鈕(重新載入 gmode.mode_pool → 重建清單)
#     中  「可動」按鈕(鐵打模式 0x0200 = SERVO 組 mode 0,點擊直接設置;
#          取代原 control_panel 的 Bit6 可動 toggle)
#     下  當前模式顯示 + 亮度 slider(0-255,對應 MODE_SET.brightness)
#
# 協議對接(本頁是「本板 pixel 控制端」,只寫狀態不碰 gmode/hardware):
#   模式設置: bus.shared["_pixel_cmd"] = {"mode": 16-bit id}
#             → PixelControlPanelTask 消費 → gmode.set_mode() → PixelTask 執行。
#   模式清單: gmode.mode_pool() = pixel_maps(PixelTask 載入) + /audio/modes(純音效)。
#   亮度:     bus.shared["_pixel_cmd"] = {"brightness": 0-255} → task 寫 st_pixel。
import time
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_SLIDER, ITEM_BUTTON

# 鐵打可動模式:16-bit 內部 id = (mode_type<<8)|mode_id = 0x02<<8 | 0 = 0x0200
# mode_type=2 (SERVO)、mode_id=0。臨時應急:點擊「可動」直接設置此模式。
MOVABLE_ID = 0x0200

nav = Nav()
scr = None
_mode_list = None
_mode_btns = []
_mode_ids = []       # 有序 16-bit id(與 _mode_labels 一一對應)
_mode_labels = []    # 清單顯示文字
_cur_idx = 0         # 目前選中索引(選中即設置)
_bright_sl = _bright_lb = None
_cur_lb = None       # 目前模式顯示


@register(id="pixel_controller", title="Pixel 控制", icon="sliders-horizontal",
          desc="模式設置·列表·可動", order=2, accent=0x1A73E8)
def build():
    global scr, _mode_list, _mode_btns, _cur_idx
    global _bright_sl, _bright_lb, _cur_lb
    nav.reset()
    _cur_idx = 0
    _mode_ids = []
    _mode_labels = []

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    # 左欄:動態模式清單(初次先放一個空項,載入後重建)
    lx, lw = 4, 96
    rx = lx + lw + 4
    rw = u.W - 4 - rx
    _mode_list, _mode_btns = u.mk_list(scr, lx, 4, lw, u.H - 8, ["(載入中)"],
                                       font=u.F_NUM_M)
    nav.add(_mode_list, ITEM_LIST, on_change=_sel_mode_delta)

    # 右欄上:動作區(獲取列表 + 可動)
    c2 = _panel(scr, rx, 4, rw, 54)
    u.mk_label(c2, "動作", 6, 4, u.TEXT2, u.ZH)
    btn_fetch = u.mk_btn(c2, "獲取列表", 6, 26, 74, 22, "primary")
    nav.add(btn_fetch, ITEM_BUTTON, on_change=_fetch_modes)
    btn_mov = u.mk_btn(c2, "可動", 88, 26, 74, 22, "secondary")
    nav.add(btn_mov, ITEM_BUTTON, on_change=_apply_movable)

    # 右欄中:目前模式顯示
    c3 = _panel(scr, rx, 62, rw, 34)
    u.mk_label(c3, "目前", 6, 4, u.TEXT2, u.ZH)
    _cur_lb = lv.label(c3)
    _cur_lb.align(lv.ALIGN.RIGHT_MID, -6, 0)
    _cur_lb.set_style_text_font(u.F_NUM_M, 0)
    _cur_lb.set_style_text_color(u.C(u.PRIMARY), 0)
    _cur_lb.set_text("—")

    # 右欄下:亮度 slider(0-255,對應 MODE_SET.brightness)
    c4 = _panel(scr, rx, 100, rw, 50)
    u.mk_label(c4, "亮度", 6, 4, u.TEXT2, u.ZH)
    _bright_lb = lv.label(c4)
    _bright_lb.align(lv.ALIGN.TOP_RIGHT, -6, 4)
    _bright_lb.set_style_text_font(u.F_NUM_M, 0)
    _bright_lb.set_style_text_color(u.C(u.PRIMARY), 0)
    _bright_lb.set_text("255")
    _bright_sl = u.mk_slider(c4, 6, 28, rw - 12, 0, 255, 255)
    nav.add(_bright_sl, ITEM_SLIDER, on_change=_adj_bright)

    _load_modes(initial=True)
    nav.paint()
    return scr


def _panel(parent, x, y, w, h):
    """輕量分區(與 control_panel._panel 相同)。"""
    c = lv.obj(parent)
    c.set_size(w, h)
    c.set_pos(x, y)
    c.set_style_bg_color(u.C(u.SURFACE), 0)
    c.set_style_radius(8, 0)
    c.set_style_border_width(0, 0)
    c.set_style_pad_all(0, 0)
    c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return c


# ═══ 模式池載入 / 列表重建 ═══

def _get_gmode():
    from lib.sys.sys_bus import bus
    return bus.get_service("gmode")


def _mode_by_id(pool, mid):
    """從池裡取 mode dict(容忍 key 是 int 或 str)。"""
    m = pool.get(mid)
    if m is None:
        try:
            m = pool.get(str(mid))
        except Exception:
            m = None
    return m


def _load_modes(initial=False):
    """從 gmode.mode_pool() 載入模式清單,重建左欄列表。"""
    global _mode_ids, _mode_labels, _cur_idx
    from lib.sys.sys_bus import bus
    gmode = _get_gmode()
    if gmode is not None:
        try:
            pool = gmode.mode_pool()
        except Exception:
            pool = bus.shared.get("pixel_maps", {})
    else:
        pool = bus.shared.get("pixel_maps", {})

    ids = []
    try:
        ids = sorted(int(i) for i in pool.keys())
    except Exception:
        ids = []

    _mode_ids = ids
    _mode_labels = []
    for mid in ids:
        m = _mode_by_id(pool, mid) or {}
        name = m.get("name", "")
        if name:
            _mode_labels.append("0x{:04X} {}".format(mid, name))
        else:
            _mode_labels.append("0x{:04X}".format(mid))
    if not _mode_ids:
        _mode_labels = ["(無模式)"]
    _cur_idx = 0
    _refresh_list()
    if not initial:
        print("[PixelCtrl] 模式列表載入: {} 個".format(len(ids)))


def _refresh_list():
    """清空並重建左欄 lv.list 內容(保持同一 widget,nav 引用不變)。"""
    global _mode_btns, _cur_idx
    if _mode_list is None:
        return
    try:
        _mode_list.clean()
    except Exception:
        pass
    _mode_btns = []
    for txt in _mode_labels:
        try:
            b = _mode_list.add_text(txt)
        except Exception:
            continue
        if u.F_NUM_M:
            try:
                b.set_style_text_font(u.F_NUM_M, 0)
            except Exception:
                pass
        _mode_btns.append(b)
    _cur_idx = max(0, min(_cur_idx, max(0, len(_mode_btns) - 1)))
    _sync_list()


def _sync_list():
    """依目前選中索引高亮列表。"""
    if not _mode_btns:
        return
    u.list_select(_mode_btns, _cur_idx, color=u.PRIMARY)


# ═══ 動作 ═══

def _espnow_send_mid(mid):
    """最直接硬件發送:拿底層 espnow 物件直接 send(broadcast),不經任何 bus 封裝。
    點擊即發、重複也無妨。frame 印出完整 hex 供對照。"""
    import struct
    from lib.sys.sys_bus import bus
    from lib.sys.proto import Proto

    mid = int(mid) & 0xFFFF
    mode_type = (mid >> 8) & 0xFF
    mode_id = mid & 0xFF
    payload = struct.pack("<BBHB", mode_type, mode_id, 0, 0xFF)  # delay=0, bri=不設置
    frame = bytes(Proto.pack(0x3105, payload))

    # 拿底層 espnow 硬體物件(優先重用 app 已 active 的,避免 ESP_ERR_ESPNOW_EXIST)
    esp = None
    now = bus.get_service("NowBus")
    if now is not None and getattr(now, "_esp", None) is not None:
        esp = now._esp
    else:
        import espnow as _espnow
        import network as _net
        sta = _net.WLAN(_net.STA_IF)
        if not sta.active():
            sta.active(True)
        try:
            sta.config(channel=6)
        except Exception:
            pass
        esp = _espnow.ESPNow()
        esp.active(True)
        esp.add_peer(b"\xff\xff\xff\xff\xff\xff")

    bcast = b"\xff\xff\xff\xff\xff\xff"
    try:
        ok = esp.send(bcast, frame)
    except Exception as e:
        ok = False
        print("[PixelCtrl] ESP-NOW send 例外: {}".format(e))

    print("[PixelCtrl] ESP-NOW 0x3105 type={} id={} (0x{:04X}) ret={} frame={}".format(
        mode_type, mode_id, mid, ok, frame.hex()))
    return ok


def _set_mode(mid):
    """臨時:選中即發 ESP-NOW,不寫狀態、不經 task。"""
    _espnow_send_mid(mid)
    if _cur_lb is not None:
        try:
            _cur_lb.set_text("0x{:04X}".format(int(mid) & 0xFFFF))
        except Exception:
            pass


def _apply_movable():
    """「可動」按鈕:點擊即發 ESP-NOW MODE_SET 0x0200(SERVO 組 mode 0)。"""
    print("[PixelCtrl] 可動 → 0x{:04X}".format(MOVABLE_ID))
    _espnow_send_mid(MOVABLE_ID)


def _sel_mode_delta(dd):
    """列表編輯態 enc:上下移選中(移動即設置,對齊 control_panel 語意)。"""
    global _cur_idx
    if not _mode_ids:
        return
    _cur_idx = (_cur_idx + dd) % len(_mode_ids)
    _sync_list()
    _set_mode(_mode_ids[_cur_idx])


def _fetch_modes():
    """「獲取列表」按鈕:重新載入模式池。"""
    _load_modes(initial=False)


def _adj_bright(dd):
    """亮度 slider 編輯態 enc:0-255,寫 _pixel_cmd 由 task 套用(與 MODE_SET.brightness 同源)。"""
    from lib.sys.sys_bus import bus
    v = max(0, min(255, _bright_sl.get_value() + dd))
    _bright_sl.set_value(v, 0)
    _bright_lb.set_text(str(v))
    bus.shared["_pixel_cmd"] = {"brightness": v}


# ====== 頁面接口(轉發給 nav) ======

def on_enter():
    pass


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


def update(run):
    if run % 10 != 0:
        return
    # 本頁無需週期性拉取(模式清單由「獲取列表」手動觸發;目前模式在設置時即時顯示)
    pass
