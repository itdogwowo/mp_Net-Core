# ui/lvgl/app.py — 動態註冊式 UI 主程式
#
# 採「扁平設計」(對齊 mp_LVGL/ui/lvgl_ui_app.py):
#   build_all() 一次性預建所有頁 screen,widget 永駐,go() 沿用不 rebuild。
#   省去 rebuild 的 widget 生命週期問題,啟動時 widget 全部就位。
#
# 平台解耦:所有硬體透過 platform 物件注入,本檔不 import 任何硬體。
import lvgl as lv
import ui.lvgl.registry as registry
import ui.lvgl.launcher as launcher

platform = None
cur = None
_last_scr = None
_run = 0
_screens = {}   # page_id → 預建 screen(reuse 模式,widget 永駐)


def init(plat):
    global platform
    platform = plat


def build_all():
    """預建所有頁面 screen。build 後 widget 全部就位。
    必須在 page import 後呼叫。單頁 build 失敗不拖垮其他頁(跳過該頁)。"""
    global _screens
    _screens = {"launcher": launcher.build()}
    for pid in list(registry.PAGES):
        try:
            _screens[pid] = registry.PAGES[pid]["build"]()
        except Exception as e:
            print("[app] build skip {}: {}".format(pid, e))
    print("[app] build_all: {} screen(s) pre-built".format(len(_screens)))


def _page():
    if cur == "launcher":
        return launcher
    meta = registry.get(cur)
    if meta is not None:
        return meta.get("mod")
    return launcher


def go(name, back=False):
    """切換頁面(沿用預建 screen,不 rebuild)。"""
    global cur, _last_scr
    if name == cur:
        return
    if name != "launcher" and name not in registry.PAGES:
        return
    if name not in _screens:
        return   # 沒預建過(理論上 build_all 後都有)

    old = _page()
    if hasattr(old, "on_leave"):
        old.on_leave()

    scr = _screens[name]
    try:
        lv.screen_load(scr)
    except Exception:
        pass
    _last_scr = scr

    cur = name
    print("[nav] ->", name)
    new = _page()
    if hasattr(new, "on_enter"):
        new.on_enter()


def step():
    """單幀處理。"""
    global _run
    d = platform["enc_delta"]()
    c = platform["confirm"]()
    ex = platform["exit"]()
    m = _page()

    if d != 0 and hasattr(m, "on_enc"):
        m.on_enc(d)
    if c and hasattr(m, "on_confirm"):
        target = m.on_confirm()
        if target:
            go(target)
    if ex and cur != "launcher":
        # 先讓頁面 on_exit 處理(例如退出編輯態);回 True=消耗,不回 launcher
        consumed = hasattr(m, "on_exit") and m.on_exit()
        if not consumed:
            go("launcher", back=True)

    if hasattr(m, "update"):
        m.update(_run)
    _run += 1

    platform["tick"]()
    for rect in platform["take"]():
        platform["show"](*rect)
    return 1


def run():
    while True:
        step()
        _sleep(5)


def _sleep(ms):
    try:
        import time
        time.sleep_ms(ms)
    except Exception:
        pass
