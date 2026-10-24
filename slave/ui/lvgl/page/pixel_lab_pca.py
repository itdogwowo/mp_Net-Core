# ui/lvgl/page/pixel_lab_pca.py — PixelLab PCA（仿晶片直放、2×8、目標/掃描）
#
# 版面：
#   左：目標清單（固定高度、可捲動）＋ 清單外的獨立「掃描」按鈕
#   右：I2C IO、直放晶片（左右各 8 格）、中央 Channel/百分比
# 資料：
#   pca9685_ctl（driver 建的控制表）→ 清單/掃描
#   PixelLabTask 直接寫選中的 PCA；本頁只設定 target/ch，不碰硬體
#
# 本版依 lvgl-console-ui 的最終模擬結果：
#   1. 晶片內部改成兩排對稱（14..28 / 76..90）、腳位伸出晶片外、
#      中央讀數用 22px 並依量測寬度置中（ALL/00 不會壓到右排）。
#   2. 左卡清單從 y=16 改 30（不壓「目標」標籤與 focus 外框）；
#      同步數量移到 170，掃描鈕貼底 204。
#   3. 新增「全部通道」ALL_LED：游標 -1（UI）→ state ch 0xFF（task）。
#   4. 晶片只留一個框：focus 1 = 琥珀游標框（全部時框晶片本體）、
#      focus 2 = 藍色已套用框；清單層不畫晶片框。
#
# exit = 返回上一層（不是直接離開）：
#   掃描(2) → channel(1) → 目標清單(0) → launcher
import time
import lvgl as lv

from lib.sys.sys_bus import bus
from ui.lvgl import ui_common as u
from ui.lvgl.registry import register
from ui.lvgl.page import pixel_lab_common as c

