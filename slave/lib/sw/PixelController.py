from machine import Pin, I2C, SPI
import neopixel
import micropython
import gc
import utime
import math
import array
import os

try:
    from lib.sys.sys_bus import bus as _sys_bus
except Exception:
    _sys_bus = None

# ==================== PixelController ====================
class PixelController:
    """
    精簡版 pixel 控制器 - 專為高性能流式傳輸設計
    移除多餘 buffer，直接將數據從 Source 轉換至 Hardware Buffer
    """
    def __init__(self, pixel_type, pixel_io_cfg):
        self.pixel_type = pixel_type
        self.pixel_io = pixel_io_cfg
        self.hw = pixel_io_cfg['pixel_IO']
        self.num_pixels = pixel_io_cfg['Q']

        # 中性值（停止/熄燈/暫停時回填）＝ config 的 dStay（12-bit 0-4095，
        # >>4 = 8-bit 通道值），由 config 動態帶入、可隨時改；沒輸入才用預設 0。
        # 例：燈 0（熄滅）、電機 2048（=0x80 死區停）、PWM 可設任意停留亮度。
        self.neutral_value = (int(pixel_io_cfg.get("dStay", 0) or 0) >> 4) & 0xFF
        
        # 內部映射: 1:WS2812, 2:APA102, 3:i2c_pixel
        type_map = {'WS2812': 1, 'APA102': 2, 'i2c_pixel': 3}
        self._tid = type_map.get(pixel_type, 0)
        
        # 色序與通道處理
        order = pixel_io_cfg.get('order', 'GRB').upper()
        self.bpp = len(order)
        self._r = order.find('R')
        self._g = order.find('G')
        self._b = order.find('B')
        self._w = order.find('W')
        
        # 單幀大小 (輸入源統一定義為 R,G,B,W 每像素 4 bytes)
        self.frame_size = self.num_pixels * 4 

        # 全域亮度 (0-255, 255=全亮)。由 MODE_SET 的 brightness (沒輸入=255) 設定,
        # APA102 亮度頭 (0xE0|bri>>3) 在 _convert 讀此值。
        self.brightness = 255

    def st_init(self):
        """硬體初始化 hook；PixelStreamer.init() 會逐一呼叫。"""
        return None

    @micropython.native
    def st_load_and_convert(self, source_buffer, offset: int):
        """核心載入函數：調用 Viper 機器碼加速轉換"""
        if self.hw is None:
            return
        # 直接獲取硬體驅動的 Buffer 引用（Neopixel 存放在 .buf，其他自定義驅動通常也是）
        # 如果是 PCA9685/i2c 類型的，我們假設它有自定義 buf
        self._convert(source_buffer, offset, self.num_pixels, self._tid)

    @micropython.viper
    def _convert(self, source, offset: int, n: int, tid: int):
        src = ptr8(source)
        
        bpp = int(self.bpp)
        
        if tid == 1:  # WS2812 (RGB/GRB)
            dst = self.hw.buf
            ro = int(self._r)
            go = int(self._g)
            bo = int(self._b)
            wo = int(self._w)
            for i in range(n):
                s_idx = offset + (i << 2) # i * 4
                d_idx = i * bpp
                dst[d_idx + ro] = src[s_idx]     # R
                dst[d_idx + go] = src[s_idx + 1] # G
                dst[d_idx + bo] = src[s_idx + 2] # B
                if wo >= 0:
                    dst[d_idx + wo] = src[s_idx + 3]  # W (RGBW 燈珠: 不寫會恆 0)
                
        elif tid == 2: # APA102 (物理幀: [亮度頭, B, G, R]; R/G/B 依 config order 動態對應)
            dst = self.hw.spi_buffer
            ro = int(self._r)
            go = int(self._g)
            bo = int(self._b)
            bri = int(self.brightness) >> 3    # 全域亮度 0-255 → 5-bit (0-31)
            for i in range(n):
                s_idx = offset + (i << 2)
                d_idx = 4 + (i << 2)
                dst[d_idx + 0] = 0xE0 | bri    # 亮度頭 (0xE0 + 5-bit)
                dst[d_idx + 1] = src[s_idx + bo]  # B
                dst[d_idx + 2] = src[s_idx + go]  # G
                dst[d_idx + 3] = src[s_idx + ro]  # R

        elif tid == 3: # i2c_pixel (PCA9685)
            # 專門提取 W 通道 (src[+3]) 給 PWM 控制器
            dst = self.hw.buf
            ro = int(self._r)
            go = int(self._g)
            bo = int(self._b)
            wo = int(self._w)
            for i in range(n):
                s_idx = offset + (i << 2)
                w = src[s_idx + 3]
                dst[i] = (w << 4) | (w >> 4)

    def st_show(self):
        """觸發硬體顯示"""
        t = self._tid
        if t == 1: self.hw.write()
        elif t == 2: self.hw.show_raw() if hasattr(self.hw, 'show_raw') else self.hw.show()
        elif t == 3: self.hw.show() if hasattr(self.hw, 'show') else self.hw.sync_buffer()

    def set_address(self, addr):
        """i2c_pixel 專用：切換 PCA target address（0x70=ALLCALL，其餘=單板）。

        同一個 PixelController / buffer 可以準確切 0x70 / 0x40 / 0x41；
        不需要為每個地址各建一個 PixelController。回傳 True/False。
        """
        if self._tid != 3 or self.hw is None:
            return False
        fn = getattr(self.hw, "set_address", None)
        if fn is None:
            return False
        fn(addr)
        return True

    def set_brightness(self, value):
        """設定全域亮度 (0-255)。APA102 亮度頭在 _convert 讀此值, 5-bit 封頂由 >>3 做。"""
        self.brightness = max(0, min(255, int(value)))

    def __len__(self):
        return self.num_pixels

