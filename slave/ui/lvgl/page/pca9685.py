# ui/lvgl/page/pca9685.py — PCA9685 控制器頁（橫屏 320×240）
#
# ══════════════════════════════════════════════════════════════════
# 版面（2026-10 使用者定案：遙控器式「左列表 / 右顯示」）
# ══════════════════════════════════════════════════════════════════
#   頂列    PCA9685 控制器
#   左欄    控制目標（lv.list）
#             0x70 廣播        ← 廣播：一次寫入全部板子（第一項）
#             ● 0x40           ← 單板：只寫這一顆
#             ○ 0x41           ← ○ = 掃描沒看到（離線）
#           藍底 = 已套用；琥珀底 = 游標（按確認才變藍）
#           [全熄] [掃描]
#   右欄    目標卡  0x70 廣播                  同步 3 台
#           通道卡  通道亮度                     0-4095
#                   00 ▓▓▓▓▓░░ 2048   08 ░░░░░░░    0
#                   ...（左 CH0-7、右 CH8-15，每列 = 編號＋長條＋數值）
#           數值卡  CH0  ──────●──────  2048
#
# 為什麼要這樣分：
#   - **列表 = 控制目標**（遙控器選「對誰講話」）。清單同時包含廣播與每一顆
#     實體板，所以「廣播控制」和「單板控制」是同一個入口，不用另一個模式切換。
#   - **右邊 = 那個目標的 16 通道狀態**。廣播時只顯示一組（所有板子收到相同
#     內容，顯示模式自然更簡單）；單板時顯示那一顆的快取。
#   - **層次 = 目標 → 通道 → 數值**：
#       清單：encoder 移動「游標」（琥珀底），**按確認才套用**成控制目標（藍底），
#             套用後焦點自動移到通道方格（使用者定案 2026-10）。
#       方格：confirm 進通道選擇，encoder 選 CH0-15。
#       數值：最下面那條，confirm 進編輯、encoder 調 12-bit 亮度。
#     每一層都有自己的焦點框（nav 模組）。
#
# 資料/控制路徑：
#   bus.get_service("pca9685_ctl")  ← pca9685_drv 建的控制表（原始 PCA9685 物件）
#     record = {"i2c", "addr", "pca", "broadcast", "boards", "online"}
#   顯示：讀 `pca.buf[0..15]`（硬體側 12-bit）。
#   控制：單通道 `pca.set_channel(ch, v)`（只寫 4 bytes）；全熄用
#         `pca.show()`（一次 64 bytes）。舊驅動沒有 set_channel 時自動退回
#         `pca.buf[ch] = v; pca.show()`。
#   廣播：record.broadcast=True 時，寫 0x70 之外**同時**更新 `boards` 對應的
#         單板 record 快取，這樣切回單板時看到的值不會是上一個畫面。
#   ★ 顯示的是「控制物件的 buf」（本頁的控制快取），不是從 PCA 暫存器回讀；
#     播放端有自己的 controller 物件，所以播放中的最後一幀不會自動出現在這裡。
#
# ★ 不要在這裡自己建 I2C/PCA9685：硬體一律由 driver 建，UI 只讀 bus 服務。
# ★ 手動控制和自動播放（PixelTask/PixelStreamer）同時跑時，播放端每幀會覆寫
#   這裡寫的值（last-write-wins）。本頁適合播放停止時檢查/調整，或當測試工具。
# ★ 掃描鈕走 `_pca_actions` 佇列（在 task 執行，避免 I2C.scan() 卡 LVGL），
#   task 掃完會呼叫 `refresh_pca_ctl()` → 這裡的目標清單跟著增減。
# ★ 離開頁面**不自動熄燈**：這是控制器頁，使用者調好的亮度要留著（舊版的
#   on_leave 會送 stop/alloff，但那個 action 本來就沒有消費端）。
# ★ 舊版的「逐腳呼吸/全腳呼吸」與 bus.shared["pca9685"] 已移除：它們沒有消費端，
#   而且廣播/單板的控制路徑現在是直接寫 PCA 物件，不需要另外的狀態機。
import time
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_SLIDER, ITEM_BUTTON

