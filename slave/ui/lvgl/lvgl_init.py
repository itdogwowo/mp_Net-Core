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
        #  LVGL 初始化：軟重開機後**沿用同一個 display**，絕不重新 create
        # ══════════════════════════════════════════════════════════════
        # 舊版寫的是：
        #     if lv.is_initialized(): lv.deinit()
        #     lv.init(); disp = lv.display_create(W, H)
        # 軟重開機（Ctrl-D／mpremote 預設收尾）之後那條路會炸：
        #
        #     [ERROR] ❌ [Core 0] Failed to start lvgl:
        #             memory allocation failed, allocating 1634034300 bytes
        #
        # 那個數字**不是尺寸，是指標**（每次跑都不一樣：3254793227 / 1009759640 /
        # 1634034300）。逐步量出來的因果是：
        #
        #     lv.is_initialized()            -> True     ← C 層確實還活著
        #     lv.display_get_default()       -> 有物件
        #       └ resolution                 -> 320 x 240 ← **好的！**
        #     lv.deinit()                    -> 回 OK，但 is_initialized() 仍 True
        #       └ 這一步之後 resolution 變成 (1009033516, 8029) ← 壞了
        #     lv.display_create(320,240)     -> MemoryError
        #
        # ★ 所以元兇是 **deinit**：它沒有真的把狀態清乾淨，反而把一個**還堪用**的
        #   display 弄成半死。而就算 deinit 有效，`display_create` 也會在舊的還在
        #   的時候再建一個 —— 那也是錯的。
        # ★ 為什麼不能「刪掉舊的再建」：軟重開機把 MicroPython 的整個 heap 重來，
        #   LVGL 記的那些指標已經不屬於它了。實測 `disp.delete()` → 直接 hard fault。
        #
        # 正確做法（＝使用者提的「重複使用同一個 Object」）：
        #   **C 層還在就直接拿回來用**，只把 buffers / flush_cb 重新裝上去。
        #   實測可行：set_buffers + set_flush_cb + screen_load + task_handler
        #   全部 OK，flush_cb 真的被呼叫（畫面有出來）。
        self._reused = False
        self._disp = None
        if lv.is_initialized():
            try:
                self._disp = lv.display_get_default()
            except Exception as e:
                print("[lvgl_init] display_get_default 失敗:", e)
                self._disp = None
        if self._disp is not None:
            self._reused = True
        else:
            lv.init()
            self._disp = lv.display_create(self.W, self.H)

        self._disp.set_color_format(18)  # RGB565
        buf = bytearray(self.W * _LINES * _BPP)
        self._disp.set_buffers(buf, None, len(buf), 0)  # PARTIAL
        self._disp.set_flush_cb(self._flush_cb)
        self._drop_stale()
        print("[lvgl_init] {}x{} MADCTL=0x{:02X} PARTIAL lines={}{}".format(
            self.W, self.H, _MADCTL, _LINES,
            "  (沿用既有 display)" if self._reused else "  (全新 init)"))

    def _drop_stale(self):
        """把「目前畫面」從上一個執行週期留下來的 screen 換掉（只在沿用時做）。

        軟重開機後 C 層的 `act_scr` 還指著舊的 widget 樹，而那些物件已經被
        MicroPython 的 heap 重來回收掉了 —— 那是**死指標**。
        換上一個全新的空 screen，renderer 就不會再去走訪它。

        ★ 刻意**只做最小的一件事**：
          - 不呼叫 `lv.anim_delete()`。它會走訪殘留的動畫鏈，而那些節點同樣是
            死指標 —— 我沒有真機證據說它安全，就不放進來。
          - 不做任何 free/delete。記憶體已經不屬於 LVGL，實測 `disp.delete()`
            直接 hard fault。
          殘留的動畫計時器到期時最多是「找不到物件而不動作」，比主動去走訪安全。

        ★ app 自己也會載入新 screen（`app.cur` 初值是 None，所以
          `app.go("launcher")` 不會 early-return）。這裡先墊一次是為了
          「就算那條路以後改了，LVGL 也不會踩到死 screen」。
        """
        if not self._reused:
            return
        try:
            lv.screen_load(lv.obj())
        except Exception as e:
            print("[lvgl_init] 換掉殘留 screen 失敗:", e)

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
    已初始化過就 reuse;沒有就建立一次並註冊進 bus。"""
    existing = bus.get_service(_SERVICE)
    if existing is not None:
        return existing
    plat = LvglDisp()
    bus.register_service(_SERVICE, plat)
    return plat


def is_ready():
    """LVGL 是否已初始化並在 bus 上。"""
    return bus.get_service(_SERVICE) is not None
