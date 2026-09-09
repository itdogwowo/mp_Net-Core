# ui/lvgl/page/pca9685.py — PCA9685 I2C 檢查/示範播放頁(橫屏 320×240)
#
# 導覽用共用 nav helper:enc 選(掃描鈕/模式/目標)→ confirm 動作。
#   掃描鈕(btn):confirm 觸發掃描。
#   模式/目標(enum):confirm 循環選項。
# 16 通道方格唯讀顯示亮度(從 bus 讀)。
# 進頁即播放(on_enter post 事件)、退頁全熄(on_leave post 事件)。
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_BUTTON, ITEM_ENUM

MODE_LABELS = ["逐腳呼吸", "全腳呼吸"]

nav = Nav()
scr = None
_scan_btn = _mode_lb = _target_lb = None
_dots = []


@register(id="pca9685", title="PCA9685", icon="sun",
          desc="I2C PWM 檢查器", order=2, accent=0x1A73E8)
def build():
    global scr, _scan_btn, _mode_lb, _target_lb, _dots
    _dots = []
    nav.reset()

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    # 掃描鈕 + mode + target(頂部)
    _scan_btn = u.mk_btn(scr, "掃描", 12, 6, 64, 28, "primary")
    nav.add(_scan_btn, ITEM_BUTTON, on_change=_do_scan)

    u.mk_label(scr, "模式", 84, 10, u.TEXT2, u.ZH)
    mbox = lv.obj(scr)
    mbox.set_size(70, 24)
    mbox.set_pos(116, 6)
    _box_style(mbox)
    _mode_lb = u.mk_label(mbox, MODE_LABELS[0], 8, 4, u.TEXT, u.ZH)
    nav.add(mbox, ITEM_ENUM, on_change=_cycle_mode)

    u.mk_label(scr, "目標", 196, 10, u.TEXT2, u.ZH)
    tbox = lv.obj(scr)
    tbox.set_size(72, 24)
    tbox.set_pos(228, 6)
    _box_style(tbox)
    _target_lb = u.mk_label(tbox, "廣播", 6, 4, u.TEXT, u.ZH)
    nav.add(tbox, ITEM_ENUM, on_change=_cycle_target)

    # 16 通道方格(左右各 8:左 ch0-7,右 ch8-15)
    u.mk_label(scr, "通道亮度", 12, 42, u.TEXT2, u.ZH)
    _build_dots(scr)

    nav.paint()
    return scr


def _box_style(o):
    o.set_style_bg_color(u.C(u.SURFACE), 0)
    o.set_style_radius(6, 0)
    o.set_style_border_color(u.C(u.BORDER), 0)
    o.set_style_border_width(1, 0)
    o.set_style_pad_all(0, 0)
    o.remove_flag(lv.obj.FLAG.SCROLLABLE)


def _build_dots(parent):
    """左欄 ch0-7(x=20)、右欄 ch8-15(x=180),各 8 個直排方格。"""
    sz = 14
    gap = 4
    cols = [20, 180]
    for col in range(2):
        x0 = cols[col]
        for row in range(8):
            idx = col * 8 + row
            x = x0
            y = 62 + row * (sz + gap)
            d = lv.obj(parent)
            d.set_size(sz, sz)
            d.set_pos(x, y)
            d.set_style_radius(3, 0)
            d.set_style_bg_color(u.C(u.TRACK), 0)
            d.set_style_border_color(u.C(u.BORDER), 0)
            d.set_style_border_width(1, 0)
            d.set_style_pad_all(0, 0)
            d.remove_flag(lv.obj.FLAG.SCROLLABLE)
            _dots.append(d)
            lb = lv.label(parent)
            lb.set_text(str(idx))
            lb.set_pos(x + sz + 2, y - 1)
            lb.set_style_text_font(u.F_NUM_S, 0)
            lb.set_style_text_color(u.C(u.TEXT3), 0)


def _state():
    from lib.sys.sys_bus import bus
    s = bus.shared.get("pca9685")
    if not isinstance(s, dict):
        s = {}
        bus.shared["pca9685"] = s
    return s


def _save(**kw):
    from lib.sys.sys_bus import bus
    s = bus.shared.get("pca9685")
    if not isinstance(s, dict):
        s = {}
    s.update(kw)
    bus.shared["pca9685"] = s


def _devices():
    from lib.sys.sys_bus import bus
    return list(bus.shared.get("_pca_devices", []) or [])


def _post(event):
    from lib.sys.sys_bus import bus
    q = bus.shared.get("_pca_actions")
    if not isinstance(q, list):
        q = []
    q.append(event)
    bus.shared["_pca_actions"] = q


def _do_scan():
    _post({"action": "scan"})
    print("[pca9685] scan requested")


def _cycle_mode():
    cur = int(_state().get("mode", 0))
    nxt = (cur + 1) % len(MODE_LABELS)
    _mode_lb.set_text(MODE_LABELS[nxt])
    _save(mode=nxt)


def _cycle_target():
    devs = _devices()
    opts = [-1] + devs
    cur = int(_state().get("target", -1))
    ci = opts.index(cur) if cur in opts else 0
    tgt = opts[(ci + 1) % len(opts)]
    _target_lb.set_text("廣播" if tgt == -1 else "0x{:02X}".format(tgt))
    _save(target=tgt)


def on_enter():
    s = _state()
    _post({"action": "start", "mode": s.get("mode", 0),
           "target": s.get("target", -1)})

def on_leave():
    if nav.is_editing():
        nav.exit()
    _post({"action": "stop", "alloff": True})

def on_enc(d):
    nav.enc(d)

def on_confirm():
    nav.confirm()
    return None

def on_exit():
    return nav.exit()

def update(run):
    if run % 8 != 0:
        return
    try:
        s = _state()
        for i in range(16):
            v = int(s.get("ch{}".format(i), 0))
            v = max(0, min(4095, v))
            ratio = v / 4095.0
            if ratio > 0.05:
                _dots[i].set_style_bg_color(u.C(u.PRIMARY), 0)
                _dots[i].set_style_opa(int(80 + 175 * ratio), 0)
            else:
                _dots[i].set_style_bg_color(u.C(u.TRACK), 0)
                _dots[i].set_style_opa(255, 0)
    except Exception:
        pass