CH_COUNT = 16
ALL_CH = 0xFF
CURSOR_ALL = -1
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
        self._in_area = 0
        self._ch_cursor = CURSOR_ALL
        self._cell = 14
        self._last_run = -99
        self._last_targets = -99
        self._last_pcts = None
        self._sub_lb = None
        self._status_lb = None
        self._io_lb = None
        self._pins_lb = None
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
            raw = int(st.get("ch", 0) or 0) & 0xFF
            if raw == ALL_CH:
                return CURSOR_ALL
            return c.wrap(raw, CH_COUNT)
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
        # 列高是「字高 + item pad」，pad 是 add_text 之後才設的 →
        # 全部加完要重排一次，否則列距還是預設值（3 列就超出清單被裁掉）。
        try:
            self._list._layoutItems()
        except Exception:
            pass

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
        self._sub_lb = u.mk_label(scr, "PCA9685", 6, 21, u.TEXT3, u.ZH)
        self._status_lb = u.mk_label(scr, "待機", 0, 5, u.TEXT3, u.ZH)
        try:
            self._status_lb.align(lv.ALIGN.TOP_RIGHT, -6, 4)
        except Exception:
            pass

        # ── 左：固定高度清單卡 + 清單外的同步/掃描 ──
        left = u.mk_card(scr, 6, 38, 104, 128)
        u.mk_label(left, "目標", 6, 4, u.TEXT3, u.ZH)
        # 清單從 y=30 開始：原 y=16 會壓到「目標」標籤（4..23），
        # 清單的 focus 外框再往外 3px 會切到字下緣；30 之後留 7px。
        self._list, self._btns = u.mk_list(left, 2, 30, 100, 88, ["(載入中)"], font=u.ZH)
        try:
            self._list.set_style_pad_all(1, 0)
        except Exception:
            pass
        # 同步數量移到原掃描位置（視線先看到），掃描鈕貼底。
        self._sync_lb = u.mk_label(scr, "", 6, 170, u.SUCCESS, u.ZH)
        self._scan_btn = u.mk_btn(scr, "掃描", 6, 204, 104, 28, "secondary")

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
        # 兩行（行高 19）：IO 在 6、接腳在 25 → 文字佔到 44
        u.mk_label(right, "IO", 12, 6, u.TEXT3, u.ZH)
        self._io_lb = u.mk_label(right, "—", 40, 6, u.TEXT, u.ZH)
        self._pins_lb = u.mk_label(right, "", 12, 25, u.TEXT3, u.ZH)

        # ── 晶片（設計尺寸 → 實際）────────────────────────────
        # 設計尺寸 104×138、格子 14、間距 16、腳位 4；S=1.0 照設計；
        # 其餘全部由它推導，要微調只改這一個數字。
        S = 1.0
        CHIP_W = int(round(104 * S))
        CHIP_H = int(round(138 * S))
        CELL = int(round(14 * S))
        PITCH = int(round(16 * S))
        PIN_W = max(2, int(round(4 * S)))
        PAD = int(round(4 * S))
        NUM_FONT = u.F_NUM_L
        self._cell = CELL
        chipX = (198 - 2 - CHIP_W) // 2  # 46（扣 1px 邊框）
        chipY = 48                       # 文字 6..44、晶片 48..186
        chip = lv.obj(right)
        chip.set_size(CHIP_W, CHIP_H)
        chip.set_pos(chipX, chipY)
        chip.set_style_bg_color(u.C(0xFBFCFE), 0)
        chip.set_style_radius(int(round(10 * S)), 0)
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
            notch.set_size(4, 4)
            notch.set_pos(4, 3)
            notch.set_style_radius(2, 0)
            notch.set_style_bg_color(u.C(u.TEXT3), 0)
            notch.set_style_border_width(0, 0)
            notch.set_style_pad_all(0, 0)
        except Exception:
            notch = None

        # side pins：伸出晶片外（畫在晶片內會像虛線邊框）；
        # 不能掛在 chip 底下（父物件裁切會剪掉負座標），掛在卡片 right 上。
        for i in range(8):
            y = chipY + PAD + 2 + i * PITCH + int((CELL - 2) / 2)
            for x in (chipX - PIN_W, chipX + CHIP_W):
                try:
                    pin = lv.obj(right)
                    pin.set_size(PIN_W, 1)
                    pin.set_pos(x, y)
                    pin.set_style_bg_color(u.C(0xB4BDC9), 0)
                    pin.set_style_border_width(0, 0)
                    pin.set_style_pad_all(0, 0)
                except Exception:
                    pass

        # 2 columns × 8 squares（兩排對稱；margin 0.13×寬 → 14）
        self._squares = []
        margin = max(3, int(round(CHIP_W * 0.13)))
        col_x = [margin, CHIP_W - margin - CELL]
        for idx in range(CH_COUNT):
            col = 0 if idx < 8 else 1
            row = idx % 8
            x = col_x[col]
            y = PAD + 1 + row * PITCH
            track, fill = c.mk_vbar(chip, x, y, CELL, CELL, 0xD97706)
            self._squares.append({"track": track, "fill": fill, "idx": idx, "x": x, "y": y})

        # center channel + selected %
        # F_NUM_L 22：量測 "00" 約 24px → (104-24)/2-1 = 39；固定 38 留 1px。
        # numY = 138/2 - 22px 行高*0.75 ≈ 49；pct 在下一行 +2。
        self._ch_lb = u.mk_label(chip, "00", 38, 49, 0xD97706, NUM_FONT)
        self._pct_lb = u.mk_label(chip, "0%", 40, 78, u.TEXT2, u.F_NUM_S)

        self._paint_focus()
        return scr

    # ── focus / squares ─────────────────────────────────
    def _paint_focus(self):
        # 第一層：選區域（清單 / 晶片 / 掃描），三區各自有 focus。
        # 進入區域後：晶片不再畫大框，改用游標框/晶片框（_paint_selected）。
        list_focus = (self._focus == 0)
        scan_focus = (self._focus == 2)
        chip_area = (self._focus == 1)
        try:
            u.set_focus(self._list, list_focus)
        except Exception:
            pass
        try:
            u.set_focus(self._chip, chip_area and not self._in_area)
        except Exception:
            pass
        try:
            u.set_focus(self._scan_btn, scan_focus)
        except Exception:
            pass
        # 第一層要清楚告訴使用者「哪一區可以按 OK 進入」。
        if not self._in_area:
            hint = ("目標：OK 進入" if self._focus == 0 else
                    "LED 控制：OK 進入" if self._focus == 1 else
                    "掃描：OK 執行")
        else:
            hint = "PCA9685"
        try:
            if self._sub_lb is not None:
                self._sub_lb.set_text(hint)
        except Exception:
            pass
        self._paint_selected()

    def _set_chip_frame(self, color):
        try:
            if color is None:
                self._chip.set_style_outline_width(0, 0)
            else:
                self._chip.set_style_outline_color(u.C(color), 0)
                self._chip.set_style_outline_width(2, 0)
                self._chip.set_style_outline_pad(2, 0)
        except Exception:
            pass

    def _paint_selected(self):
        # 第一層只顯示區域框；晶片上的游標/已套用框只在「進入晶片區」後畫。
        if not self._in_area:
            # 第一層：晶片（LED 控制）也要跟清單/掃描一樣有藍框，
            # 否則使用者不知道這一區可以按 OK 進入。
            self._set_chip_frame(u.PRIMARY if self._focus == 1 else None)
            for s in self._squares:
                try:
                    s["track"].set_style_outline_width(0, 0)
                except Exception:
                    pass
            return
        # 晶片上只允許一個框，跟著焦點走：
        #   晶片區 → 琥珀游標框（單格或「全部」的晶片本體）
        #   掃描區 → 藍框，停在剛套用的位置
        ch = self._applied_ch()
        cur = self._ch_cursor
        all_cursor = (self._focus == 1 and cur == CURSOR_ALL)
        all_applied = (self._focus == 2 and ch == CURSOR_ALL)
        self._set_chip_frame(u.WARNING if all_cursor else
                             (u.PRIMARY if all_applied else None))
        for s in self._squares:
            try:
                color = None
                if self._focus == 1 and cur != CURSOR_ALL and s["idx"] == cur:
                    color = u.WARNING
                elif self._focus == 2 and ch != CURSOR_ALL and s["idx"] == ch:
                    color = u.PRIMARY
                if color is None:
                    s["track"].set_style_outline_width(0, 0)
                else:
                    s["track"].set_style_outline_color(u.C(color), 0)
                    s["track"].set_style_outline_width(2, 0)
                    s["track"].set_style_outline_pad(1, 0)
            except Exception:
                pass

    def _paint_squares(self, v):
        pcts = v.get("ch_pcts")
        base = c.pct(v.get("base_pct", 0))
        ch = self._applied_ch()
        cur = self._ch_cursor if self._focus == 1 else ch
        sel = c.pct(v.get("sel_pct", 0))
        for s in self._squares:
            i = s["idx"]
            p = base
            if isinstance(pcts, (list, tuple)) and i < len(pcts):
                p = c.pct(pcts[i])
            # 亮色跟游標；但「ALL」顯示呼吸 base，不是 selected 閃爍。
            is_cur = (cur == CURSOR_ALL) or (i == cur)
            if cur != CURSOR_ALL and i == cur:
                p = sel
            color = 0xD97706 if is_cur else 0xF0C98A
            try:
                c.set_vbar(s["fill"], self._cell, self._cell, p, color,
                           x=s["x"], y=s["y"])
            except Exception:
                pass
        # 中央顯示游標（全部 → ALL）；enter 後才會套用到 task。
        try:
            self._ch_lb.set_text("ALL" if cur == CURSOR_ALL else "{:02d}".format(cur))
        except Exception:
            pass
        # ALL 的中央百分比顯示呼吸 base；單一選取才顯示 selected。
        pct = v.get("base_pct", 0) if cur == CURSOR_ALL else v.get("sel_pct", 0)
        if cur != CURSOR_ALL and cur != ch and isinstance(pcts, (list, tuple)) and cur < len(pcts):
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
        self._in_area = 0
        # 一開始進入 = 選取 ALL：所有通道一起呼吸。
        self._ch_cursor = CURSOR_ALL
        st = c.state()
        st["ch"] = ALL_CH
        c.touch()
        self._refresh_targets(force=True)
        c.open_page("pca")
        c.touch()
        self._paint_focus()
        self._paint_sync()

    def on_leave(self):
        # 離開頁面 = 回到 ALL（所有通道一起呼吸），不是停在最後單一通道。
        st = c.state()
        st["ch"] = ALL_CH
        self._ch_cursor = CURSOR_ALL
        c.touch()
        c.close_page()
        self._focus = 0
        self._in_area = 0

    def on_enc(self, d):
        if not d:
            return
        step = int(d)
        if not self._in_area:
            # 第一層：左右選區域（目標清單 / 晶片 / 掃描）。
            self._focus = c.wrap(self._focus + step, 3)
            c.touch()
            self._paint_focus()
            return
        if self._focus == 0:
            n = len(self._targets)
            if n:
                self._cursor = c.wrap(self._cursor + step, n)
                c.touch()
                # 滾動即套用；enter 不再重複 commit。
                self._commit()
        elif self._focus == 1:
            # 游標順序：ALL → 00 → 01 … → 15 → ALL（繞一圈）
            n = CH_COUNT + 1
            pos = 0 if self._ch_cursor == CURSOR_ALL else self._ch_cursor + 1
            nxt = c.wrap(pos + step, n)
            self._ch_cursor = CURSOR_ALL if nxt == 0 else nxt - 1
            # 滾動即套用；enter 不再重複 commit。
            st = c.state()
            st["ch"] = ALL_CH if self._ch_cursor == CURSOR_ALL else c.wrap(self._ch_cursor, CH_COUNT)
            c.touch()
            self._paint_selected()
            self._paint_squares(c.view())

    def on_confirm(self):
        if not self._in_area:
            # 第一層：enter 進入目前選中的區域；掃描是動作，直接觸發。
            self._in_area = 1
            if self._focus == 1:
                # 剛進入 LED 選取時仍視為 ALL；滾動才標記特定通道。
                self._ch_cursor = CURSOR_ALL
                st = c.state()
                st["ch"] = ALL_CH
                c.touch()
            if self._focus == 2:
                self._do_scan()
            self._paint_focus()
            return None
        if self._focus == 0:
            # 清單區也是滾動即套用；enter 只留在區內，不再重複 commit。
            return None
        elif self._focus == 1:
            # 晶片區已經是「滾動即套用」；再按 OK 會變成第二套選擇邏輯，
            # 跟 on_enc 衝突，所以這裡不再 commit，直接留給滾動。
            return None
        else:
            self._do_scan()
        return None

    def on_exit(self):
        # exit = 返回上一層：
        #   區域內 → 第一層（區域選擇）→ launcher。
        if self._in_area:
            self._in_area = 0
            # 退出 LED 選取 = 回到 ALL（所有通道一起呼吸）。
            self._ch_cursor = CURSOR_ALL
            st = c.state()
            st["ch"] = ALL_CH
            c.touch()
            self._paint_focus()
            self._paint_squares(c.view())
            return True
        return False

    def update(self, run):
        if run - self._last_run >= 10:
            self._last_run = run
            c.touch()
        self._poll_scan()
        # 清單不需要每幀重建；掃描結果/清單只要約每 1 秒更新一次。
        if run - self._last_targets >= 30:
            self._last_targets = run
            self._refresh_targets()
        v = c.view()
        txt, col = c.status_text(v)
        c.set_label(self._status_lb, txt, col)
        c.set_label(self._io_lb, v.get("io") or "—")
        c.set_label(self._pins_lb, v.get("pins") or "")
        self._paint_sync()
        # 16 格只在值真的變了才重畫，避免每幀 16 次 set_vbar 拖垮 LVGL。
        pcts = (v.get("base_pct"), v.get("sel_pct"), v.get("ch"))
        if pcts != self._last_pcts:
            self._last_pcts = pcts
            if v.get("mode") == "pca":
                self._paint_squares(v)
            else:
                # 尚未開始/被接手：保持最後顯示，但焦點/清單仍更新
                self._paint_selected()


_page = PcaPage()


@register(id="pixel_lab_pca", title="PixelLab PCA", icon="gauge",
          desc="PCA9685", order=1, accent=0xD97706)
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
