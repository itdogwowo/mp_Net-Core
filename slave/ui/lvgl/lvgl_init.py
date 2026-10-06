# ui/lvgl/lvgl_init.py — LVGL display 一次初始化 + bus reuse
#
# PARTIAL mode — LVGL 渲染小 buffer(40 行),flush_cb 取像素 + swap + 送 SPI。
#
# 螢幕方向:
#   - LVGL 自己送 MADCTL(0x60 橫屏),讓 ST7789 framebuffer 旋轉。
#   - show 用 bus adapter 的 set_window(繞過 ST7789.set_window 的 x/y swap)。
#   - 重要:config TFT.rotation 必須維持 0,否則 double-rotate。
#
# 注意:LVGL 必須跑在 CPU0(MicroPython 主執行緒)。
#   測試確認:_thread(CPU1) + 完整 UI(多 widget)會崩潰(GC/stack 跨核競態)。
#   與 lvgl-micropython 專案一致:Python 層只用一核,CPU1 工作在 C 層做。
import time
import lvgl as lv
from lib.sys.sys_bus import bus

_LINES = 40    # PARTIAL draw buffer 行數
_BPP = 2       # RGB565
_SERVICE = "lvgl_disp"
_MADCTL = 0x60  # 橫屏 MV|MX(ST7789);改 0x00 為直屏

_W = 320
_H = 240