CH_COUNT = 16
STEP = 32            # 每個 encoder 格的亮度步進（12-bit / 4095）
REFRESH_EVERY = 10   # 每 N 幀刷新一次（run % N）
SCAN_TIMEOUT_MS = 8000   # > 單一死線最壞 ~5.6s，掃描中狀態不會提早消失
# nav 項的固定順序（build() 的 nav.add 次序）：目標清單、通道方格、數值卡、全熄、掃描
ITEM_TARGET = 0
ITEM_GRID = 1

SEL_BG = 0xD2E3FC      # 「已套用」目標列的底色（與 remote.py 相同：淺藍底，狀態色仍可讀）
CURSOR_BG = 0xFFF3CD   # 「游標候選」列的底色（淺琥珀）—— 轉動只移動它，按確認才變 SEL_BG

# 方格列（右欄通道卡）的幾何
ROW_H = 16
BAR_X = 22
BAR_Y = 4
BAR_W = 38
BAR_H = 7
VAL_X = 62

nav = Nav()
scr = None
_target_list = None
_target_btns = []
_targets = []        # pca9685_ctl 的內容（選中索引對應）
_sel = 0             # 已套用的控制目標索引（右欄顯示的、寫入的對象）
_cursor = 0          # 清單上的游標候選索引（轉動 encoder 只動它）
_target_focused = False  # 焦點是否在「控制目標」清單上
_sel_ch = 0          # 選中的通道（0..15）

_header = _grid = _val_card = None
_target_lb = _status_lb = None
_ch_lb = _slider = _val_lb = None
_rows = []           # [{"box","num","fill","val"}]（16 個，順序 = 通道）
_off_btn = _scan_btn = None

_last_vals = [0] * CH_COUNT
_last_sig = None
_last_lb = {}        # 文字快取（避免每幀 set_text）
_scan_pending = False
_scan_at = 0


# ══════════════════════════════════════════════════════════════════
#  建立畫面
# ══════════════════════════════════════════════════════════════════
def _panel(parent, x, y, w, h):
    """無邊框分區（與 pixel_controller._panel 相同）。"""
    c = lv.obj(parent)
    c.set_size(w, h)
    c.set_pos(x, y)
    c.set_style_bg_color(u.C(u.SURFACE), 0)
    c.set_style_radius(8, 0)
    c.set_style_border_width(0, 0)
    c.set_style_pad_all(0, 0)
    c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return c


def _mk_row(parent, ch, x, y):
    """一列通道：編號 + 長條（track/fill）+ 12-bit 數值。"""
    box = lv.obj(parent)
    box.set_size(94, ROW_H)
    box.set_pos(x, y)
    box.set_style_bg_color(u.C(u.SURFACE), 0)
    box.set_style_radius(4, 0)
    box.set_style_border_width(0, 0)
    box.set_style_pad_all(0, 0)
    box.remove_flag(lv.obj.FLAG.SCROLLABLE)

    num = u.mk_label(box, "{:02d}".format(ch), 3, 0, u.TEXT3, u.F_NUM_S)

    track = lv.obj(box)
    track.set_size(BAR_W, BAR_H)
    track.set_pos(BAR_X, BAR_Y)
    track.set_style_bg_color(u.C(u.TRACK), 0)
    track.set_style_radius(3, 0)
    track.set_style_border_width(0, 0)
    track.set_style_pad_all(0, 0)
    track.remove_flag(lv.obj.FLAG.SCROLLABLE)

    fill = lv.obj(track)
    fill.set_size(1, BAR_H)
    fill.set_pos(0, 0)
    fill.set_style_bg_color(u.C(u.TRACK), 0)
    fill.set_style_radius(3, 0)
    fill.set_style_border_width(0, 0)
    fill.set_style_pad_all(0, 0)
    fill.remove_flag(lv.obj.FLAG.SCROLLABLE)

    val = u.mk_label(box, "   0", VAL_X, 0, u.TEXT3, u.F_NUM_S)
    return {"box": box, "num": num, "fill": fill, "val": val}


