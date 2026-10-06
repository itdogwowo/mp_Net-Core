# ui/lvgl/soft_reboot_guard.py — LVGL 的 soft-reboot 自我修復
#
# ══════════════════════════════════════════════════════════════════════════
#  為什麼需要這個檔案
# ══════════════════════════════════════════════════════════════════════════
#  軟重開機（friendly REPL 的 Ctrl-D／mpremote 預設收尾）之後 LVGL 一定起不來。
#  真根因（C 源碼 + ELF + 真機三路確認）：
#
#    ext_mod/lvgl/mem_core.c   lv_malloc_core() → gc_alloc()
#        → LVGL 的**每一筆配置**（display / screen / widget / timer / anim /
#          style…）都在 MicroPython 的 GC heap 上
#    gen/lvgl_api_gen_mpy.py   void *mp_lv_roots                  ← 普通 C 全域
#                              static bool mp_lv_roots_initialized ← 也是常駐
#        ELF 實證：3fcaceec B mp_lv_roots
#                  3fcacef4 b mp_lv_roots_initialized$7   （兩個都在 .bss）
#
#    軟重開機把 GC heap 整個重來，卻不清這兩個 C 全域 →
#      (a) `lv_global`（在舊 heap 上）變成死指標
#      (b) `mp_lv_init_gc()` 因為旗標還是 true，再也不會重建它
#    ⇒ LVGL 整棵樹都是死指標。
#
#  真機實測（軟重開機後攔在 REPL、main.py 未執行）：
#      display_get_default()        -> 上一個 session 的 display（★ 殘留判準）
#        └ 解析度                    -> 320 x 240 ← 還讀得到（舊 heap 沒被覆寫）
#      screen_active() 子物件數       -> 14        ← 上一個 session 的 UI 樹
#      lv.display_create(320,240)   -> OK
#      lv.obj() / lv.label() / lv.screen_load() / lv.deinit()
#                                   -> **直接打死板子**（USB-CDC 消失）
#
#  ⚠️ 兩個踩過的陷阱（別再犯）：
#    1. `lv.is_initialized()` **不能**當殘留判準 —— 這個 binding 在 import
#       `lvgl` 時就會做掉 C 層初始化，乾淨開機時它也可能是 True。
#       拿它判斷會誤判、把自己的 UI 擋掉（真的發生過）。
#    2. 純 Python 沒有任何辦法救 —— 試過 4 種配方全部失敗，包括
#       `lv.mp_lv_deinit_gc()` + `lv.mp_lv_init_gc()`（能清掉 Python 側狀態，
#       但這個 binding 沒把真正的 `lv_init()` 匯出到 Python，治不了 C 層）。
#       ⇒ 唯一保證可靠的路是**硬重置一次**：實測硬重置後
#         `display_get_default() is None`、`display_create()` 成功、UI 正常。
#
# ══════════════════════════════════════════════════════════════════════════
#  放在哪裡、誰呼叫
# ══════════════════════════════════════════════════════════════════════════
#  **不放在 boot.py。** boot.py 是硬體初始化，不該為了 LVGL 弄髒；
#  而且探測必須在 LVGL 真正要被初始化的那一刻做（heap 越乾淨越安全）。
#
#      lvgl_init.get_platform()   ← 唯一入口，進來先 recover()
#      lvgl_init.LvglDisp.__init__ ← 建完 display 後 note_owned()
#      board._setup()             ← UI 完整起來後 mark_ready()
#
#  這也對齊 `mp_lcd_bus/PLAN_lcd_bus_hardening_and_selfowned.md` 的 M2：
#  「`make_new` 偵測殘留 → `esp_restart()` 保險」——檢查放在物件自己的入口。
#
# ══════════════════════════════════════════════════════════════════════════
#  狀態機
# ══════════════════════════════════════════════════════════════════════════
#  標記檔 /lvgl_state：`owned=<0|1>,reset=<0|1>`
#
#      recover()  讀標記
#         reset=1  → 上一輪就是為了這件事重置回來的
#                    → 清標記、回 False（不再重置；就算重置沒救到也只多一次）
#      然後探測 lvgl.display_get_default()
#         非 None  → 殘留 → 標記改 reset=1 → machine.reset()（不會回來）
#         None     → 乾淨 → 回 True
#
#      note_owned()  寫下 owned=1,reset=0（本輪有 LVGL 在手上）
#      mark_ready()  UI 完整起來 → 清掉標記
#
#  代價：每次軟重開機多一次約 8 秒的硬重置 + USB 重新列舉。
#
# ══════════════════════════════════════════════════════════════════════════
#  根治（待做，需重編韌體）
# ══════════════════════════════════════════════════════════════════════════
#  把 `mp_lv_roots_initialized` 從 function-local static 換成 `MP_STATE_VM`
#  （soft reset 會清），`mp_lv_init_gc()` 就會在新 heap 上重建 `lv_global`。
#  改法見 `todo/08_lvgl_reinit.md` §4。做完這個檔案就可以整包刪掉。
#
# ══════════════════════════════════════════════════════════════════════════
#  可以推廣到 i80 / rgb / dsi 嗎？
# ══════════════════════════════════════════════════════════════════════════
#  可以，而且介面就是照那個形狀留的（`probe` 注入 + 共用標記檔邏輯）。
#  I80 那份計畫書的 M1/M2 是同一件事：soft reset 不拆 esp_lcd/ISR/DMA，
#  所以要在 `make_new` 偵測殘留。
#  但**現在不做**，理由：
#    - i80/rgb/dsi 的殘留是「可 cleanup」的（deinit 外設就好），LVGL 不是
#      （配置在死掉的 heap 上，無從救起）；兩者策略不同，硬併會互相牽制。
#    - 目前只有一個呼叫者。等第二個真的出現，再把「標記檔 + 防無窮重置」
#      這段抽成 `lib/sys/soft_reboot.py`，各子系統註冊自己的 probe。
#      在那之前先讓它待在 LVGL 自己的地盤裡 —— 一個呼叫者的抽象是猜測。