class LvglDisp:
    """LVGL display + slave new LCD 平台。構造一次後放 bus reuse。
    提供 app 要的 platform 介面:{tick, take, show, enc_delta, confirm, exit}。"""

    def __init__(self):
        self.lcd = bus.get_service("lcd")
        if self.lcd is None:
            raise RuntimeError("lcd not on bus — 先跑 boot.py")
        self._bus = getattr(self.lcd, "_bus", None)
        if self._bus is None:
            raise RuntimeError("lcd service missing _bus (adapter)")

        self.W = _W
        self.H = _H
        self._dirty = []
        self._last_tick = time.ticks_ms()

        # 送 MADCTL(讓 framebuffer 橫屏)。
        self._bus.write_cmd_data(0x36, bytes([_MADCTL]))

        # ══════════════════════════════════════════════════════════════
        #  LVGL 初始化：軟重開機後**不能沿用**，只能走全新 init
        # ══════════════════════════════════════════════════════════════
        # 舊版寫的是：
        #     if lv.is_initialized(): lv.deinit()
        #     lv.init(); disp = lv.display_create(W, H)
        # 軟重開機之後那條路會炸：
        #     [ERROR] ❌ [Core 0] Failed to start lvgl:
        #             memory allocation failed, allocating 1634034300 bytes
        # 那個數字**不是尺寸，是指標**（每次都不一樣）。
        #
        # 有一版改成「沿用 C 層還活著的 display」，結果也不對 —— 真相是：
        #
        # ★ LVGL 的**每一筆配置**都在 MicroPython 的 GC heap 上
        #   （ext_mod/lvgl/mem_core.c: lv_malloc_core → gc_alloc），
        #   而 binding 的 root pointer 是普通 C 全域，soft reset 不會清它
        #   （gen/lvgl_api_gen_mpy.py: `void *mp_lv_roots` +
        #    `static bool mp_lv_roots_initialized`，實測在 .bss
        #    0x3fcaceec / 0x3fcacef4，軟重開機不動）。
        #   ⇒ 軟重開機後 LVGL 的每一棵樹都是**死指標**。
        #
        # 真機實測（軟重開機後攔在 REPL、main.py 未執行）：
        #     lv.is_initialized()          -> True       ← 乾淨開機時**也是 True**（見下）
        #     display_get_default()        -> 有物件      ← ★ 這才是殘留的判準
        #       └ resolution               -> 320 x 240   ← 讀得到（舊 heap 還沒被覆寫）
        #     screen_active() 子物件數       -> 14         ← 上一個 session 的 UI 樹
        #     lv.display_create(320,240)   -> OK
        #     ★ lv.obj() / lv.label() / lv.screen_load() / lv.deinit()
        #                                  -> **直接打死板子**（USB-CDC 消失）
        #
        #   ⇒ 「沿用同一個 Object」不可行：能沿用的不是一個堪用的 display，
        #     是一整棵死指標樹。唯一保證可靠的路是**硬重置一次**。
        #
        # 所以這裡只保留「全新 init」一條路。
        # ★ 不要去「偵測殘留」——真機實測在**乾淨開機**時：
        #       is_initialized() = True   （binding 在 import 時就做掉 C 層初始化）
        #       display_get_default() = None
        #   而在**殘留**時兩者都可能是 True + 非 None，但也可能被中途的
        #   heap 活動打亂成別的組合 —— 用旗標判斷不可靠，而且會把正常開機
        #   誤判成殘留（實際踩過一次：UI 被自己的守門擋掉）。
        #   殘留的判斷交給 _soft_reboot_recover()（標記檔是外部狀態、不會騙人），
        #   而且它會直接 machine.reset() —— 不會走到這裡。
        lv.init()
        self._disp = lv.display_create(self.W, self.H)
        if self._disp is None:
            # 走到這裡代表 C 層是壞的（lv_global 沒被重生）。這條路在真機上
            # 只在「軟重開機且守門沒攔到」時出現 —— 留明確訊息方便定位。
            raise RuntimeError(
                "display_create 回 None — LVGL 的 C 層狀態不乾淨"
                "（軟重開機殘留）。請硬重置（RST / 斷電）。")

        self._disp.set_color_format(18)  # RGB565
        buf = bytearray(self.W * _LINES * _BPP)
        self._disp.set_buffers(buf, None, len(buf), 0)  # PARTIAL
        self._disp.set_flush_cb(self._flush_cb)
        print("[lvgl_init] {}x{} MADCTL=0x{:02X} PARTIAL lines={}  (全新 init)".format(
            self.W, self.H, _MADCTL, _LINES))

    def _flush_cb(self, disp_drv, area, color_p):
        """LVGL 渲染一塊 → 拷貝到 bytes(PARTIAL 單緩衝必須拷貝)+ 立即 flush_ready。"""
        w = area.x2 - area.x1 + 1
        h = area.y2 - area.y1 + 1
        data = color_p.__dereference__(w * h * _BPP)
        lv.draw_sw_rgb565_swap(data, w * h)
        self._dirty.append((area.x1, area.y1, area.x2, area.y2, bytes(data)))
        disp_drv.flush_ready()

    # ---- platform 介面(app.step 用) ----
    def tick(self):
        # 真實時間差:幀時間 >5ms 時 tick_inc(5) 會讓 LVGL 內部時鐘越跑越慢
        now = time.ticks_ms()
        diff = time.ticks_diff(now, self._last_tick)
        self._last_tick = now
        if diff > 0:
            lv.tick_inc(diff)
        lv.task_handler()
        lv.refr_now(self._disp)

    def take(self):
        rects = self._dirty
        self._dirty = []
        return rects

    def show(self, x1, y1, x2, y2, data):
        self._bus.set_window(x1, y1, x2, y2)
        self._bus.write_data_async(data)
        self._bus.flush()

    def enc_delta(self):
        return 0   # 預設;encoder 由 board 覆寫

    def confirm(self):
        return False   # 預設;confirm 由 board 覆寫

    def exit(self):
        return False