@register(id="pca9685", title="PCA9685", icon="sun",
          desc="目標·16 通道·廣播", order=2, accent=0x1A73E8)
def build():
    global scr, _target_list, _target_btns, _targets, _sel, _sel_ch
    global _cursor, _target_focused
    global _header, _grid, _val_card
    global _target_lb, _status_lb, _ch_lb, _slider, _val_lb
    global _rows, _off_btn, _scan_btn
    global _last_vals, _last_sig, _last_lb, _scan_pending, _scan_at

    nav.reset()
    _target_btns = []
    _targets = []
    _sel = 0
    _cursor = 0
    _target_focused = False
    _sel_ch = 0
    _rows = []
    _last_vals = [0] * CH_COUNT
    _last_sig = None
    _last_lb = {}
    _scan_pending = False
    _scan_at = 0

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    # ── 頂列 ──
    u.mk_label(scr, "PCA9685 控制器", 6, 2, u.TEXT, u.ZH)

    # ── 左欄：控制目標 + 兩顆動作鈕 ──
    u.mk_label(scr, "控制目標", 6, 24, u.TEXT3, u.ZH)
    _target_list, _target_btns = u.mk_list(scr, 4, 50, 106, 156,
                                           ["(載入中)"], font=u.ZH)
    # 預設 list 的左右內距吃掉寬度（廣播列「0x70 廣播」很長）→ 縮小。
    try:
        _target_list.set_style_pad_all(2, 0)
    except Exception:
        pass
    # 兩顆按鈕放左下角：焦點框（outline 6px）才不會被螢幕邊緣切掉。
    _off_btn = u.mk_btn(scr, "全熄", 4, 212, 51, 20, "danger")
    _scan_btn = u.mk_btn(scr, "掃描", 59, 212, 51, 20, "secondary")

    # ── 右欄：目標卡 ──
    _header = _panel(scr, 118, 24, 198, 30)
    _target_lb = u.mk_label(_header, "—", 6, 5, u.TEXT, u.ZH)
    _status_lb = u.mk_label(_header, "", 0, 5, u.TEXT3, u.ZH)
    _status_lb.align(lv.ALIGN.TOP_RIGHT, -6, 5)

    # ── 右欄：16 通道卡（左 CH0-7、右 CH8-15）──
    _grid = _panel(scr, 118, 60, 198, 150)
    u.mk_label(_grid, "通道亮度", 6, 0, u.TEXT2, u.ZH)
    note = u.mk_label(_grid, "0-4095", -6, 2, u.TEXT3, u.F_NUM_S)
    note.align(lv.ALIGN.TOP_RIGHT, -6, 2)
    for col in range(2):
        for row in range(8):
            ch = col * 8 + row
            x = 4 if col == 0 else 102
            _rows.append(_mk_row(_grid, ch, x, 20 + row * ROW_H))

    # ── 右欄：選中通道的數值卡（slider 只是顯示；導覽由 nav 的 ITEM_SLIDER 做）──
    _val_card = _panel(scr, 118, 214, 198, 18)
    _ch_lb = u.mk_label(_val_card, "CH0", 6, 1, u.TEXT, u.F_NUM_S)
    # slider x=48：左邊留給「CH15」（4 個半形 ≈ 32px）＋旋鈕的半徑；
    # 右邊到 150，數值標籤從 160 開始，旋鈕在最右也不會壓到數字。
    _slider = u.mk_slider(_val_card, 48, 5, 102, 0, 4095, 0)
    _val_lb = u.mk_label(_val_card, "   0", 160, 1, u.PRIMARY, u.F_NUM_S)

    # ── 導覽項（層次：目標 → 通道 → 數值 → 全熄 → 掃描）──
    nav.add(_target_list, ITEM_LIST, on_change=_on_target_move,
            on_focus=_on_target_focus)
    nav.add(_grid, ITEM_LIST, on_change=_on_channel_move)
    nav.add(_val_card, ITEM_SLIDER, on_change=_on_value_adjust,
            on_focus=_on_value_focus)
    nav.add(_off_btn, ITEM_BUTTON, on_change=_all_off)
    nav.add(_scan_btn, ITEM_BUTTON, on_change=_do_scan)

    try:
        _refresh_targets(force=True)
    except Exception as e:
        print("[pca9685] build refresh err:", e)
    nav.paint()
    return scr


