# ui_test_tool.py — LVGL UI 獨立測試入口
#
# 跟 tft_test_tool.py 同一層。假設 boot.py 已跑完(LCD 已在 bus、字型已就位)。
# 測試用入口:import 即進主迴圈(仍需 LCD 存在)。
#
# ★這是一個完整、獨立、可直接操作的入口:
#   import ui_test_tool           # 一行即可。直接進主迴圈,旋鈕+按鈕操作。
#                                 # Ctrl-C 回 REPL,LVGL 留 bus reuse(不壞)。
#
# REPL 除錯 API(主迴圈跑時用不到,REPL 才用):
#   ui_test_tool.pages()          # 列出已註冊頁面
#   ui_test_tool.goto("settings") # 跳頁(主迴圈跑時也可用)
#   ui_test_tool.peek("control_panel") # 看 bus 值
#   ui_test_tool.set("control_panel", {"mode":2})  # 設 bus 值,看頁面反應
import sys

# LVGL 資源路徑
_SRC = "/ui/lvgl/src"
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_started = False
_plat = None
_running = False


def _log(msg):
    print("[ui_test] " + msg)


def start():
    """★主入口:初始化 + 進主迴圈(旋鈕+按鈕直接操作)。
    Ctrl-C 回 REPL。重複呼叫安全。
    這就是「完整獨立入口」——import 後即可操作。"""
    global _started, _plat
    _init()
    run()


def _init():
    """初始化 LVGL + 預建頁面 + 接輸入(不進主迴圈)。"""
    global _started, _plat
    if _started:
        return

    from ui.lvgl import lvgl_init, app, ui_common

    # LVGL display:一次初始化 + bus reuse
    _plat = lvgl_init.get_platform()
    ui_common.init(_plat)
    ui_common.init_fonts()

    # 註冊所有頁面 → 預建 screen
    try:
        import ui.lvgl.page  # noqa: F401
    except ImportError as e:
        _log("page import fail: {}".format(e))
    app.build_all()

    # 輸入:encoder + 確認(encC) + 離開(btn),跟 board 共用 _make_inputs
    _enc_delta, _confirm, _exit = _make_inputs_safe()

    app.init({
        "tick": _plat.tick,
        "take": _plat.take,
        "show": _plat.show,
        "enc_delta": _enc_delta,
        "confirm": _confirm,
        "exit": _exit,
    })
    app.go("launcher")
    _refresh()
    _started = True
    _log("ready — {} page(s), 旋鈕=導覽/調值 · encC=確認 · btn=返回".format(_page_count()))
    pages()


def _make_inputs_safe():
    """接實體輸入;失敗則 no-op(不阻擋啟動)。"""
    try:
        from ui.lvgl import board
        return board._make_inputs()
    except Exception as e:
        _log("inputs init fail (改用 no-op): {}".format(e))
        return (lambda: 0, lambda: False, lambda: False)


def run():
    """進主迴圈(旋鈕+按鈕操作)。Ctrl-C 乾淨回 REPL。
    若已在跑則不重入。"""
    global _running
    if _running:
        _log("already running")
        return
    _init()
    _running = True
    from ui.lvgl import app
    import time
    _log("▶ running — Ctrl-C 回 REPL")
    try:
        while True:
            try:
                app.step()
            except Exception as e:
                _log("loop err: {}".format(e))
            time.sleep_ms(5)
    except KeyboardInterrupt:
        _log("⏹ stopped (回 REPL,LVGL 保留)")
    finally:
        _running = False


def _page_count():
    from ui.lvgl import registry
    return len(registry.PAGES)


def _refresh():
    """渲染一幀並送到 LCD。"""
    if _plat is None:
        return
    _plat.tick()
    for rect in _plat.take():
        _plat.show(*rect)


# ══════════════════════════════════════════════════════════════
#  REPL 除錯 API(主迴圈跑時用不到,REPL 才用)
# ══════════════════════════════════════════════════════════════

def pages():
    """列出已註冊頁面(id / title / order)。"""
    from ui.lvgl import registry
    _log("registered pages ({}):".format(len(registry.PAGES)))
    for m in registry.ordered():
        print("  {:02d}  {:14s}  {}".format(m["order"], m["id"], m.get("title", "")))


def goto(name):
    """跳到指定頁面。主迴圈跑時也可用(下幀就切)。"""
    _ensure()
    from ui.lvgl import app, registry
    if name != "launcher" and name not in registry.PAGES:
        _log("unknown page: {}".format(name))
        return
    app.go(name)
    if not _running:
        _refresh()
    _log("-> {}".format(name))

# go 是 goto 的短別名(相容舊呼叫)
go = goto


def frame(n=1):
    """跑 N 幀(沒進主迴圈時,看單次渲染)。"""
    _ensure()
    from ui.lvgl import app
    import time
    for _ in range(n):
        app.step()
        time.sleep_ms(5)
    _log("ran {} frame(s)".format(n))


def peek(key="control_panel", default=None):
    """看 bus.shared[key]。"""
    from lib.sys.sys_bus import bus
    v = bus.shared.get(key, default)
    print("bus.shared[{}] = {}".format(key, v))
    return v


def set(key, value):
    """設 bus.shared[key]=value(測試頁面讀 bus 的反應)。
    例:set("control_panel", {"mode":2,"brightness":20})"""
    from lib.sys.sys_bus import bus
    bus.shared[key] = value
    _log("bus.shared[{}] = {}".format(key, value))


def cur():
    """看當前頁面。"""
    _ensure()
    from ui.lvgl import app
    _log("current page: {}".format(app.cur))


# ══════════════════════════════════════════════════════════════

def _ensure():
    if not _started:
        raise RuntimeError("not initialized — call start() first")


# import 即用(對齊 tft_test_tool 的 if __name__ 慣例,但這裡 import 就跑)
if __name__ == "__main__":
    start()
