# lv_ui_fx.py — 動態效果 helper
#
# 對應設計稿 CSS 動效（用 lv_binding_micropython 的 anim API）:
#   pulse()    ← pulse / live-ping / mon-ping（呼吸閃爍:livedot、tag dot、狀態燈）
#   fade_in()  ← rise / fade-in / set-in / tile-in（載入淡入 + 位移）
#   bar_grow() ← bar-grow（數值/進度成長）
#   set_state_colors() ← :hover/:active/:focus（焦點/按壓換色,配合 set_focus 外框）
#
# 注意:此 binding 的 anim API 是 lv.anim_t()（沒有 lv.anim()）:
#   - set_time() 不存在 → 用 set_duration()（LVGL 9）
#   - 呼吸來回用 set_reverse_duration()（播放完反向播）
#   - 無限重複用 repeat_count = 0xFFFF（LV_ANIM_REPEAT_INFINITE）
# anim 不可用時自動降級為「直接設定最終值」,不影響功能。
import lvgl as lv

_ANIM_CLASS = getattr(lv, "anim_t", None)
_REPEAT_INF = 0xFFFF  # LV_ANIM_REPEAT_INFINITE


def _anim_start(a):
    """建立 anim 後呼叫 init() + start()（對齊官方範例）。"""
    try:
        a.init()
    except Exception:
        pass
    a.start()
    return a


def pulse(wid, period_ms=1500, min_opa=110, max_opa=255):
    """呼吸閃爍:opa 在 min/max 間往返,永續播放。CSS 'pulse' 對應。"""
    if _ANIM_CLASS is None:
        wid.set_style_opa(max_opa, 0)
        return None
    half = max(80, period_ms // 2)
    a = _ANIM_CLASS()
    a.set_var(wid)
    a.set_values(max_opa, min_opa)
    a.set_duration(half)
    a.set_reverse_duration(half)
    a.set_repeat_count(_REPEAT_INF)
    a.set_custom_exec_cb(lambda _a, v: wid.set_style_opa(int(v), 0))
    return _anim_start(a)


def fade_in(wid, dy=6, time_ms=300, delay_ms=0):
    """載入淡入 + 向上位移。CSS 'rise'/'fade-in' 對應。"""
    x, y = wid.get_x(), wid.get_y()
    if _ANIM_CLASS is None:
        wid.set_style_opa(255, 0)
        return None
    a = _ANIM_CLASS()
    a.set_var(wid)
    a.set_values(y + dy, y)
    a.set_duration(time_ms)
    a.set_delay(delay_ms)
    a.set_custom_exec_cb(lambda _a, v: wid.set_pos(x, int(v)))
    _anim_start(a)

    b = _ANIM_CLASS()
    b.set_var(wid)
    b.set_values(0, 255)
    b.set_duration(time_ms)
    b.set_delay(delay_ms)
    b.set_custom_exec_cb(lambda _a, v: wid.set_style_opa(int(v), 0))
    _anim_start(b)
    return (a, b)


def bar_grow(bar, from_val=0, to_val=None, time_ms=400):
    """進度/數值成長動畫。CSS 'bar-grow' 對應。"""
    to_val = to_val if to_val is not None else bar.get_value()
    if _ANIM_CLASS is None:
        bar.set_value(to_val, 0)
        return None
    a = _ANIM_CLASS()
    a.set_var(bar)
    a.set_values(from_val, to_val)
    a.set_duration(time_ms)
    a.set_custom_exec_cb(lambda _a, v: bar.set_value(int(v), 0))
    return _anim_start(a)


def set_state_colors(wid, on, color_on, color_off, part=0):
    """依狀態切換文字/元件顏色。CSS ':hover/:active/:focus' 換色對應。
    on=True → color_on(例如焦點 PRIMARY),False → color_off。"""
    wid.set_style_text_color(
        lv.color_hex(color_on if on else color_off), part)
