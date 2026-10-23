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
    from ui.lvgl.page import remote
except Exception as _e:
    print("[page] remote import skip:", _e)
    remote = None
try:
    # ESP-NOW 設定頁（**傳輸層**）：從 remote.py 拆出來的，見 todo/07_now_setting_ui.md
    from ui.lvgl.page import now_setting
except Exception as _e:
    print("[page] now_setting import skip:", _e)
    now_setting = None
# 2026-10-24 暫時停用 APA / RGB 兩頁（WS2812 2000 的 50fps 不可能，先用 PCA）。
# 檔案保留；要恢復時把下面兩個 try-import 與 _PAGES_MOD 的兩行加回即可。
# try:
#     from ui.lvgl.page import pixel_lab_apa
# except Exception as _e:
#     print("[page] pixel_lab_apa import skip:", _e)
#     pixel_lab_apa = None
# try:
#     from ui.lvgl.page import pixel_lab_rgb
# except Exception as _e:
#     print("[page] pixel_lab_rgb import skip:", _e)
#     pixel_lab_rgb = None
try:
    from ui.lvgl.page import pixel_lab_pca
except Exception as _e:
    print("[page] pixel_lab_pca import skip:", _e)
    pixel_lab_pca = None
try:
    from ui.lvgl.page import settings
except Exception as _e:
    print("[page] settings import skip:", _e)
    settings = None
try:
    from ui.lvgl.page import pixel_controller
except Exception as _e:
    print("[page] pixel_controller import skip:", _e)
    pixel_controller = None

# 把模組引用補進 registry(給 app 呼叫 on_enc/on_confirm/update 用)。
# 用 if 守護:頁面 import 失敗時 PAGES 裡不會有它,跳過不報錯。
_PAGES_MOD = [
    ("remote", remote),
    ("now_setting", now_setting),
    ("pixel_lab_pca", pixel_lab_pca),
    ("settings", settings),
    ("pixel_controller", pixel_controller),
]
for _pid, _mod in _PAGES_MOD:
    if _mod is not None and _pid in registry.PAGES:
        registry.PAGES[_pid]["mod"] = _mod