# ══════════════════════════════════════════════════════════════════
#  資料來源
# ══════════════════════════════════════════════════════════════════
def _ctl():
    """pca9685_drv 的控制表（不是播放端的 pca9685_list）。

    ★ 回傳淺拷貝：task 端 refresh 會用 `_CTL[:] = new` 原地換內容，
      拷貝一份可以避免 UI 在迭代時剛好被換掉。
    ★ 排序：**廣播一定排第一**（舊頁面的「目標 = 廣播」預設值），其餘按
      (bus, 位址)。driver 是「先實體、後廣播」的順序，UI 這裡統一成
      「先廣播、後實體」—— 使用者一進來看到的第一項就是廣播。
    """
    from lib.sys.sys_bus import bus
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


def _ctl_exists():
    from lib.sys.sys_bus import bus
    return bus.get_service("pca9685_ctl") is not None


def _sig(ts):
    """控制表的「內容簽章」：只有這些欄位變了才重建清單。"""
    out = []
    for r in ts:
        out.append((r.get("i2c"), r.get("addr"),
                    bool(r.get("online")),
                    len(r.get("boards") or [])))
    return tuple(out)


def _cur_rec():
    if 0 <= _sel < len(_targets):
        return _targets[_sel]
    return None


def _cur_key():
    r = _cur_rec()
    if r is None:
        return None
    return (r.get("i2c"), r.get("addr"))


def _multi_bus():
    buses = []
    for r in _targets:
        b = r.get("i2c")
        if b not in buses:
            buses.append(b)
    return len(buses) > 1


def _row_text(r):
    """清單一行。

    廣播列：`0x70 廣播`（不加狀態圓點 —— 「廣播」兩個字已經夠長，而且它的
            在線狀態由右欄標題的「同步 N 台 / 離線」負責）。
    單板列：`● 0x40`（● = 掃描有看到、○ = 離線）。
    多 bus 才附 `B0`；此時廣播列變成 `0x70 廣播 B0`（仍在 106px 內）。
    """
    s = "0x{:02X}".format(int(r.get("addr", 0)))
    if r.get("broadcast"):
        s += " 廣播"
    else:
        s = "{} {}".format("●" if r.get("online") else "○", s)
    if _multi_bus():
        s += " B{}".format(int(r.get("i2c", 0)))
    return s


def _read_vals(rec):
    """讀該目標的 16 通道（12-bit）。沒有物件 → 全 0。"""
    vals = [0] * CH_COUNT
    pca = rec.get("pca") if rec else None
    buf = getattr(pca, "buf", None) if pca is not None else None
    if buf is None:
        return vals
    try:
        for i in range(CH_COUNT):
            vals[i] = int(buf[i]) & 0xFFF
    except Exception:
        return [0] * CH_COUNT
    return vals


