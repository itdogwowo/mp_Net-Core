# ui/lvgl/page/pixel_lab_common.py — PixelLab 三頁共用的小工具
#
# 這裡只放「頁面共用」的狀態/小元件，不放 Pixel 計算（計算在 tasks/pixel_lab_task.py）。
# 三頁都透過 bus.shared["pixel_lab"] 設定 mode/active/index/ch/target，
# 由 PixelLabTask 低優先消費；owner 不是 test 時 task 會停止並顯示「被接手」。
import time
import lvgl as lv

from lib.sys.sys_bus import bus
from ui.lvgl import ui_common as u


def state():
    st = bus.shared.get("pixel_lab")
    if not isinstance(st, dict):
        st = {
            "active": 0, "mode": "apa", "index": 0, "ch": 0,
            "target": 0x70, "target_bus": 0, "hb": 0,
        }
        bus.shared["pixel_lab"] = st
    return st


def view():
    v = bus.shared.get("pixel_lab_view")
    return v if isinstance(v, dict) else {}


def touch():
    st = state()
    st["hb"] = time.ticks_ms()


def open_page(mode):
    st = state()
    st["mode"] = mode
    st["active"] = 1
    st["hb"] = time.ticks_ms()


def close_page():
    st = state()
    st["active"] = 0
    st["hb"] = time.ticks_ms()


def status_text(v):
    if v.get("busy"):
        return "被接手", u.WARNING
    if v.get("active"):
        return "使用中", u.SUCCESS
    return "待機", u.TEXT3


def pct(v):
    try:
        return max(0, min(100, int(v)))
    except Exception:
        return 0


def mk_vbar(parent, x, y, w, h, color):
    """垂直亮度方塊：track + fill。回傳 (track, fill)。"""
    track = lv.obj(parent)
    track.set_size(w, h)
    track.set_pos(x, y)
    track.set_style_bg_color(u.C(u.TRACK), 0)
    track.set_style_radius(7, 0)
    track.set_style_border_color(u.C(u.BORDER), 0)
    track.set_style_border_width(1, 0)
    track.set_style_pad_all(0, 0)
    track.remove_flag(lv.obj.FLAG.SCROLLABLE)

    fill = lv.obj(track)
    fill.set_size(w - 4, 1)
    fill.set_pos(2, h - 3)
    fill.set_style_bg_color(u.C(color), 0)
    fill.set_style_radius(5, 0)
    fill.set_style_border_width(0, 0)
    fill.set_style_pad_all(0, 0)
    fill.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return track, fill


def set_vbar(fill, w, h, p, color, color_empty=None):
    """p=0..100；填滿高度依 p 改變；0 也留 1px 讓 track 不被動態隱藏。"""
    p = pct(p)
    fh = int((h - 4) * p / 100.0)
    if fh < 1:
        fh = 1
    try:
        fill.set_size(w - 4, fh)
        fill.set_pos(2, h - 2 - fh)
        fill.set_style_bg_color(u.C(color if p > 0 or not color_empty else color_empty), 0)
    except Exception:
        pass


def set_label(lb, text, color=None):
    try:
        lb.set_text(text)
        if color is not None:
            lb.set_style_text_color(u.C(color), 0)
    except Exception:
        pass


