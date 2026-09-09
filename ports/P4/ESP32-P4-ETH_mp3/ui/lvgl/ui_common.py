# ui/lvgl/ui_common.py — 共用 UI 層(palette / 字型 / widget builder)
#
# 移植自 mp_LVGL/ui/lvgl_ui_common.py,差異:
#   - 字型路徑改 /ui/lvgl/src/zh_hant_16.bin(slave new 佈局)
#   - W/H 不寫死:由 init(plat) 注入(lvgl_init 從 bus 讀 tft_width/tft_height)
#   - 加 sw_set/sw_get wrapper(binding 版本差異防護)
#
# 注意(對應 DEV_NOTES 踩坑):
#   - 字體用 getattr fallback(binding 沒編到的尺寸自動降級)
#   - 枚舉常數能用整數就用整數(soft reboot 後常數可能不穩)
#   - 所有容器 pad_all(0) + 移除 SCROLLABLE(預設樣式會干擾佈局)
import lvgl as lv

# ====== 版面(由 init 注入;fallback 240×320 直向) ======
W = 240
H = 320

# ====== Palette(來自 colors_and_type.css) ======
BG       = 0xF5F5F5
SURFACE  = 0xFFFFFF
BORDER   = 0xE0E0E0
TEXT     = 0x1F1F1F
TEXT2    = 0x5F5F5F
TEXT3    = 0x8F8F8F
PRIMARY  = 0x1A73E8
SUCCESS  = 0x188038
WARNING  = 0xF9AB00
DANGER   = 0xD93025
TRACK    = 0xDADCE0
FOCUS_BG = 0xE8F0FE
DANGER_BG = 0xFCE8E6


def init(plat):
    """注入螢幕尺寸(lvgl_init.LvglDisp 的 W/H,從 bus 讀)。"""
    global W, H
    W = getattr(plat, "W", 240)
    H = getattr(plat, "H", 320)


# ====== 字體 ======
ZH = None

def init_fonts():
    """在 lv.init() 完成後呼叫,載入繁中 .bin 字體。
    slave new 佈局:/ui/lvgl/src/zh_hant_16.bin"""
    global ZH
    if ZH:
        return
    try:
        with open("/ui/lvgl/src/zh_hant_16.bin", "rb") as fp:
            buf = fp.read()
        print("[font] read {} bytes".format(len(buf)))
        f = None
        if hasattr(lv, "binfont_create_from_buffer"):
            try:
                f = lv.binfont_create_from_buffer(bytearray(buf), len(buf))
            except TypeError:
                try:
                    f = lv.binfont_create_from_buffer(bytearray(buf))
                except Exception as e2:
                    print("[font] from_buffer(1arg) fail:", e2)
            except Exception as e1:
                print("[font] from_buffer(2arg) fail:", e1)
        else:
            print("[font] binfont_create_from_buffer NOT in binding")
        if f:
            ZH = f
            print("[font] loaded from buffer OK")
            return
        print("[font] from_buffer returned None")
    except Exception as _e:
        print("[font] buffer load fail:", _e)
    ZH = getattr(lv, "font_simsun_16_cjk", None)
    print("[font] fallback:", ZH)

_BASE_FONT = None
for _n in ("font_montserrat_14", "font_montserrat_16", "font_montserrat_12",
           "font_montserrat_18", "font_montserrat_20"):
    _BASE_FONT = getattr(lv, _n, None)
    if _BASE_FONT:
        break

# ====== 圖示字體(src/lv_icons.py) ======
_icon_font = None

def _icon_font_ready():
    global _icon_font
    if _icon_font is None:
        try:
            from lv_icons import load_icon_font
            _icon_font = load_icon_font()
        except Exception as e:
            print("[icons] skip:", e)
            _icon_font = False
    return _icon_font or None

def mk_icon(parent, name, x, y, color=TEXT2):
    f = _icon_font_ready()
    if f is None:
        return None
    from lv_icons import ICONS
    if name not in ICONS:
        return None
    lb = lv.label(parent)
    lb.set_text(ICONS[name])
    lb.set_pos(x, y)
    lb.set_style_text_color(C(color), 0)
    lb.set_style_text_font(f, 0)
    return lb

# ====== 動效 helper(src/lv_ui_fx.py) ======
try:
    from lv_ui_fx import pulse as _fx_pulse, fade_in as _fx_fade_in
except Exception:
    _fx_pulse = _fx_fade_in = None

def pulse(wid, period_ms=1500, min_opa=110, max_opa=255):
    if _fx_pulse:
        return _fx_pulse(wid, period_ms, min_opa, max_opa)
    return None

def fade_in(wid, dy=6, time_ms=300, delay_ms=0):
    if _fx_fade_in:
        return _fx_fade_in(wid, dy, time_ms, delay_ms)
    return None