# ══════════════════════════════════════════════════════════════════
#  目標清單
# ══════════════════════════════════════════════════════════════════
def _refresh_targets(force=False):
    global _targets, _target_btns, _sel, _cursor, _last_sig, _scan_pending
    if _target_list is None:
        return
    ts = _ctl()
    sig = _sig(ts)
    changed = (sig != _last_sig)
    if not (force or changed):
        _targets = ts
        _sync_list()
        return

    old_key = _cur_key()
    _last_sig = sig
    _targets = ts
    if changed:
        _scan_pending = False       # 掃描結果回來了

    labels = [_row_text(r) for r in ts]
    if not labels:
        labels = ["(無控制器)"]
    try:
        _target_list.clean()
    except Exception:
        pass
    _target_btns = []
    for txt in labels:
        try:
            b = _target_list.add_text(txt)
            if u.ZH:
                b.set_style_text_font(u.ZH, 0)
            try:
                # 縮小列的左右內距：多 bus 的「0x70 廣播 B0」很長，預設內距會讓
                # 最後一個字被切掉。背景色仍由 _sync_list() 覆蓋整個列。
                b.set_style_pad_all(1, 0)
            except Exception:
                pass
            _target_btns.append(b)
        except Exception:
            continue

    # 選中項：優先保留原本那個 (bus, addr)；不見了 → 退回廣播，再退第一個。
    _sel = 0
    found_old = False
    if old_key is not None:
        for i, r in enumerate(ts):
            if (r.get("i2c"), r.get("addr")) == old_key:
                _sel = i
                found_old = True
                break
    if not found_old:
        for i, r in enumerate(ts):
            if r.get("addr") == 0x70:
                _sel = i
                break

    # 清單重建（進頁/掃描）→ 游標回到已套用的目標，避免指向不存在的索引。
    _cursor = _sel
    _sync_list()
    _refresh_selected(force=True)


def _sync_list():
    """清單兩種底色（互不干擾）＋文字色。

    底色：
      CURSOR_BG（淺琥珀） = 游標候選（轉動 encoder 只動這一格）
      SEL_BG（淺藍）      = 已套用的控制目標（右欄顯示/寫入的對象）
    文字色：廣播藍 / 在線綠 / 離線灰。
    """
    n = len(_target_btns)
    for i in range(n):
        b = _target_btns[i]
        rec = _targets[i] if i < len(_targets) else None
        if rec is None:
            color = u.TEXT3
            bg = u.BG
        else:
            color = (u.PRIMARY if rec.get("broadcast")
                     else (u.SUCCESS if rec.get("online") else u.TEXT3))
            if _target_focused and i == _cursor:
                bg = CURSOR_BG          # 游標候選
            elif i == _sel:
                bg = SEL_BG             # 已套用
            else:
                bg = u.BG
        try:
            b.set_style_bg_color(u.C(bg), 0)
            b.set_style_text_color(u.C(color), 0)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  右欄顯示
# ══════════════════════════════════════════════════════════════════
def _set_lb(key, lb, txt, color=None):
    """文字快取：只有真的變了才 set_text（省 LVGL 重繪）。"""
    sig = (txt, color)
    if _last_lb.get(key) == sig:
        return
    _last_lb[key] = sig
    if lb is None:
        return
    try:
        lb.set_text(txt)
        if color is not None:
            lb.set_style_text_color(u.C(color), 0)
    except Exception:
        pass


