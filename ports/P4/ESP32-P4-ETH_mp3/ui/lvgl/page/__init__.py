# ui/lvgl/page/__init__.py — 集中 import 所有頁面(保證全部 @register)
#
# 新增頁面流程:
#   1. 建立 ui/lvgl/page/xxx.py,在 build() 前加 @register(id="xxx", ...)
#   2. 下面 try-import 區塊加一行、_PAGES_MOD 加一行。
#   動態 launcher 會自動出現該頁面。
#
# 容錯:單一頁面 import 失敗(檔被刪/語法錯)不會拖垮其他頁。
#   try-import 跳過失敗的頁;_set_mod 用 if pid in PAGES 守護。
from ui.lvgl import registry

# 嘗試 import 每個頁面模組(觸發 @register);失敗就跳過,不中斷整個 package。
try:
    from ui.lvgl.page import control_panel
except Exception as _e:
    print("[page] control_panel import skip:", _e)
    control_panel = None
try:
    from ui.lvgl.page import pca9685
except Exception as _e:
    print("[page] pca9685 import skip:", _e)
    pca9685 = None
try:
    from ui.lvgl.page import settings
except Exception as _e:
    print("[page] settings import skip:", _e)
    settings = None

# 把模組引用補進 registry(給 app 呼叫 on_enc/on_confirm/update 用)。
# 用 if 守護:頁面 import 失敗時 PAGES 裡不會有它,跳過不報錯。
_PAGES_MOD = [
    ("control_panel", control_panel),
    ("pca9685", pca9685),
    ("settings", settings),
]
for _pid, _mod in _PAGES_MOD:
    if _mod is not None and _pid in registry.PAGES:
        registry.PAGES[_pid]["mod"] = _mod
