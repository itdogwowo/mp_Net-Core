import machine
import micropython
import array
import time

class APA102:
    """
    APA102 極速驅動 - 專為 LEDcontroller 配套設計
    特性：雙緩衝、Viper 轉換、對齊 LEDcontroller 的 buf 操作
    """
    def __init__(self, spi, num_leds,  baudrate=8_000_000):
        self.n = num_leds
        self.buf_length = num_leds * 4
        
        # 1. 暴露給 LEDcontroller 的標準緩衝區 [G, R, B, W]
        # 注意：為了符合你 LEDcontroller 的 set_rgb 邏輯與 f.readinto 的性能
        self.buf = bytearray(self.buf_length)
        
        # 2. SPI 物理傳輸數據區 (原生 APA102 格式)
        # 整合 Start + Data + End 為單一緩衝區以避免 SPI 分段寫入造成的時序問題
        self.start_len = 4
        self.end_len = max(4, (num_leds + 15) // 16)
        self.spi_total_len = self.start_len + self.buf_length + self.end_len
        self.spi_buffer = bytearray(self.spi_total_len)
        
        # 3. SPI 硬體初始化
        self.spi = spi
        
        # 初始化 SPI 緩衝區 (Start=0x00, End=0xFF, Data Header=0xE0)
        self._init_spi_buffer()

    @micropython.viper
    def _init_spi_buffer(self):
        """初始化物理緩衝區：Start(0x00) + Data(0xE0) + End(0xFF)"""
        p_spi: ptr8 = ptr8(self.spi_buffer)
        buf_len: int = int(self.buf_length)
        start_len: int = int(self.start_len)
        end_len: int = int(self.end_len)
        
        # 1. Start Frame (0x00)
        for i in range(start_len):
            p_spi[i] = 0x00
            
        # 2. Data Frame Headers (0xE0)
        # Data starts at offset start_len
        for i in range(0, buf_len, 4):
            p_spi[start_len + i] = 0xE0
            
        # 3. End Frame (0x00)
        # End starts at start_len + buf_len
        # 修正：使用 0x00 替代 0xFF，避免下一顆未使用的燈珠將其誤判為全亮信號 (Phantom White Pixel)
        end_start: int = start_len + buf_len
        for i in range(end_len):
            p_spi[end_start + i] = 0x00

    @micropython.viper
    def _convert(self):
        """
        Viper 內核：將 LEDcontroller 寫入的 [G, R, B, W] 轉換為 [0xE0|W, B, G, R]
        寫入到 spi_buffer 的中間數據區
        直接由 show() 調用
        """
        p_in: ptr8 = ptr8(self.buf)
        p_out: ptr8 = ptr8(self.spi_buffer)
        n: int = int(self.buf_length)
        offset: int = int(self.start_len) # Offset for data in spi_buffer
        
        for i in range(0, n, 4):
            # 讀取 LEDcontroller 規範的四字節 (假設最後一字節為亮度)
            g = p_in[i]
            r = p_in[i+1]
            b = p_in[i+2]
            w = p_in[i+3]
            
            # 寫入 APA102 格式 (亮度位 0xE0 + 5-bit)
            p_out[offset + i]     = 0xE0 | (w >> 3) 
            p_out[offset + i + 1] = b
            p_out[offset + i + 2] = g
            p_out[offset + i + 3] = r

    def show_raw(self):
        """
        🚀 快車道：直接輸出整合後的緩衝區
        前提：self.spi_buffer 已準備好 (通常由 _convert 或外部直接寫入)
        """
        # 單次調用底層寫入，解決分段寫入導致的時序問題
        self.spi.write(self.spi_buffer)
            
    def show(self):
        """物理輸出"""
        self._convert()
        self.show_raw()
        
    def write(self):
        """相容 LEDcontroller 的調用習慣"""
        self.show_raw()

    def fill(self, color):
        """相容 neopixel 接口"""
        g, r, b = color # 預設三元組
        for i in range(0, self.buf_length, 4):
            self.buf[i] = g
            self.buf[i+1] = r
            self.buf[i+2] = b
            self.buf[i+3] = 255 # 預設滿亮度