def _refresh_selected(force=False):
    global _last_vals
    rec = _cur_rec()

    if rec is None:
        _set_lb("target", _target_lb, "—", u.TEXT3)
        if not _ctl_exists():
            _set_lb("status", _status_lb, "PCA 未啟用", u.TEXT3)
        else:
            _set_lb("status", _status_lb, "無裝置", u.DANGER)
        if force or _last_vals != [0] * CH_COUNT:
            for i in range(CH_COUNT):
                _set_row(i, 0)
            _last_vals = [0] * CH_COUNT
        if force or not nav.is_editing():
            _sync_slider()
        return

    # ── 目標卡 ──
    name = "0x{:02X}".format(int(rec.get("addr", 0)))
    if rec.get("broadcast"):
        name += " 廣播"
    if _multi_bus():
        name += "  B{}".format(int(rec.get("i2c", 0)))
    _set_lb("target", _target_lb, name, u.TEXT)

    if _target_focused:
        # 游標在清單上 = 還沒套用；右欄仍是「已套用」的目標，按確認才換。
        _set_lb("status", _status_lb, "按確認選擇", u.WARNING)
    elif _scan_pending:
        _set_lb("status", _status_lb, "掃描中…", u.WARNING)
    elif rec.get("broadcast"):
        n = len(rec.get("boards") or [])
        if n and rec.get("online"):
            _set_lb("status", _status_lb, "同步 {} 台".format(n), u.SUCCESS)
        elif n:
            _set_lb("status", _status_lb, "離線", u.DANGER)
        else:
            _set_lb("status", _status_lb, "無實體板", u.DANGER)
    elif rec.get("online"):
        _set_lb("status", _status_lb, "在線", u.SUCCESS)
    else:
        _set_lb("status", _status_lb, "離線", u.DANGER)

    # ── 16 通道 ──
    vals = _read_vals(rec)
    if force or vals != _last_vals:
        for i in range(CH_COUNT):
            _set_row(i, vals[i])
        _last_vals = vals

    if force or not nav.is_editing():
        _sync_slider()


def _set_row(i, v):
    """更新一列的長條長度與數值（v = 0..4095）。"""
    if i < 0 or i >= len(_rows):
        return
    v = max(0, min(4095, int(v)))
    r = _rows[i]

    w = int(BAR_W * v / 4095.0)
    try:
        if v <= 0:
            # 0 也留 1px：不動態隱藏 widget，避免 add_flag/clear_flag 的版本差異
            r["fill"].set_style_bg_color(u.C(u.TRACK), 0)
            r["fill"].set_size(1, BAR_H)
        else:
            # 非 0 至少 1px 的 PRIMARY：很小的值（例如 1）也看得到「有亮」
            if w < 1:
                w = 1
            r["fill"].set_style_bg_color(u.C(u.PRIMARY), 0)
            r["fill"].set_size(w, BAR_H)
    except Exception:
        pass

    sel = (i == _sel_ch)
    try:
        r["val"].set_text("%4d" % v)
        r["val"].set_style_text_color(
            u.C(u.PRIMARY if sel else (u.TEXT2 if v else u.TEXT3)), 0)
    except Exception:
        pass


def _paint_selection():
    """只重畫「哪一列被選中」（不重寫長條）。"""
    has = _cur_rec() is not None     # 沒有目標時不要假裝 CH0 被選中
    for i in range(len(_rows)):
        r = _rows[i]
        sel = has and (i == _sel_ch)
        try:
            r["box"].set_style_bg_color(u.C(u.FOCUS_BG if sel else u.SURFACE), 0)
            r["num"].set_style_text_color(u.C(u.PRIMARY if sel else u.TEXT3), 0)
        except Exception:
            pass
        v = _last_vals[i] if i < len(_last_vals) else 0
        try:
            r["val"].set_style_text_color(
                u.C(u.PRIMARY if sel else (u.TEXT2 if v else u.TEXT3)), 0)
        except Exception:
            pass


def _sync_slider():
    """把數值卡同步到「目前目標的目前通道」。"""
    rec = _cur_rec()
    if rec is None:
        v = 0
    else:
        v = _read_vals(rec)[_sel_ch]
    _set_lb("ch", _ch_lb, "CH{}".format(_sel_ch), u.TEXT)
    _set_lb("val", _val_lb, "%4d" % v, u.PRIMARY)
    try:
        _slider.set_value(v, 0)
    except Exception:
        pass
    _paint_selection()


