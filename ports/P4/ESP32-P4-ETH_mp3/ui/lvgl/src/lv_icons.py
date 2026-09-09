# lv_icons.py — 圖示 helper（由 LVGL UI Asset Studio 產生）
#
# 使用:
#   from lv_icons import ICONS, load_icon_font, mk_icon
#   ic = mk_icon(parent, "thermometer", x, y, color=0x1F1F1F)
#
# 板上需有 /ui/lvgl/src/icons_16.bin。
import lvgl as lv

_ICON = None
_FONT_FILE = "/ui/lvgl/src/icons_16.bin"

# lucide 圖示名 → 字符（Material Symbols Rounded）
ICONS = {
    "activity": "\uF190",
    "alert-triangle": "\uF083",
    "battery-full": "\uE1A5",
    "chevron-down": "\uE313",
    "chevron-left": "\uE5CB",
    "chevron-right": "\uE5CC",
    "clock": "\uEFD6",
    "droplets": "\uE798",
    "fan": "\uF168",
    "flame": "\uEF55",
    "gauge": "\uE9E4",
    "info": "\uE88E",
    "layout-dashboard": "\uE871",
    "lightbulb": "\uE90F",
    "play": "\uE037",
    "power": "\uF8C7",
    "refresh-cw": "\uE5D5",
    "save": "\uE161",
    "sensors": "\uE51E",
    "settings": "\uE8B8",
    "shield": "\uE9E0",
    "sliders-horizontal": "\uE429",
    "square": "\uE3C6",
    "sun": "\uE518",
    "thermometer": "\uE1FF",
    "trending-up": "\uE8E5",
    "wifi": "\uE63E",
    "wind": "\uEFD8",
    "zap": "\uEA0B",
}


def load_icon_font():
    """載入 icon 字體（lf binfont,需在 lv.init() 之後呼叫）。"""
    global _ICON
    if _ICON is not None:
        return _ICON
    with open(_FONT_FILE, "rb") as fp:
        buf = fp.read()
    if hasattr(lv, "binfont_create_from_buffer"):
        try:
            _ICON = lv.binfont_create_from_buffer(bytearray(buf), len(buf))
        except TypeError:
            _ICON = lv.binfont_create_from_buffer(bytearray(buf))
    if _ICON is None:
        raise RuntimeError("icons_16.bin 載入失敗")
    print("[icons] loaded", len(ICONS), "icons")
    return _ICON


def mk_icon(parent, name, x, y, color=0x1F1F1F):
    """建立一個圖示 label。name 為 ICONS 的鍵名。"""
    if name not in ICONS:
        raise KeyError("unknown icon: " + str(name))
    lb = lv.label(parent)
    lb.set_text(ICONS[name])
    lb.set_pos(x, y)
    lb.set_style_text_color(lv.color_hex(color), 0)
    lb.set_style_text_font(load_icon_font(), 0)
    return lb