def font(*names):
    for n in names:
        f = getattr(lv, n, None)
        if f:
            return f
    return _BASE_FONT

F_NUM_L = font("font_montserrat_22", "font_montserrat_20", "font_montserrat_18")
F_NUM_M = font("font_montserrat_16", "font_montserrat_14")
F_NUM_S = font("font_montserrat_12", "font_montserrat_10")
F_NUM_XL = font("font_montserrat_28", "font_montserrat_26", "font_montserrat_24",
                "font_montserrat_22", "font_montserrat_20")

# ====== 基礎 builder ======

def C(hexval):
    return lv.color_hex(hexval)

def mk_label(parent, text, x, y, color=TEXT, f=None):
    lb = lv.label(parent)
    lb.set_text(text)
    lb.set_pos(x, y)
    lb.set_style_text_color(C(color), 0)
    if f:
        lb.set_style_text_font(f, 0)
    elif ZH:
        lb.set_style_text_font(ZH, 0)
    return lb

def mk_card(parent, x, y, w, h):
    c = lv.obj(parent)
    c.set_size(w, h)
    c.set_pos(x, y)
    c.set_style_bg_color(C(SURFACE), 0)
    c.set_style_radius(10, 0)
    c.set_style_border_color(C(BORDER), 0)
    c.set_style_border_width(1, 0)
    c.set_style_pad_all(0, 0)
    c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return c

def mk_appbar(scr, title, right=""):
    """頂欄 36px:返回符號 + 標題 + 右側狀態。"""
    bar = lv.obj(scr)
    bar.set_size(W, 36)
    bar.set_pos(0, 0)
    bar.set_style_bg_color(C(SURFACE), 0)
    bar.set_style_radius(0, 0)
    bar.set_style_border_color(C(BORDER), 0)
    bar.set_style_border_width(1, 0)
    bar.set_style_pad_all(0, 0)
    bar.remove_flag(lv.obj.FLAG.SCROLLABLE)
    back = mk_icon(bar, "chevron-left", 8, 9, TEXT2)
    if back is None:
        mk_label(bar, "<", 10, 9, TEXT2, F_NUM_M)
    mk_label(bar, title, 28, 9, TEXT, ZH)
    r = None
    if right:
        r = mk_label(bar, right, 0, 0, TEXT3, F_NUM_S)
        r.align(lv.ALIGN.RIGHT_MID, -10, 0)
    return bar, r

def mk_btn(parent, text, x, y, w, h, kind="primary"):
    b = lv.button(parent)
    b.set_size(w, h)
    b.set_pos(x, y)
    if kind == "primary":
        b.set_style_bg_color(C(PRIMARY), 0)
        b.set_style_border_width(0, 0)
        fg = 0xFFFFFF
    elif kind == "danger":
        b.set_style_bg_color(C(DANGER_BG), 0)
        b.set_style_border_width(0, 0)
        fg = DANGER
    else:
        b.set_style_bg_color(C(SURFACE), 0)
        b.set_style_border_color(C(BORDER), 0)
        b.set_style_border_width(1, 0)
        fg = TEXT2
    b.set_style_radius(8, 0)
    lb = lv.label(b)
    lb.set_text(text)
    lb.align(lv.ALIGN.CENTER, 0, 0)
    lb.set_style_text_color(C(fg), 0)
    if ZH:
        lb.set_style_text_font(ZH, 0)
    return b

def mk_slider(parent, x, y, w, lo, hi, val, color=PRIMARY):
    s = lv.slider(parent)
    s.set_size(w, 8)
    s.set_pos(x, y)
    s.set_range(lo, hi)
    s.set_value(val, 0)
    s.set_style_bg_color(C(TRACK), lv.PART.MAIN)
    s.set_style_radius(4, lv.PART.MAIN)
    s.set_style_bg_color(C(color), lv.PART.INDICATOR)
    s.set_style_radius(4, lv.PART.INDICATOR)
    s.set_style_bg_color(C(color), lv.PART.KNOB)
    s.set_style_radius(8, lv.PART.KNOB)
    s.set_style_pad_all(4, lv.PART.KNOB)
    return s

def mk_arc(parent, x, y, size, color, lo=0, hi=100):
    """環形進度量表(不可調整,knob 隱藏)。"""
    a = lv.arc(parent)
    a.set_size(size, size)
    a.set_pos(x, y)
    a.set_range(lo, hi)
    a.set_value(hi)
    a.set_style_arc_width(6, lv.PART.MAIN)
    a.set_style_arc_color(C(TRACK), lv.PART.MAIN)
    a.set_style_arc_width(6, lv.PART.INDICATOR)
    a.set_style_arc_color(C(color), lv.PART.INDICATOR)
    # 隱藏 knob
    a.set_style_arc_opa(0, lv.PART.KNOB)
    a.set_style_bg_opa(0, lv.PART.KNOB)
    a.set_style_outline_width(0, lv.PART.KNOB)
    a.remove_flag(lv.obj.FLAG.CLICKABLE)
    return a