# ══════════════════════════════════════════════════════════════════
#  控制
# ══════════════════════════════════════════════════════════════════
def _write(rec, ch, v):
    """寫一個通道。廣播時同步各單板快取。

    ★ 優先用 PCA9685.set_channel(ch, v)：只寫該通道的 4 bytes（約 0.5 ms）。
      舊版/show() 一次寫 64 bytes（約 6 ms @100 kHz）—— 每轉一格都寫會讓 UI 頓。
      若部署到的驅動還沒有 set_channel（例如舊的 port 完整副本），退回 buf+show()。
    """
    pca = rec.get("pca") if rec else None
    if pca is None:
        return
    v = max(0, min(4095, int(v)))
    try:
        if hasattr(pca, "set_channel"):
            pca.set_channel(ch, v)
        else:
            pca.buf[ch] = v
            pca.show()
    except Exception as e:
        print("[pca9685] write err:", e)
        return
    # 廣播：把同一筆值寫進各單板的「顯示快取」（不再逐一 show()，
    # 實體板已經由 0x70 廣播收到；這裡只是讓切回單板時的數值一致）。
    if rec.get("broadcast"):
        boards = rec.get("boards") or []
        for peer in _targets:
            if peer is rec or peer.get("broadcast"):
                continue
            if peer.get("i2c") != rec.get("i2c"):
                continue
            if peer.get("addr") not in boards:
                continue
            try:
                peer["pca"].buf[ch] = v
            except Exception:
                pass


def _fill(rec, v):
    """一次填滿 16 通道再 show()（全部熄滅用）。"""
    global _last_vals
    pca = rec.get("pca") if rec else None
    if pca is None:
        return
    v = max(0, min(4095, int(v)))
    for ch in range(CH_COUNT):
        try:
            pca.buf[ch] = v
        except Exception:
            pass
    if rec.get("broadcast"):
        boards = rec.get("boards") or []
        for peer in _targets:
            if peer is rec or peer.get("broadcast"):
                continue
            if peer.get("i2c") != rec.get("i2c"):
                continue
            if peer.get("addr") not in boards:
                continue
            for ch in range(CH_COUNT):
                try:
                    peer["pca"].buf[ch] = v
                except Exception:
                    pass
    try:
        pca.show()
    except Exception as e:
        print("[pca9685] show err:", e)
    for ch in range(CH_COUNT):
        _set_row(ch, v)
    _last_vals = [v] * CH_COUNT
    _sync_slider()


def _all_off():
    rec = _cur_rec()
    if rec is None:
        return
    _fill(rec, 0)
    print("[pca9685] all off ->", "0x{:02X}".format(int(rec.get("addr", 0))))


def _post(event):
    from lib.sys.sys_bus import bus
    q = bus.shared.get("_pca_actions")
    if not isinstance(q, list):
        q = []
    q.append(event)
    bus.shared["_pca_actions"] = q


def _do_scan():
    global _scan_pending, _scan_at
    _post({"action": "scan"})
    _scan_pending = True
    _scan_at = time.ticks_ms()
    _refresh_selected(force=True)
    print("[pca9685] scan requested")


def _poll_scan():
    global _scan_pending
    if not _scan_pending:
        return
    try:
        since = time.ticks_diff(time.ticks_ms(), _scan_at)
    except Exception:
        since = SCAN_TIMEOUT_MS + 1
    if since > SCAN_TIMEOUT_MS:
        _scan_pending = False


# ══════════════════════════════════════════════════════════════════
#  導覽回呼（層次 1/2/3）
# ══════════════════════════════════════════════════════════════════
def _on_target_focus(focused):
    """焦點進/出「控制目標」清單。

    清單的互動是「游標 + 確認才套用」：
      - 進來：游標先對齊已套用的目標（琥珀底）。
      - 離開：右欄/標題恢復正常狀態（藍底 = 已套用）。
    """
    global _target_focused, _cursor
    _target_focused = bool(focused)
    if focused:
        _cursor = _sel
    _sync_list()
    _refresh_selected()


def _commit_target():
    """把游標候選變成真正的控制目標（右欄立即切換）。"""
    global _sel
    if 0 <= _cursor < len(_targets):
        _sel = _cursor
    _sync_list()
    _refresh_selected(force=True)


