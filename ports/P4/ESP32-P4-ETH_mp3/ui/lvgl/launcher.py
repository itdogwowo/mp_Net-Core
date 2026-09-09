# ui/lvgl/launcher.py — 動態首頁(讀 registry 產生卡片)
#
# 橫屏 320×240 佈局:卡片橫向輪播(旋鈕左右選,按下確認)。
# 對齊 mp_LVGL/ui/lvgl_ui_launcher.py 的輪播風格。
import lvgl as lv
from ui.lvgl.registry import ordered
from ui.lvgl import ui_common as u

scr = None
cards = []
_focus = 0

CARD_W = 160
CARD_H = 140
CX = 0
STRIDE = 176
FOCUS_Y = 48
IDLE_Y = 56


def build():
    global scr, cards, _focus, CX
    _focus = 0
    CX = (u.W - CARD_W) // 2
    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)
    title = u.mk_label(scr, "選擇功能", 0, 8, u.TEXT, u.ZH)
    title.align(lv.ALIGN.TOP_MID, 0, 8)
    sub = u.mk_label(scr, "旋鈕切換 · 按下確認", 0, 28, u.TEXT3, u.ZH)
    sub.align(lv.ALIGN.TOP_MID, 0, 28)

    metas = ordered()
    cards = []
    for meta in metas:
        c = lv.obj(scr)
        c.set_size(CARD_W, CARD_H)
        c.set_style_bg_color(u.C(u.SURFACE), 0)
        c.set_style_radius(12, 0)
        c.set_style_border_color(u.C(u.BORDER), 0)
        c.set_style_border_width(1, 0)
        c.set_style_pad_all(0, 0)
        c.remove_flag(lv.obj.FLAG.SCROLLABLE)

        blk = lv.obj(c)
        blk.set_size(40, 40)
        blk.set_pos(14, 16)
        blk.set_style_bg_color(u.C(meta["accent"]), 0)
        blk.set_style_radius(8, 0)
        blk.set_style_border_width(0, 0)
        blk.set_style_pad_all(0, 0)
        blk.remove_flag(lv.obj.FLAG.SCROLLABLE)
        ic = u.mk_icon(blk, meta["icon"], 0, 0, 0xFFFFFF)
        if ic is not None:
            ic.align(lv.ALIGN.CENTER, 0, 0)
        num = u.mk_label(c, "{:02d}".format(meta["order"]), 0, 0, u.TEXT3, u.F_NUM_S)
        num.align(lv.ALIGN.TOP_RIGHT, -8, 8)

        u.mk_label(c, meta["title"], 14, 68, u.TEXT, u.ZH)
        u.mk_label(c, meta["desc"], 14, 92, u.TEXT3, u.ZH)
        cards.append(c)

    # 分頁指示點
    n = len(cards)
    x0 = (u.W - (n * 8 + (n - 1) * 6)) // 2
    for i in range(n):
        d = lv.obj(scr)
        d.set_size(8, 8)
        d.set_pos(x0 + i * 14, u.H - 40)
        d.set_style_radius(4, 0)
        d.set_style_bg_color(u.C(0xDADCE0), 0)
        d.set_style_border_width(0, 0)

    _layout()
    return scr


def _layout():
    n = len(cards)
    for i, c in enumerate(cards):
        rel = ((i - _focus + n + n // 2) % n) - n // 2
        x = CX + rel * STRIDE
        foc = rel == 0
        c.set_pos(x, FOCUS_Y if foc else IDLE_Y)
        c.set_style_opa(255 if foc else 160, 0)
        c.set_style_border_color(u.C(u.PRIMARY if foc else u.BORDER), 0)
        c.set_style_border_width(2 if foc else 1, 0)


def on_enc(d):
    global _focus
    n = len(cards)
    if n == 0:
        return
    _focus = (_focus + d) % n
    _layout()


def on_confirm():
    metas = ordered()
    if not metas:
        return None
    target = metas[_focus]["id"]
    print("[launcher] enter", target)
    return target


def on_exit():
    return False


def update(run):
    pass