# ==================== PixelStreamer ====================
class PixelStreamer:
    """
    pixel 流式傳輸管理器 - 零拷貝高性能版
    """
    def __init__(self, controllers):
        self.controllers = controllers
        self.total_bytes = sum(c.frame_size for c in controllers)
        self.big_buffer = bytearray(self.total_bytes)
        self.offsets = []

        # ── 小緩衝 dirty gate（每個 PixelController Object 一格）────
        # 不是單一 global flag，也不是按 type 合併：每個 Object 各自 1 byte。
        # show_all() 只轉換+輸出 dirty=1 的那幾個 Object；沒改的 Object 直接跳過。
        # 同一個 type 有多個 Object 時，也各自獨立，不會被合併。
        self._dirty = bytearray(len(controllers))
        for i in range(len(self._dirty)):
            self._dirty[i] = 1      # 上電第一幀全送
        self._led_on = True         # /led_off 或 set_led_enabled(False) 可停硬體輸出
        self._stat_show = 0
        self._stat_skip = 0
        self._stat_gate_us = 0
        self._stat_show_us = 0
        self._stat_gate_n = 0
        self._stat_ctrl_show = [0] * len(controllers)
        self._stat_ctrl_skip = [0] * len(controllers)
        self._stat_ctrl_us = [0] * len(controllers)
        
        # 預計算偏移量，減少循環中的算力支出
        current_offset = 0
        for c in controllers:
            self.offsets.append(current_offset)
            current_offset += c.frame_size

        # 初始化就把每個 PixelController 的絕對範圍算好、按 byte 起點排序。
        # cell = 4 bytes（R,G,B,W）；之後 mark_dirty_range() 可直接用 cell/byte 範圍。
        self.ranges = []
        current_offset = 0
        for i, c in enumerate(controllers):
            fs = int(c.frame_size)
            self.ranges.append({
                "i": i,
                "type": getattr(c, "pixel_type", "?"),
                "start_byte": current_offset,
                "end_byte": current_offset + fs,
                "start_cell": current_offset >> 2,
                "count": fs >> 2,
            })
            current_offset += fs
        self.ranges.sort(key=lambda r: r["start_byte"])

    def init(self):
        for c in self.controllers:
            c.st_init()
        for i in range(len(self._dirty)):
            self._dirty[i] = 1
        # /led_off 檔案：開機直接熄燈（先推一幀中性值，之後 show_all 全跳過）。
        try:
            if os.path.exists("/led_off"):
                self._led_on = True
                self.clear_all()
                self._led_on = False
                for i in range(len(self._dirty)):
                    self._dirty[i] = 0
        except Exception:
            pass
        if _sys_bus is not None:
            try:
                _sys_bus.shared["led_enabled"] = self._led_on
            except Exception:
                pass
        print(f"[Streamer] Ready. Total Buffer: {self.total_bytes} bytes | led={'on' if self._led_on else 'off'}")
        try:
            rs = ", ".join("#{} {}[{}+{})".format(r["i"], r["type"], r["start_cell"], r["count"])
                           for r in self.ranges)
            print("[Streamer] ranges: " + rs)
        except Exception:
            pass

    def get_write_view(self):
        """獲取原始緩衝供外部填充數據；寫完請呼叫 mark_dirty()。"""
        return self.big_buffer

    def mark_dirty(self, which=None):
        """標記有修改；which=None/\"all\" 代表全部。

        which 可用 index、PixelController object、pixel_type
        （WS2812/APA102/i2c_pixel），或頁面別名（rgb / apa / pca）。
        同一個 type 有多個 Object 時，會全部 mark。
        """
        d = self._dirty
        n = len(d)
        if which is None:
            for i in range(n):
                d[i] = 1
            return
        if isinstance(which, int):
            if 0 <= which < n:
                d[which] = 1
            return
        for i in range(n):
            if self.controllers[i] is which:
                d[i] = 1
                return
        w = str(which).lower()
        if w in ("all", "*", ""):
            for i in range(n):
                d[i] = 1
            return
        for i in range(n):
            t = str(getattr(self.controllers[i], "pixel_type", "")).lower()
            hit = (t == w)
            if not hit and w in ("rgb", "ws", "ws2812") and t == "ws2812":
                hit = True
            if not hit and w in ("apa", "apa102") and t == "apa102":
                hit = True
            if not hit and w in ("pca", "pca9685", "i2c", "i2c_pixel") and t == "i2c_pixel":
                hit = True
            if hit:
                d[i] = 1

    def mark_dirty_range(self, start, count=1, unit="cell"):
        """用絕對範圍精準標記：只 mark 跟這個範圍重疊的 PixelController Object。

        unit="cell"（預設，4 bytes/cell）或 "byte"。回傳這次新 mark 的 controller 數。
        """
        if unit == "byte":
            s = int(start)
            e = s + int(count)
        else:
            s = int(start) << 2
            e = s + (int(count) << 2)
        n = 0
        for r in self.ranges:
            if r["end_byte"] <= s or e <= r["start_byte"]:
                continue
            i = r["i"]
            if not self._dirty[i]:
                self._dirty[i] = 1
                n += 1
        return n

    def set_led_enabled(self, on):
        """硬體輸出總開關。關閉時先推一幀中性值（真熄燈），之後 show_all 全跳過。"""
        on = bool(on)
        if not on and self._led_on:
            self._led_on = True
            try:
                self.clear_all()
            except Exception:
                pass
        self._led_on = on
        for i in range(len(self._dirty)):
            self._dirty[i] = 0 if not on else 1
        if _sys_bus is not None:
            try:
                _sys_bus.shared["led_enabled"] = on
            except Exception:
                pass
        print("[Streamer] LED", "on" if on else "off")

    def stats(self):
        """回傳 (show, skip, gate_avg_us, show_avg_us, gate_overhead_pct)，累計。"""
        gn = self._stat_gate_n
        gate_avg = self._stat_gate_us // gn if gn else 0
        show_avg = self._stat_show_us // self._stat_show if self._stat_show else 0
        total = gate_avg + show_avg
        overhead = (gate_avg * 100000.0 / total) / 1000.0 if total else 0.0
        return (self._stat_show, self._stat_skip, gate_avg, show_avg, overhead)

    def ctrl_stats(self):
        """每個 Object 的 (index, type, show, skip, avg_us, start_cell, count)。"""
        out = []
        for i, c in enumerate(self.controllers):
            n = self._stat_ctrl_show[i]
            avg = self._stat_ctrl_us[i] // n if n else 0
            out.append((i, getattr(c, "pixel_type", "?"), n,
                        self._stat_ctrl_skip[i], avg,
                        self.offsets[i] >> 2, c.frame_size >> 2))
        return out

    @micropython.native
    def show_all(self, force=False):
        """只輸出 dirty=1 的 controller；全部乾淨或 LED 關閉時整幀跳過。"""
        t0 = utime.ticks_us()
        if not self._led_on:
            self._stat_skip += 1
            for i in range(len(self._dirty)):
                self._stat_ctrl_skip[i] += 1
            self._stat_gate_us += utime.ticks_diff(utime.ticks_us(), t0)
            self._stat_gate_n += 1
            return False
        d = self._dirty
        any_d = 0
        for i in range(len(d)):
            any_d |= d[i]
        if not force and not any_d:
            self._stat_skip += 1
            for i in range(len(d)):
                self._stat_ctrl_skip[i] += 1
            self._stat_gate_us += utime.ticks_diff(utime.ticks_us(), t0)
            self._stat_gate_n += 1
            return False
        t1 = utime.ticks_us()
        buf = self.big_buffer
        offs = self.offsets
        for i in range(len(self.controllers)):
            if not force and not d[i]:
                self._stat_ctrl_skip[i] += 1
                continue
            ct0 = utime.ticks_us()
            ctrl = self.controllers[i]
            # 1. 搬運與轉換
            ctrl.st_load_and_convert(buf, offs[i])
            
            # 2. 硬體輸出
            ctrl.st_show()
            d[i] = 0            # 這一格渲染完馬上釋放 dirty
            self._stat_ctrl_show[i] += 1
            self._stat_ctrl_us[i] += utime.ticks_diff(utime.ticks_us(), ct0)
        t2 = utime.ticks_us()
        self._stat_show += 1
        self._stat_gate_us += utime.ticks_diff(t1, t0)
        self._stat_show_us += utime.ticks_diff(t2, t1)
        self._stat_gate_n += 1
        return True

    def clear_all(self):
        """熄燈/停機：填中性值（燈=0 熄滅，motor=0x80 死區停），再推一幀到硬體。

        不能全清 0 —— UART-412 的 0 = 全速正轉！中性值取各 controller 的
        neutral_value（PixelController 無此屬性 → 0 = 熄燈；UartMotor = 0x80 停）。
        與 stream_task._build_neutral() 同源。
        """
        buf = self.big_buffer
        offs = self.offsets
        for i, c in enumerate(self.controllers):
            neutral = getattr(c, "neutral_value", 0)
            off = offs[i]
            for k in range(c.num_pixels):
                o = off + (k << 2)
                buf[o] = 0
                buf[o + 1] = 0
                buf[o + 2] = 0
                buf[o + 3] = neutral
        self.mark_dirty()
        self.show_all(force=True)

    def stop_motors(self):
        """暫停：電機填中性值（0x80 死區停）歸位，燈保持最後一幀，再推一幀到硬體。

        與 clear_all() 的差異：只動電機 controller（pixel_type="uartMotor1"），
        燈光通道保持 big_buffer 原值（暫停前最後一幀），所以燈不熄。
        big_buffer 的電機 W 通道被改成中性值 — 恢復播放後新幀會覆寫，無需還原。
        """
        buf = self.big_buffer
        offs = self.offsets
        for i, c in enumerate(self.controllers):
            if getattr(c, "pixel_type", "") != "uartMotor1":
                continue
            neutral = getattr(c, "neutral_value", 0)
            off = offs[i]
            for k in range(c.num_pixels):
                buf[off + (k << 2) + 3] = neutral
        self.mark_dirty()
        self.show_all(force=True)

    def close(self):
        for c in self.controllers:
            c.is_active = False
        gc.collect()

