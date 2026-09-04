from machine import Pin, I2C, SPI
import neopixel
import micropython
import gc
import utime
import math
import array

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
        
        # 預計算偏移量，減少循環中的算力支出
        current_offset = 0
        for c in controllers:
            self.offsets.append(current_offset)
            current_offset += c.frame_size

    def init(self):
        for c in self.controllers:
            c.st_init()
        print(f"[Streamer] Ready. Total Buffer: {self.total_bytes} bytes")

    def get_write_view(self):
        """獲取原始緩衝供外部填充數據"""
        return self.big_buffer

    @micropython.native
    def show_all(self):
        """執行一幀完整的渲染流程"""
        buf = self.big_buffer
        offs = self.offsets
        for i in range(len(self.controllers)):
            ctrl = self.controllers[i]
            # 1. 搬運與轉換
            ctrl.st_load_and_convert(buf, offs[i])
            
            # 2. 硬體輸出
            ctrl.st_show()

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
        self.show_all()

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
        self.show_all()

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
            streamer.show_all()
            
            angle += 0.1
            if frame % 50 == 0:
                print(f"Frame {frame} | Free Mem: {gc.mem_free()} bytes")
            utime.sleep_ms(10)
            
    except KeyboardInterrupt:
        pass

    streamer.close()
    print("🏁 測試結束")