class SimplePage:
    """APA / RGB 共用版型：IO、選取 index、呼吸/選中兩個亮度方塊。"""

    def __init__(self, mode, title, subtitle, accent):
        self.mode = mode
        self.title = title
        self.subtitle = subtitle
        self.accent = accent
        self.scr = None
        self._io_lb = None
        self._idx_lb = None
        self._status_lb = None
        self._bars = []
        self._count = 0
        self._cursor = 0
        self._last_run = -99

    def build(self):
        scr = lv.obj(None)
        scr.set_style_bg_color(u.C(u.BG), 0)
        self.scr = scr

        u.mk_label(scr, self.title, 6, 2, u.TEXT, u.ZH)
        u.mk_label(scr, self.subtitle, 6, 21, u.TEXT3, u.ZH)
        self._status_lb = u.mk_label(scr, "待機", 0, 5, u.TEXT3, u.ZH)
        try:
            self._status_lb.align(lv.ALIGN.TOP_RIGHT, -6, 4)
        except Exception:
            pass

        card = u.mk_card(scr, 6, 38, 308, 194)
        line = lv.obj(card)
        line.set_size(3, 190)
        line.set_pos(0, 2)
        line.set_style_bg_color(u.C(self.accent), 0)
        line.set_style_radius(2, 0)
        line.set_style_border_width(0, 0)
        line.set_style_pad_all(0, 0)
        try:
            line.remove_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass

        u.mk_label(card, "IO", 12, 8, u.TEXT3, u.ZH)
        self._io_lb = u.mk_label(card, "—", 12, 20, u.TEXT, u.ZH)

        u.mk_label(card, "選取 INDEX", 12, 52, u.TEXT3, u.ZH)
        self._idx_lb = u.mk_label(card, "0", 12, 62, self.accent, u.F_NUM_XL)

        # 兩個垂直亮度方塊
        u.mk_label(card, "呼吸", 196, 30, u.TEXT3, u.ZH)
        u.mk_label(card, "選中", 244, 30, u.TEXT3, u.ZH)
        for x in (196, 244):
            track, fill = mk_vbar(card, x, 44, 34, 126, self.accent)
            plb = u.mk_label(card, "0%", x + 5, 150, u.TEXT3, u.F_NUM_S)
            self._bars.append({"track": track, "fill": fill, "pct": plb, "x": x, "y": 44, "w": 34, "h": 126})
        # base fill 用淡色、selected 用主色
        try:
            self._bars[0]["fill"].set_style_bg_color(u.C(0xE7E2F8 if self.mode == "apa" else 0xD9F3E9), 0)
        except Exception:
            pass
        return scr

    def on_enter(self):
        open_page(self.mode)
        st = state()
        try:
            self._cursor = int(st.get("index", 0) or 0)
        except Exception:
            self._cursor = 0
        if self._count > 0 and self._cursor >= self._count:
            self._cursor = 0
        touch()
        self._paint_index(self._cursor)

    def on_leave(self):
        close_page()

    def on_enc(self, d):
        # 左右只移動游標/數字；真正選取要按 enter。
        if not d:
            return
        n = self._count if self._count > 0 else 1
        self._cursor = (self._cursor + int(d)) % n
        touch()
        self._paint_index(self._cursor)

    def on_confirm(self):
        # enter 才把游標 commit 給 task。
        st = state()
        st["index"] = int(self._cursor)
        touch()
        self._paint_index(self._cursor)
        return None

    def on_exit(self):
        return False

    def _paint_index(self, idx):
        set_label(self._idx_lb, str(int(idx)))

    def _paint_bar(self, i, p):
        b = self._bars[i]
        color = self.accent if i == 1 else (0xC9BFF0 if self.mode == "apa" else 0x9FE1C8)
        set_vbar(b["fill"], b["w"], b["h"], p, color)
        # 百分比放在 fill 頂端附近
        p = pct(p)
        try:
            b["pct"].set_pos(b["x"] + 4, b["y"] + b["h"] - 4 - int((b["h"] - 4) * p / 100.0) + 2)
        except Exception:
            pass
        set_label(b["pct"], "{}%".format(p), self.accent if i == 1 else u.TEXT2)

    def update(self, run):
        # hb 約 200ms 一次；task lease 3s
        if run - self._last_run >= 10:
            self._last_run = run
            touch()
        v = view()
        try:
            c = int(v.get("count", 0) or 0)
        except Exception:
            c = 0
        if c > 0 and c != self._count:
            self._count = c
            if self._cursor >= c:
                self._cursor = c - 1
            st = state()
            idx = int(st.get("index", 0) or 0)
            if idx >= c:
                st["index"] = c - 1
            self._paint_index(self._cursor)
        txt, col = status_text(v)
        set_label(self._status_lb, txt, col)
        set_label(self._io_lb, v.get("io") or "—")
        # 不從 view 覆蓋 _cursor：左右只改數字，enter 才 commit。
        self._paint_bar(0, v.get("base_pct", 0))
        self._paint_bar(1, v.get("sel_pct", 0))

    # 給模組層使用的別名
    def paint_index(self, idx):
        self._paint_index(idx)