# ==================== 測試腳本 ====================
if __name__ == '__main__':
    # 1. 模擬硬體初始化
    # WS2812 組 (假設 10 顆燈)
    np_io = neopixel.NeoPixel(Pin(15, Pin.OUT), 10)
    ctrl_ws = PixelController('WS2812', {'pixel_IO': np_io, 'Q': 10, 'order': 'GRB'})

    # 模擬 PCA9685 (這裡使用一個假的物件來模擬，實際使用時傳入 PCA 物件)
    class FakePCA:
        def __init__(self): self.buf = bytearray(16)
        def show(self): pass 
            
    pca_io = FakePCA()
    ctrl_pca = PixelController('i2c_pixel', {'pixel_IO': pca_io, 'Q': 16, 'order': 'W'})

    # 2. 啟動 Streamer
    streamer = PixelStreamer([ctrl_ws, ctrl_pca])
    streamer.init()

    # 3. 測試循環
    print("🚀 開始測試高性能流式循環...")
    source = streamer.get_write_view()
    angle = 0.0
    
    try:
        for frame in range(200):
            # 模擬產生算法數據 (R,G,B,W 順序)
            for i in range(len(streamer.big_buffer) // 4):
                idx = i * 4
                s = (math.sin(angle + i * 0.2) + 1) * 127
                source[idx]     = int(s)          # R
                source[idx + 1] = 0               # G
                source[idx + 2] = 255 - int(s)    # B
                source[idx + 3] = int(s)          # W (供 PCA 使用)
            
            # 使用高性能接口渲染
            streamer.mark_dirty()
            streamer.show_all()
            
            angle += 0.1
            if frame % 50 == 0:
                print(f"Frame {frame} | Free Mem: {gc.mem_free()} bytes")
            utime.sleep_ms(10)
            
    except KeyboardInterrupt:
        pass

    streamer.close()
    print("🏁 測試結束")
