# ui/lvgl/page/pixel_lab_pca.py — PixelLab PCA（仿晶片直放、2×8、目標/掃描）
#
# 版面：
#   左：目標清單（固定高度、可捲動）＋ 清單外的獨立「掃描」按鈕
#   右：I2C IO、直放晶片（左右各 8 格）、中央 Channel/百分比
# 資料：
#   pca9685_ctl（driver 建的控制表）→ 清單/掃描
#   PixelLabTask 直接寫選中的 PCA；本頁只設定 target/ch，不碰硬體
import time
import lvgl as lv

from lib.sys.sys_bus import bus
from ui.lvgl import ui_common as u
from ui.lvgl.registry import register
from ui.lvgl.page import pixel_lab_common as c

CH_COUNT = 16
SCAN_TIMEOUT_MS = 8000
SEL_BG = 0xD2E3FC
CURSOR_BG = 0xFFF3CD


class PcaPage:
    def __init__(self):
        self.scr = None
        self._list = None
        self._btns = []
        self._targets = []
        self._sel = 0
        self._cursor = 0
        self._sig = None
        self._scan_pending = False
        self._scan_at = 0
        self._focus = 0
        self._ch_cursor = 0
        self._last_run = -99
        self._status_lb = None
        self._io_lb = None
        self._sync_lb = None
        self._scan_btn = None
        self._chip = None
        self._squares = []
        self._ch_lb = None
        self._pct_lb = None

    # ── 資料 ────────────────────────────────────────────
    def _ctl(self):
        s = bus.get_service("pca9685_ctl")
        if not isinstance(s, list):
            return []
        out = list(s)
        try:
            out.sort(key=lambda r: (0 if r.get("broadcast") else 1,
                                    int(r.get("i2c", 0)), int(r.get("addr", 0))))
        except Exception:
            pass
        return out

    def _sig_of(self, ts):
        out = []
        for r in ts:
            out.append((r.get("i2c"), r.get("addr"),
                        bool(r.get("online")), len(r.get("boards") or [])))
        return tuple(out)

    def _row_text(self, r):
        s = "0x{:02X}".format(int(r.get("addr", 0)))
        if r.get("broadcast"):
            s += " 廣播"
        else:
            s = "{} {}".format("●" if r.get("online") else "○", s)
        return s

    def _applied_ch(self):
        try:
            st = c.state()
            return int(st.get("ch", 0) or 0) % CH_COUNT
        except Exception:
            return 0

    def _cur_rec(self):
        if 0 <= self._sel < len(self._targets):
            return self._targets[self._sel]
        return None

    def _key(self, r):
        if r is None:
            return None
        return (int(r.get("i2c", 0) or 0), int(r.get("addr", 0) or 0))

    def _commit_state(self):
        st = c.state()
        rec = self._cur_rec()
        if rec is None:
            st["target"] = 0x70
            st["target_bus"] = 0
        else:
            st["target"] = int(rec.get("addr", 0) or 0)
            st["target_bus"] = int(rec.get("i2c", 0) or 0)
        c.touch()

    def _refresh_targets(self, force=False):
        if self._list is None:
            return
        ts = self._ctl()
        sig = self._sig_of(ts)
        if not (force or sig != self._sig):
            self._targets = ts
            self._sync_list()
            return
        old_key = self._key(self._cur_rec())
        changed = (sig != self._sig)
        self._sig = sig
        self._targets = ts
        if changed:
            # 掃描結果回來了（清單內容有變）→ 結束「掃描中…」
            self._scan_pending = False
        labels = [self._row_text(r) for r in ts] or ["(無控制器)"]
        try:
            self._list.clean()
        except Exception:
            pass
        self._btns = []
        for txt in labels:
            try:
                b = self._list.add_text(txt)
                if u.ZH:
                    b.set_style_text_font(u.ZH, 0)
                try:
                    b.set_style_pad_all(1, 0)
                except Exception:
                    pass
                self._btns.append(b)
            except Exception:
                continue

        self._sel = 0
        found = False
        if old_key is not None:
            for i, r in enumerate(ts):
                if self._key(r) == old_key:
                    self._sel = i
                    found = True
                    break
        if not found:
            for i, r in enumerate(ts):
                if int(r.get("addr", 0) or 0) == 0x70:
                    self._sel = i
                    break
        self._cursor = self._sel
        self._sync_list()
        self._commit_state()

    def _sync_list(self):
        n = len(self._btns)
        for i in range(n):
            b = self._btns[i]
            rec = self._targets[i] if i < len(self._targets) else None
            if rec is None:
                fg, bg = u.TEXT3, u.BG
            else:
                if rec.get("broadcast"):
                    fg = u.PRIMARY
                else:
                    fg = u.SUCCESS if rec.get("online") else u.TEXT3
                if i == self._cursor:
                    bg = CURSOR_BG
                elif i == self._sel:
                    bg = SEL_BG
                else:
                    bg = u.BG
            try:
                b.set_style_bg_color(u.C(bg), 0)
                b.set_style_text_color(u.C(fg), 0)
            except Exception:
                pass
        self._scroll()

    def _scroll(self):
        if self._list is None or not self._btns:
            return
        i = max(0, min(self._cursor, len(self._btns) - 1))
        b = self._btns[i]
        try:
            self._list.scroll_to_view(b)
            return
        except Exception:
            pass
        # 某些 binding 只提供 child.scroll_to_view()；有就用，沒有就維持原狀
        try:
            b.scroll_to_view()
        except Exception:
            pass

    def _commit(self):
        if not self._btns:
            return
        self._sel = max(0, min(self._cursor, len(self._targets) - 1))
        self._commit_state()
        self._sync_list()

    # ── 掃描（清單外按鈕）──────────────────────────────
    def _do_scan(self):
        if self._scan_pending:
            return
        q = bus.shared.get("_pca_actions")
        if not isinstance(q, list):
            q = []
            bus.shared["_pca_actions"] = q
        q.append({"action": "scan"})
        self._scan_pending = True
        self._scan_at = time.ticks_ms()
        self._paint_sync()

    def _poll_scan(self):
        if not self._scan_pending:
            return
        try:
            since = time.ticks_diff(time.ticks_ms(), self._scan_at)
        except Exception:
            since = SCAN_TIMEOUT_MS + 1
        if since > SCAN_TIMEOUT_MS:
            self._scan_pending = False

    # ── 建立畫面 ────────────────────────────────────────
    def build(self):
        scr = lv.obj(None)
        scr.set_style_bg_color(u.C(u.BG), 0)
        self.scr = scr

        u.mk_label(scr, "PixelLab PCA", 6, 2, u.TEXT, u.ZH)
        u.mk_label(scr, "PCA9685", 6, 21, u.TEXT3, u.ZH)
        self._status_lb = u.mk_label(scr, "待機", 0, 5, u.TEXT3, u.ZH)
        try:
            self._status_lb.align(lv.ALIGN.TOP_RIGHT, -6, 4)
        except Exception:
            pass

        # ── 左：固定高度清單卡 + 清單外的掃描 ──
        left = u.mk_card(scr, 6, 38, 104, 128)
        u.mk_label(left, "目標", 6, 4, u.TEXT3, u.ZH)
        self._list, self._btns = u.mk_list(left, 2, 16, 100, 96, ["(載入中)"], font=u.ZH)
        try:
            self._list.set_style_pad_all(1, 0)
        except Exception:
            pass
        self._sync_lb = u.mk_label(left, "", 6, 116, u.SUCCESS, u.ZH)
        self._scan_btn = u.mk_btn(scr, "掃描", 6, 174, 104, 28, "secondary")

        # ── 右：IO + 直放晶片 ──
        right = u.mk_card(scr, 116, 38, 198, 194)
        line = lv.obj(right)
        line.set_size(3, 190)
        line.set_pos(0, 2)
        line.set_style_bg_color(u.C(0xD97706), 0)
        line.set_style_radius(2, 0)
        line.set_style_border_width(0, 0)
        line.set_style_pad_all(0, 0)
        try:
            line.remove_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass
        u.mk_label(right, "IO", 12, 8, u.TEXT3, u.ZH)
        self._io_lb = u.mk_label(right, "—", 12, 20, u.TEXT, u.ZH)

        # chip body (vertical)
        chip = lv.obj(right)
        chip.set_size(104, 138)
        chip.set_pos(44, 40)
        chip.set_style_bg_color(u.C(0xFBFCFE), 0)
        chip.set_style_radius(10, 0)
        chip.set_style_border_color(u.C(0xCBD5E1), 0)
        chip.set_style_border_width(1, 0)
        chip.set_style_pad_all(0, 0)
        try:
            chip.remove_flag(lv.obj.FLAG.SCROLLABLE)
        except Exception:
            pass
        self._chip = chip

        # notch
        try:
            notch = lv.obj(chip)
            notch.set_size(5, 5)
            notch.set_pos(5, 4)
            notch.set_style_radius(3, 0)
            notch.set_style_bg_color(u.C(u.TEXT3), 0)
            notch.set_style_border_width(0, 0)
            notch.set_style_pad_all(0, 0)
        except Exception:
            notch = None

        # side pins (inside chip edges; decorative)
        for i in range(8):
            y = 10 + i * 16
            for x in (0, 100):
                try:
                    pin = lv.obj(chip)
                    pin.set_size(4, 1)
                    pin.set_pos(x, y)
                    pin.set_style_bg_color(u.C(0xB4BDC9), 0)
                    pin.set_style_border_width(0, 0)
                    pin.set_style_pad_all(0, 0)
                except Exception:
                    pass

        # 2 columns × 8 squares
        self._squares = []
        for idx in range(CH_COUNT):
            col = 0 if idx < 8 else 1
            row = idx % 8
            x = 18 if col == 0 else 91
            y = 10 + row * 16
            track, fill = c.mk_vbar(chip, x, y, 14, 14, 0xD97706)
            self._squares.append({"track": track, "fill": fill, "idx": idx, "x": x, "y": y})

        # center channel + selected %
        self._ch_lb = u.mk_label(chip, "00", 52, 32, 0xD97706, u.F_NUM_XL)
        self._pct_lb = u.mk_label(chip, "0%", 52, 72, u.TEXT2, u.F_NUM_S)

        # focus frame on the chip
        self._paint_focus()
        return scr

    # ── focus / squares ─────────────────────────────────
    def _paint_focus(self):
        try:
            u.set_focus(self._list, self._focus == 0)
        except Exception:
            pass
        try:
            u.set_focus(self._chip, self._focus == 1)
        except Exception:
            pass
        try:
            u.set_focus(self._scan_btn, self._focus == 2)
        except Exception:
            pass
        self._paint_selected()

    def _paint_selected(self):
        # 主色框 = 真正套用的 channel；左/右游標用 warning 框預覽。
        ch = self._applied_ch()
        cur = self._ch_cursor if self._focus == 1 else ch
        for s in self._squares:
            try:
                if s["idx"] == ch:
                    s["track"].set_style_outline_color(u.C(u.PRIMARY), 0)
                    s["track"].set_style_outline_width(2, 0)
                    s["track"].set_style_outline_pad(1, 0)
                elif s["idx"] == cur:
                    s["track"].set_style_outline_color(u.C(u.WARNING), 0)
                    s["track"].set_style_outline_width(1, 0)
                    s["track"].set_style_outline_pad(1, 0)
                else:
                    s["track"].set_style_outline_width(0, 0)
            except Exception:
                pass

    def _paint_squares(self, v):
        pcts = v.get("ch_pcts")
        base = c.pct(v.get("base_pct", 0))
        ch = self._applied_ch()
        cur = self._ch_cursor if self._focus == 1 else ch
        for s in self._squares:
            i = s["idx"]
            p = base
            if isinstance(pcts, (list, tuple)) and i < len(pcts):
                p = c.pct(pcts[i])
            if i == ch:
                p = c.pct(v.get("sel_pct", p))
            color = 0xD97706 if i == ch else 0xF0C98A
            try:
                c.set_vbar(s["fill"], 14, 14, p, color)
            except Exception:
                pass
        # 中央是游標數字（左/右會動）；enter 後才會套用到 task。
        try:
            self._ch_lb.set_text("{:02d}".format(cur))
        except Exception:
            pass
        pct = v.get("sel_pct", 0)
        if cur != ch and isinstance(pcts, (list, tuple)) and cur < len(pcts):
            pct = pcts[cur]
        try:
            self._pct_lb.set_text("{}%".format(c.pct(pct)))
        except Exception:
            pass
        self._paint_selected()

    def _paint_sync(self):
        if self._sync_lb is None:
            return
        if self._scan_pending:
            c.set_label(self._sync_lb, "掃描中…", u.WARNING)
            return
        rec = self._cur_rec()
        if rec is None:
            c.set_label(self._sync_lb, "無裝置", u.DANGER)
        elif rec.get("broadcast"):
            n = len(rec.get("boards") or [])
            if n and rec.get("online"):
                c.set_label(self._sync_lb, "同步 {} 台".format(n), u.SUCCESS)
            elif n:
                c.set_label(self._sync_lb, "離線", u.DANGER)
            else:
                c.set_label(self._sync_lb, "無實體板", u.DANGER)
        else:
            c.set_label(self._sync_lb, "在線" if rec.get("online") else "離線",
                        u.SUCCESS if rec.get("online") else u.DANGER)

    # ── 頁面接口 ────────────────────────────────────────
    def on_enter(self):
        self._focus = 0
        self._ch_cursor = self._applied_ch()
        self._refresh_targets(force=True)
        c.open_page("pca")
        c.touch()
        self._paint_focus()
        self._paint_sync()

    def on_leave(self):
        c.close_page()
        self._focus = 0

    def on_enc(self, d):
        if not d:
            return
        if self._focus == 0:
            n = len(self._targets)
            if n:
                self._cursor = (self._cursor + int(d)) % n
                c.touch()
                self._sync_list()
        elif self._focus == 1:
            # 左右只移動 channel 游標；enter 才寫入 state 給 task。
            self._ch_cursor = (self._ch_cursor + int(d)) % CH_COUNT
            c.touch()
            self._paint_selected()
            self._paint_squares(c.view())

    def on_confirm(self):
        if self._focus == 0:
            # enter：真正選取目標，然後換到 channel 區。
            self._commit()
            self._focus = 1
            self._ch_cursor = self._applied_ch()
            self._paint_focus()
            self._paint_squares(c.view())
            c.touch()
        elif self._focus == 1:
            # enter：真正選取 channel。
            st = c.state()
            st["ch"] = int(self._ch_cursor) % CH_COUNT
            c.touch()
            self._focus = 2
            self._paint_focus()
            self._paint_squares(c.view())
        else:
            self._do_scan()
        return None

    def on_exit(self):
        # exit =「返回上一層」，不是直接離開：
        #   掃描(2) → channel(1) → 目標清單(0) → launcher。
        # 回 True = 本頁消耗（只退一層）；回 False = 已到最上層，app 才回 launcher。
        if self._focus == 2:
            self._focus = 1
            self._paint_focus()
            self._paint_squares(c.view())
            return True
        if self._focus == 1:
            self._focus = 0
            self._paint_focus()
            return True
        return False

    def update(self, run):
        if run - self._last_run >= 10:
            self._last_run = run
            c.touch()
        self._poll_scan()
        self._refresh_targets()
        v = c.view()
        txt, col = c.status_text(v)
        c.set_label(self._status_lb, txt, col)
        c.set_label(self._io_lb, v.get("io") or "—")
        self._paint_sync()
        if v.get("mode") == "pca":
            self._paint_squares(v)
        else:
            # 尚未開始/被接手：保持最後顯示，但焦點/清單仍更新
            self._paint_selected()


_page = PcaPage()


@register(id="pixel_lab_pca", title="PixelLab PCA", icon="sun",
          desc="PCA9685 · 目標/16 通道", order=4, accent=0xD97706)
def build():
    return _page.build()


def on_enter():
    _page.on_enter()


def on_leave():
    _page.on_leave()


def on_enc(d):
    _page.on_enc(d)


def on_confirm():
    return _page.on_confirm()


def on_exit():
    return _page.on_exit()


def update(run):
    _page.update(run)