def get_platform():
    """取得 LVGL 平台(bus service "lvgl_disp")。
    已初始化過就 reuse;沒有就建立一次並註冊進 bus。

    ★ 這裡是 LVGL 唯一的初始化入口,soft-reboot 自我修復就掛在這裡。
      **不放 boot.py** —— 那是硬體初始化,子系統的復原屬於子系統自己;
      而且探測要在 LVGL 真正要被建起來的那一刻做,heap 越乾淨越安全。
    """
    existing = bus.get_service(_SERVICE)
    if existing is not None:
        return existing

    if not _soft_reboot_recover():
        # 只在 machine.reset() 沒生效時走到這裡 —— 不要硬幹(會踩死指標)
        raise RuntimeError("LVGL soft-reboot 復原失敗(reset 未生效)")

    plat = LvglDisp()
    bus.register_service(_SERVICE, plat)
    _mark_write("owned=1,reset=0")
    return plat


def is_ready():
    """LVGL 是否已初始化並在 bus 上。"""
    return bus.get_service(_SERVICE) is not None


# ══════════════════════════════════════════════════════════════════════════
#  Soft-reboot 自我修復
# ══════════════════════════════════════════════════════════════════════════
#  為什麼需要:軟重開機(friendly REPL 的 Ctrl-D／mpremote 預設收尾)之後
#  LVGL 一定起不來。真根因(C 源碼 + ELF + 真機三路確認):
#
#    ext_mod/lvgl/mem_core.c   lv_malloc_core() → gc_alloc()
#        → LVGL 的**每一筆配置**(display/screen/widget/timer/anim/style…)
#          都在 MicroPython 的 GC heap 上
#    gen/lvgl_api_gen_mpy.py   void *mp_lv_roots                    ← 普通 C 全域
#                              static bool mp_lv_roots_initialized ← 也是常駐
#        ELF 實證:3fcaceec B mp_lv_roots
#                  3fcacef4 b mp_lv_roots_initialized$7  (兩個都在 .bss)
#
#    軟重開機把 GC heap 整個重來,卻不清這兩個 C 全域 →
#      (a) `lv_global`(在舊 heap 上)變成死指標
#      (b) `mp_lv_init_gc()` 因為旗標還是 true,再也不會重建它
#    ⇒ LVGL 整棵樹都是死指標。
#
#  真機實測(軟重開機後攔在 REPL、main.py 未執行):
#      display_get_default()      -> 上一個 session 的 display ← ★ 判準
#        └ 解析度                -> 320 x 240  (還讀得到,舊 heap 沒被覆寫)
#      screen_active() 子物件數   -> 14         (上一個 session 的 UI 樹)
#      lv.obj() / lv.label() / lv.screen_load() / lv.deinit()
#                                 -> **直接打死板子**(USB-CDC 消失)
#
#  ⚠️ 兩個踩過的陷阱:
#    1. `lv.is_initialized()` **不能**當判準 —— 這個 binding 在 import
#       `lvgl` 時就會做掉 C 層初始化,乾淨開機時它也可能是 True。
#       拿它判斷會誤判、把自己的 UI 擋掉(真的發生過)。
#    2. 純 Python 沒有任何辦法救 —— 試過 4 種配方全失敗,包括
#       `lv.mp_lv_deinit_gc()` + `lv.mp_lv_init_gc()`(能清掉 Python 側狀態,
#       但這個 binding 沒把真正的 `lv_init()` 匯出到 Python,治不了 C 層)。
#       ⇒ 唯一保證可靠的路是**硬重置一次**。
#
#  判準 vs 動作(重要區分):
#    - **判準是 LVGL 專屬的**:只有 LVGL 知道自己的 C 層有沒有跨過 soft reboot。
#      i80/rgb/dsi 要看 esp_lcd handle、SPI 是 `SPI(sid).deinit()` ——
#      同一個問題,但每個子系統的探針都不一樣。
#    - **動作(硬重置)是系統層的**:它會一起清掉內部 SRAM 上殘留的
#      `heap_caps.malloc(CAP_DMA)` 佔用(見 Skills/buffer-conventions 規則一,
#      DMA 緩衝在內部 SRAM,soft reboot **不會**釋放)。
#    所以這一整包留在 LVGL 的初始化檔裡是**暫時的**:它目前的唯一價值是
#    修 LVGL。等到第二個子系統也真的需要「探測殘留 → 硬重置」,再把
#    「標記檔 + 防無窮重置」那段 boilerplate 抽成 lib/sys/ 的共用模組,
#    各子系統註冊自己的探針(＝ mp_lcd_bus 計畫書 M1/M2 的形狀)。
#    在那之前不預先抽象 —— 一個呼叫者的抽象是猜測。
#
#  標記檔 /lvgl_state: `owned=<0|1>,reset=<0|1>`
#      recover  讀標記 → reset=1 就清掉、直接放行(防無窮重置)
#               ★ 讀必須在寫「之前」,否則會蓋掉自己的訊號(踩過)
#               然後探測 display_get_default() → 非 None 就 reset
#      note     LvglDisp 建起來 → owned=1,reset=0
#      mark_ready()  UI 完整起來(board._setup) → 清標記
#
#  防無窮重置:reset=1 是「點數」。硬重置後重入會消耗掉它;降到 0 之後若又
#  偵測到殘留,會再重置一次(重新累積 1 點)→ 最壞是「每兩次開機重置一次」,
#  不會卡死、也不會無限快速重置。
#
#  代價:每次軟重開機多一次約 8 秒的硬重置 + USB 重新列舉。
#
#  根治(待做,需重編韌體):把 `mp_lv_roots_initialized` 從 function-local
#  static 換成 `MP_STATE_VM`(soft reset 會清),`mp_lv_init_gc()` 就會在
#  新 heap 上重建 `lv_global`。改法見 todo/08_lvgl_reinit.md §4。
#  做完這一整段就可以刪掉。
# ══════════════════════════════════════════════════════════════════════════
_MARK = "/lvgl_state"