_LVGL_MARK = "/lvgl_state"


# ── 標記檔 ────────────────────────────────────────────────────────────────

def _mark_read():
    try:
        with open(_LVGL_MARK) as f:
            return f.read()
    except Exception:
        return ""


def _mark_write(txt):
    try:
        f = open(_LVGL_MARK, "w")
        f.write(txt)
        f.close()
    except Exception as e:
        print("[lvgl_guard] 標記寫入失敗:", e)


def _mark_clear():
    import os
    try:
        os.remove(_LVGL_MARK)
    except Exception:
        pass


# ── 純決策（可離線測，不碰硬體）────────────────────────────────────────────

def _decide(mark_txt):
    """讀完標記後的決策：'skip'（不再重置）| 'clear'（乾淨）| 'probe'（要探測）"""
    if "reset=1" in mark_txt:
        return "skip"
    return "probe"


# ── 殘留探測 ──────────────────────────────────────────────────────────────

def _residual():
    """LVGL 的 C 層是否還留著上一個 session 的 display（＝軟重開機殘留）。

    唯一可靠的判準是 `display_get_default()`：
        乾淨開機     -> None（這個 session 還沒建過 display）
        軟重開機殘留 -> 指向上一個 session 的 display
    （`is_initialized()` 不行，見檔頭陷阱 1。）
    """
    try:
        import lvgl as lv
        return lv.display_get_default() is not None
    except Exception as e:
        print("[lvgl_guard] 探測失敗，放棄守門:", e)
        return False


# ── 對外 ──────────────────────────────────────────────────────────────────

def recover():
    """在 LVGL 被初始化之前呼叫。回傳 True = 可以往下走。

    偵測到 soft-reboot 殘留時會 `machine.reset()`（**不會回來**）。
    """
    decision = _decide(_mark_read())

    if decision == "skip":
        print("[lvgl_guard] 已是重置後的重入 → 清標記繼續")
        _mark_clear()
        return True

    if not _residual():
        return True

    print("[lvgl_guard] 偵測到 soft-reboot 殘留的 LVGL 狀態 → hard reset")
    _mark_write("owned=1,reset=1")      # ★ 必須在 reset 之前寫，否則重置後會無限循環
    try:
        import machine
        machine.reset()
    except Exception as e:
        print("[lvgl_guard] reset 失敗:", e)
    return False                        # 走到這裡代表 reset 沒生效


def note_owned():
    """LVGL 已經在這個 session 建起來了 → 留下所有權標記。
    （下一次開機若看到它還在，代表這一輪沒乾淨收尾）"""
    _mark_write("owned=1,reset=0")


def mark_ready():
    """UI 完整起來 → 清掉標記（本輪乾淨收尾）。"""
    _mark_clear()