def _on_target_move(dd):
    """清單上的 encoder：只移動游標，**不**改控制目標。

    使用者定案（2026-10）：轉動 = 預覽；按一下確認才是真正選擇。
    所以這裡只動 `_cursor`，`_sel`（右欄/寫入對象）保持不變。
    """
    global _cursor
    n = len(_targets)
    if not n:
        return
    _cursor = (_cursor + dd) % n
    _sync_list()


def _on_channel_move(dd):
    """方格編輯態：換通道（數值卡與焦點跟著換）。"""
    global _sel_ch
    _sel_ch = (_sel_ch + dd) % CH_COUNT
    _sync_slider()


def _on_value_adjust(dd):
    """數值卡編輯態：調目前通道的 12-bit 亮度（每次轉動即寫硬體）。"""
    rec = _cur_rec()
    if rec is None:
        return
    try:
        cur = int(_slider.get_value())
    except Exception:
        cur = 0
    v = max(0, min(4095, cur + int(dd) * STEP))
    try:
        _slider.set_value(v, 0)
    except Exception:
        pass
    _set_lb("val", _val_lb, "%4d" % v, u.PRIMARY)
    _set_row(_sel_ch, v)
    if _sel_ch < len(_last_vals):
        _last_vals[_sel_ch] = v
    _write(rec, _sel_ch, v)


def _on_value_focus(focused):
    """焦點移到數值卡時，先同步成目前通道的值（避免顯示上一個通道）。"""
    if focused:
        _sync_slider()


# ══════════════════════════════════════════════════════════════════
#  頁面接口
# ══════════════════════════════════════════════════════════════════
def on_enter():
    try:
        _refresh_targets(force=True)
    except Exception as e:
        # on_enter 由 app.go 直接呼叫（沒有 try）→ 這裡一定要自己擋，
        # 否則一筆壞資料就會讓「換頁」整個炸掉。
        print("[pca9685] enter refresh err:", e)
    nav.focus(ITEM_TARGET)  # 每次進頁都從「控制目標」開始


def on_leave():
    if nav.is_editing():
        nav.exit()
        _sync_list()


def on_enc(d):
    # 焦點在「控制目標」時，encoder **只移動游標**（琥珀底），不改 _sel。
    # 這是使用者定案：轉動 = 預覽、確認 = 真正選擇。
    # 沒有目標時不攔截 → 讓 encoder 能把焦點移到方格/掃描鈕。
    if _target_focused and _targets:
        _on_target_move(d)
        return
    nav.enc(d)
    if nav.current_kind() == ITEM_LIST:
        _sync_list()


def on_confirm():
    # 清單上的確認 = 真正選擇：套用游標 → 焦點直接移到通道方格。
    # （不進 nav 的 ITEM_LIST 編輯態，所以只按一次就完成選擇。）
    if _target_focused:
        nav.focus(ITEM_GRID)  # 套用後直接跳到通道方格；on_focus(False) 恢復一般狀態
        _commit_target()
        return None
    nav.confirm()
    if nav.current_kind() == ITEM_LIST:
        _sync_list()
    if not nav.is_editing():
        _sync_slider()
    return None


def on_exit():
    global _cursor
    if _target_focused and _cursor != _sel:
        # 游標還沒套用 → 返回鍵先取消（留在頁面）；再按一次才回 launcher。
        _cursor = _sel
        _sync_list()
        _refresh_selected(force=True)
        return True
    consumed = nav.exit()
    if nav.current_kind() == ITEM_LIST:
        _sync_list()
    if not nav.is_editing():
        _sync_slider()
    return consumed


def update(run):
    if run % REFRESH_EVERY != 0:
        return
    try:
        _poll_scan()
        _refresh_targets()
        _refresh_selected()
    except Exception as e:
        print("[pca9685] update err:", e)
