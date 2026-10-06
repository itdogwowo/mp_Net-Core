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
        #   殘留的判斷交給 soft_reboot_guard（標記檔是外部狀態、不會騙人），
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

    ★ 這裡是 LVGL 唯一的初始化入口 —— soft-reboot 自我修復守門就掛在這裡
      （不放 boot.py：那是硬體初始化，不該為了 LVGL 弄髒；
        而且探測要在 LVGL 真正要被建起來的那一刻做，heap 越乾淨越安全）。
      詳見 ui/lvgl/soft_reboot_guard.py。
    """
    existing = bus.get_service(_SERVICE)
    if existing is not None:
        return existing

    from ui.lvgl import soft_reboot_guard
    if not soft_reboot_guard.recover():
        # 只在 machine.reset() 沒生效時走到這裡 —— 不要硬幹（會踩死指標）
        raise RuntimeError("LVGL soft-reboot 守門無法復原（reset 未生效）")

    plat = LvglDisp()
    bus.register_service(_SERVICE, plat)
    soft_reboot_guard.note_owned()
    return plat


def is_ready():
    """LVGL 是否已初始化並在 bus 上。"""
    return bus.get_service(_SERVICE) is not None
