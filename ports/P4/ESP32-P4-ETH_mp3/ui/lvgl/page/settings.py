# ui/lvgl/page/settings.py — 系統設定頁(橫屏 320×240)
#
# 導覽用共用 nav helper:enc 選 Wi-Fi 開關 → confirm 切換。
# update() 自己讀 bus:wifi_enable / hostname / master_IP / wifi_ssid / slave_id / mac
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_SWITCH

# 唯讀字串欄位:(顯示標籤, 讀取函式)
_STR_FIELDS = [
    ("主機名",   lambda: _sys_str("hostname")),
    ("Master",  lambda: "{}:{}".format(_sys_str("master_IP"),
                                      _sys_cfg().get("master_port", 0))),
    ("Wi-Fi",   lambda: _wifi_str("ssid")),
    ("裝置 ID",  lambda: _sid()),
    ("MAC",     lambda: _mac()),
]

nav = Nav()
scr = None
_wifi_sw = None
_str_lbs = []
_last_txt = {}


@register(id="settings", title="系統設定", icon="settings",
          desc="網路·裝置資訊", order=3, accent=0x7F8C8D)
def build():
    global scr, _wifi_sw, _str_lbs, _last_txt
    _str_lbs = []
    _last_txt = {}
    nav.reset()

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    # Wi-Fi 開關(左上)
    c1 = u.mk_card(scr, 12, 6, 148, 48)
    u.mk_icon(c1, "wifi", 10, 12, u.TEXT2)
    u.mk_label(c1, "Wi-Fi 啟用", 32, 14, u.TEXT, u.ZH)
    _wifi_sw = u.mk_switch(c1, 100, 12, on=False)
    nav.add(_wifi_sw, ITEM_SWITCH, on_change=_toggle_wifi)

    # 裝置資訊(唯讀字串列表):左欄 i<3(Wi-Fi 卡下方),右欄 i>=3(頂部)
    for i, (label, _fn) in enumerate(_STR_FIELDS):
        col_x = 12 if i < 3 else 166
        col_y = 62 + (i % 3) * 42 if i < 3 else 6 + (i - 3) * 42
        c = u.mk_card(scr, col_x, col_y, 148 if i < 3 else u.W - 24 - 154, 38)
        u.mk_label(c, label, 8, 10, u.TEXT2, u.ZH)
        lb = lv.label(c)
        lb.set_pos(8, 22)
        lb.set_style_text_color(u.C(u.TEXT), 0)
        if u.ZH:
            lb.set_style_text_font(u.ZH, 0)
        lb.set_text("—")
        _str_lbs.append(lb)

    nav.paint()
    return scr


def _sys_cfg():
    from lib.sys.sys_bus import bus
    return bus.shared.get("System", {}) or {}

def _sys_str(key):
    return str(_sys_cfg().get(key, "") or "—")

def _wifi_str(key):
    from lib.sys.sys_bus import bus
    return str(bus.shared.get("Network", {}).get("wifi", {}).get(key, "") or "—")

def _sid():
    from lib.sys.sys_bus import bus
    return str(bus.slave_id or "—")

def _mac():
    sid = _sid()
    if len(sid) >= 12:
        return ":".join(sid[i:i + 2] for i in range(0, 12, 2))
    return sid


def _toggle_wifi():
    """confirm 在 Wi-Fi 開關:切換。"""
    new = not u.sw_get(_wifi_sw)
    u.sw_set(_wifi_sw, new)
    print("[settings] wifi_enable -> {}".format("ON" if new else "OFF"))


def on_enter(): pass
def on_leave():
    if nav.is_editing():
        nav.exit()

def on_enc(d):
    nav.enc(d)

def on_confirm():
    nav.confirm()
    return None

def on_exit():
    return nav.exit()

def update(run):
    if run % 20 != 0:
        return
    try:
        from lib.sys.sys_bus import bus
        wenable = int(bus.shared.get("Network", {}).get("wifi", {}).get("enable", 0))
        if u.sw_get(_wifi_sw) != bool(wenable):
            u.sw_set(_wifi_sw, bool(wenable))
        for i, (label, fn) in enumerate(_STR_FIELDS):
            if i < len(_str_lbs):
                txt = str(fn())
                if _last_txt.get(i) != txt:
                    _last_txt[i] = txt
                    _str_lbs[i].set_text(txt)
    except Exception:
        pass