def _mark_read():
    try:
        with open(_MARK) as f:
            return f.read()
    except Exception:
        return ""


def _mark_write(txt):
    try:
        f = open(_MARK, "w")
        f.write(txt)
        f.close()
    except Exception as e:
        print("[lvgl_init] soft-reboot 標記寫入失敗:", e)


def _mark_clear():
    import os
    try:
        os.remove(_MARK)
    except Exception:
        pass


def _decide(mark_txt):
    """讀完標記後的決策:'skip'(不再重置) | 'probe'(要探測)。可離線測。"""
    if "reset=1" in mark_txt:
        return "skip"
    return "probe"


def _residual():
    """LVGL 的 C 層是否還握著上一個 session 的 display(＝soft-reboot 殘留)。

    唯一可靠的判準是 `display_get_default()`:
        乾淨開機     -> None(這個 session 還沒建過 display)
        軟重開機殘留 -> 指向上一個 session 的 display
    (`is_initialized()` 不行,見上面陷阱 1。)
    """
    try:
        return lv.display_get_default() is not None
    except Exception as e:
        print("[lvgl_init] 殘留探測失敗,放棄守門:", e)
        return False


def _soft_reboot_recover():
    """在 LVGL 被初始化之前呼叫。回傳 True = 可以往下走。

    偵測到 soft-reboot 殘留時會 `machine.reset()`(**不會回來**)。
    """
    if _decide(_mark_read()) == "skip":
        print("[lvgl_init] 已是重置後的重入 → 清標記繼續")
        _mark_clear()
        return True

    if not _residual():
        return True

    print("[lvgl_init] 偵測到 soft-reboot 殘留的 LVGL 狀態 → hard reset")
    _mark_write("owned=1,reset=1")   # ★ 必須在 reset 之前寫,否則重置後會無限循環
    try:
        import machine
        machine.reset()
    except Exception as e:
        print("[lvgl_init] reset 失敗:", e)
    return False                     # 走到這裡代表 reset 沒生效


def mark_ready():
    """UI 完整起來(board._setup) → 清掉標記(本輪乾淨收尾)。"""
    _mark_clear()