def mk_switch(parent, x, y, on=False, color=PRIMARY):
    s = lv.switch(parent)
    s.set_size(44, 24)
    s.set_pos(x, y)
    s.set_style_bg_color(C(TRACK), lv.PART.MAIN)
    s.set_style_radius(12, lv.PART.MAIN)
    s.set_style_bg_color(C(color), lv.PART.INDICATOR)
    s.set_style_radius(12, lv.PART.INDICATOR)
    s.set_style_bg_color(C(SURFACE), lv.PART.KNOB)
    s.set_style_radius(10, lv.PART.KNOB)
    s.set_style_shadow_width(0, lv.PART.KNOB)
    if on:
        sw_set(s, True)
    return s

# ====== switch state 防護 wrapper(binding 版本差異) ======
def _state_const():
    try:
        return lv.STATE.CHECKED
    except Exception:
        return 1

def sw_set(sw, on):
    """設定 switch 開/關。跨 binding 防護(add_state/clear_state/add_flag/clear_flag)。"""
    chk = _state_const()
    if on:
        for m in ("add_state", "add_flag"):
            fn = getattr(sw, m, None)
            if fn is not None:
                try:
                    fn(chk if m == "add_state" else getattr(lv.obj.STATE, "CHECKED", 0x1000))
                    return
                except TypeError:
                    try:
                        fn(); return
                    except Exception:
                        continue
                except Exception:
                    continue
    else:
        for m in ("clear_state", "clear_flag"):
            fn = getattr(sw, m, None)
            if fn is not None:
                try:
                    fn(chk if m == "clear_state" else getattr(lv.obj.STATE, "CHECKED", 0x1000))
                    return
                except TypeError:
                    try:
                        fn(); return
                    except Exception:
                        continue
                except Exception:
                    continue

def sw_get(sw):
    """讀 switch 是否開。跨 binding 防護(has_state/get_state)。"""
    for m in ("has_state", "get_state"):
        fn = getattr(sw, m, None)
        if fn is not None:
            try:
                return bool(fn(_state_const()))
            except TypeError:
                try:
                    return bool(fn())
                except Exception:
                    continue
            except Exception:
                continue
    return False

# ====== 焦點視覺 ======
def set_focus(wid, on, editing=False):
    if on:
        wid.set_style_outline_color(C(WARNING if editing else PRIMARY), 0)
        wid.set_style_outline_width(3, 0)
        wid.set_style_outline_pad(3, 0)
    else:
        wid.set_style_outline_width(0, 0)


# ====== list helper ======
# 用 lv.list 顯示文字清單;選中項用手動背景色標示(不依賴 LVGL indev group)。

def mk_list(parent, x, y, w, h, items, font=None):
    """建立 lv.list 並填入文字項。回傳 (list_widget, [按鈕widgets])。
    items: 文字清單 list[str]。font: 指定字體(預設用 ZH)。"""
    lst = lv.list(parent)
    lst.set_size(w, h)
    lst.set_pos(x, y)
    f = font or ZH
    btns = []
    for txt in items:
        b = lst.add_text(txt)
        if f:
            try:
                b.set_style_text_font(f, 0)
            except Exception:
                pass
        btns.append(b)
    return lst, btns


def mk_led(parent, x, y, size=12, on=False, on_color=SUCCESS, off_color=BORDER):
    """LED 方格:亮(on_color)/滅(off_color)。回傳 widget,用 led_set() 切換。"""
    d = lv.obj(parent)
    d.set_size(size, size)
    d.set_pos(x, y)
    d.set_style_radius(size // 2, 0)
    d.set_style_bg_color(C(on_color if on else off_color), 0)
    d.set_style_border_width(0, 0)
    d.set_style_pad_all(0, 0)
    d.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return d


def led_set(led, on, on_color=SUCCESS, off_color=BORDER):
    """切換 LED 亮/滅。"""
    try:
        led.set_style_bg_color(C(on_color if on else off_color), 0)
    except Exception:
        pass


def list_select(btns, idx, color=PRIMARY):
    """標示 list 第 idx 項為選中(背景色);其他項清除。
    color: PRIMARY(選中) / WARNING(編輯中/已送出等回覆) / SUCCESS(已確認)。"""
    n = len(btns)
    if n == 0:
        return
    idx = max(0, min(idx, n - 1))
    for i, b in enumerate(btns):
        try:
            if i == idx:
                b.set_style_bg_color(C(color), 0)
                b.set_style_text_color(C(0xFFFFFF), 0)
            else:
                b.set_style_bg_color(C(BG), 0)
                b.set_style_text_color(C(TEXT), 0)
        except Exception:
            pass


