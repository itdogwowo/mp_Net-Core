import os
import gc
import machine ,time

def _sleep_ms(_ms):
    time.sleep_ms(_ms)

class VideoStreamReader:
    def __init__(self, filename, frame_size=1024 * 1024):
        self.filename = filename
        self.frame_size = frame_size
        self.file_size = os.stat(filename)[6]
        self.total_frames = self.file_size // frame_size
        
        # 保持文件打开状态避免重复打开开销
        self.file = open(self.filename, "rb")
        
        # 预分配可重用缓冲区
        self._buffer = bytearray(frame_size)
        self._buf_mv = memoryview(self._buffer)
        
    def read_frame(self, frame_index):
        """读取单个指定索引的帧"""
        if frame_index < 0 or frame_index >= self.total_frames:
            return None
            
        offset = frame_index * self.frame_size
        bytes_to_read = min(self.frame_size, self.file_size - offset)
        
        self.file.seek(offset)
        bytes_read = self.file.readinto(self._buf_mv)
        return self._buf_mv[:bytes_read] if bytes_read < self.frame_size else self._buf_mv

    def read_sequential(self):
        """顺序读取下一帧（最高效的方法）"""
        bytes_read = self.file.readinto(self._buf_mv)
        if bytes_read == 0:
            # 文件结束，重置到开头
            self.file.seek(0)
            bytes_read = self.file.readinto(self._buf_mv)
        
        return self._buf_mv[:bytes_read] if bytes_read < self.frame_size else self._buf_mv

    def stream_frames_in_range(self, start_frame=0, end_frame=None, step=1, loop=False):
        """
        生成器：按指定范围流式读取帧
        优化：使用顺序读取方法提高性能
        """
        # 参数校验和默认值处理
        if start_frame < 0:
            start_frame = 0
            
        if end_frame is None or end_frame > self.total_frames:
            end_frame = self.total_frames
            
        # 计算实际需要读取的帧数
        frame_count = end_frame - start_frame
        if frame_count <= 0 or start_frame >= self.total_frames:
            return

        # 直接使用顺序读取方法
        self.file.seek(start_frame * self.frame_size)
        
        frames_to_read = frame_count
        while True:
            # 读取指定范围内的帧
            for _ in range(frames_to_read):
                frame = self.read_sequential()
                if frame is not None:
                    yield frame
            
            # 如果不是循环模式，则退出
            if not loop:
                break
                
            # 重置文件指针到起始位置
            self.file.seek(start_frame * self.frame_size)

    # 上下文管理器支持
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.file.close()


# ====== 通用TFT驅動類 ======
class TFT:
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=320,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None, variant=0,
                 chunk_size=0):
        if adapter is not None:
            self._bus = adapter
            self.spi = getattr(adapter, '_spi', None)
            self.dc  = getattr(adapter, '_dc', None)
            self.cs  = getattr(adapter, '_cs', None)
            self.rst = getattr(adapter, '_rst', None)
        else:
            from lib.sys.bus_adapter import SpiBusAdapter
            self.spi = spi
            self.dc = dc
            self.cs = cs
            self.rst = rst
            if self.dc is not None:
                self.dc.init(machine.Pin.OUT, value=0)
            if self.cs is not None:
                self.cs.init(machine.Pin.OUT, value=1)
            if self.rst is not None:
                self.rst.init(machine.Pin.OUT, value=1)
            self._bus = SpiBusAdapter(spi, dc, cs, rst)

        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self.bytes_per_pixel = int(bytes_per_pixel)
        self._rotation = 0
        self._color_order = "RGB"
        self._inverted = False
        self._variant = variant

        # ── Chunked Display 狀態 ──
        self.chunk_size = int(chunk_size)          # 每次傳輸塊位元組數 (0=不分塊)
        self._chunk_total = 0                      # 當前幀總塊數
        self._chunk_done = 0                       # 已傳輸塊數
        self._display_active = False               # begin_display 後為 True
        if self.chunk_size > 0:
            ppc = max(1, self.chunk_size // self.bytes_per_pixel)
            self._chunk_total = (self.width * self.height + ppc - 1) // ppc

        self._bus.reset()
        _sleep_ms(100)

    def set_variant(self, variant):
        """更新初始化深度後重新 init（子類別實作，預設無效）"""
        self._variant = variant
    
    def _bytes_per_pixel(self):
        bpp = getattr(self, "bytes_per_pixel", 2)
        return 3 if int(bpp) >= 3 else 2
    
    def _colmod_value_for_bpp(self, bpp):
        if int(bpp) >= 3:
            return 0x66
        return 0x55
    
    def _get_colmod_cmd(self):
        bpp = self._bytes_per_pixel()
        return bytes([self._colmod_value_for_bpp(bpp)])
    
    def reset(self):
        self._bus.reset()

    def write_cmd(self, cmd):
        self._bus.write_cmd(cmd)

    def write_data(self, data):
        self._bus.write_data(data)

    def write_cmd_data(self, cmd, data):
        self._bus.write_cmd_data(cmd, data)

    def set_window(self, x0, y0, x1=None, y1=None):
        if x1 is None:
            x1 = x0 + self.width - 1
        if y1 is None:
            y1 = y0 + self.height - 1
        if self._rotation in [90, 270]:
            x0, y0, x1, y1 = y0, x0, y1, x1
        self._bus.set_window(x0, y0, x1, y1)

    def show(self, data, x=0, y=0, w=None, h=None):
        """顯示 pixel data — 堵塞模式 (set_window → 寫入 → flush)
        不區分總線類型: 各 adapter 的 set_window 已處理 CASET/PASET/RAMWR 差異
        """
        if w is None: w = self.width
        if h is None: h = self.height
        self.set_window(x, y, x + w - 1, y + h - 1)
        self._bus.write_data_async(data)
        self._bus.flush()
    def show_frame(self, data):
        """Send pixel data (window must be set already)"""
        self._bus.write_frame(data)

    def show_async(self, data, x=0, y=0, w=None, h=None):
        """DMA 異步顯示 — queue 全幀後立即返回, caller 需自行呼叫 flush()"""
        if w is None: w = self.width
        if h is None: h = self.height
        self.set_window(x, y, x + w - 1, y + h - 1)
        self._bus.write_data_async(data)

    # ══════════════════════════════════════════════════════════════
    #  Pipeline Display API — 持久視窗 + DMA chunk 平行
    #  適用連續幀播放：視窗只設一次，每幀只送 RAMWR。
    # ══════════════════════════════════════════════════════════════

    def begin_display(self):
        """設定持久全螢幕視窗（CASET/PASET/RAMWR）一次。
        之後 present() 只送 RAMWR + 資料，省去每幀重設視窗的命令開銷。"""
        self._bus.set_window(0, 0, self.width - 1, self.height - 1)
        self._display_active = True

    def present(self, fb):
        """送一幀到螢幕（視窗已由 begin_display 設好）。
        流程：write_cmd(0x2C RAMWR, polling 確保送達) → write_frame_dma(分 chunk 填 queue)。
        回傳 tid 列表；caller 需在下一幀前 flush() 等完成，實現 DMA 與解碼重疊。"""
        self._bus.write_cmd(0x2C)               # RAMWR（polling，wait_all）
        return self._bus.write_frame_dma(fb)    # 分 chunk DMA，回傳 tids

    def present_wait(self):
        """等所有 pending DMA 完成（每幀結尾或切換前呼叫）"""
        self._bus.flush()

    # ══════════════════════════════════════════════════════════════
    #  Classic Write Session API — begin_write / write_pixels / end_write
    #  (中斷 write_pixels / 非中斷 write_pixels_nonblock)
    # ══════════════════════════════════════════════════════════════

    def begin_write(self, x=0, y=0, w=None, h=None):
        """寫入會話開始 — set_window + 重置塊計數。

        類似 Adafruit beginWrite() / TFT_eSPI startWrite()。
        由 adapter 處理 CASET/PASET/RAMWR 的螢幕差異。
        """
        if w is None: w = self.width
        if h is None: h = self.height
        self.set_window(x, y, x + w - 1, y + h - 1)
        self._chunk_done = 0
        total_pixels = w * h
        if self.chunk_size > 0:
            ppc = max(1, self.chunk_size // self.bytes_per_pixel)
            self._chunk_total = (total_pixels + ppc - 1) // ppc
        else:
            self._chunk_total = 1  # 不分塊 → 單塊

    def write_pixels(self, data):
        """中斷寫入 — 將像素資料送入總線，等待傳輸完成後返回。

        類似 TFT_eSPI pushPixels() / Adafruit writePixels(block=True)。
        在 DMA 和非 DMA 總線上都可靠阻塞。
        """
        self._bus.write_data_async(data)
        self._bus.flush()
        self._chunk_done += 1

    def write_pixels_nonblock(self, data):
        """非中斷 DMA 寫入 — 嘗試將數據塊排入 DMA 隊列。

        返回:
            True  — 排隊成功，可立即準備下一塊數據
            False — DMA 隊列已滿，需要重試同一塊數據

        非 DMA 總線上永遠返回 True（write_data_async 同步完成）。
        """
        handle = self._bus.write_data_async(data)
        if handle is not None:
            self._chunk_done += 1
            return True
        return False

    def end_write(self):
        """寫入會話結束 — flush 剩餘 DMA 數據。

        類似 Adafruit endWrite() / TFT_eSPI endWrite()。
        """
        self._bus.flush()

    @property
    def chunk_total(self):
        """當前幀總塊數（begin_write 後才有效）"""
        return self._chunk_total

    @property
    def chunk_done(self):
        """已成功傳輸的塊數"""
        return self._chunk_done

    @property
    def remaining(self):
        """剩餘未傳輸的塊數"""
        return max(0, self._chunk_total - self._chunk_done)

    @property
    def busy(self):
        """DMA 是否仍在傳輸中（還有塊未完成）"""
        return self._chunk_done < self._chunk_total

    def set_rotation(self, rotation):
        """
        設置屏幕旋轉角度
        :param rotation: 0, 90, 180, 270
        """
        if rotation not in [0, 90, 180, 270]:
            raise ValueError("Rotation must be 0, 90, 180, or 270")
        
        self._rotation = rotation
        self._update_rotation()
        return self
    
    def get_rotation(self):
        """獲取當前旋轉角度"""
        return self._rotation
    
    def set_color_order(self, order):
        """
        設置顏色順序
        :param order: "RGB" 或 "BGR"
        """
        if order.upper() not in ["RGB", "BGR"]:
            raise ValueError("Color order must be 'RGB' or 'BGR'")
        
        self._color_order = order.upper()
        self._update_color_order()
        return self
    
    def get_color_order(self):
        """獲取當前顏色順序"""
        return self._color_order
    
    def invert_display(self, invert=True):
        """
        設置顏色反轉
        :param invert: True 開啟反轉, False 關閉反轉
        """
        self._inverted = bool(invert)
        self._update_inversion()
        return self
    
    def get_inversion_state(self):
        """獲取當前顏色反轉狀態"""
        return self._inverted
    
    def toggle_inversion(self):
        """切換顏色反轉狀態"""
        self._inverted = not self._inverted
        self._update_inversion()
        return self._inverted
    
    def _update_rotation(self):
        """更新旋轉設置 (子類需實現)"""
        pass
    
    def _update_color_order(self):
        """更新顏色順序設置 (子類需實現)"""
        pass
    
    def _update_inversion(self):
        """更新顏色反轉設置 (子類需實現)"""
        pass
    
    def fill(self, color):
        """填充整個屏幕為指定顏色"""
        # 將顏色轉換為RGB565格式
        if isinstance(color, tuple) and len(color) == 3:
            # 從RGB元組轉換
            r, g, b = color
            color = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        
        # 創建顏色緩衝區
        buffer = bytearray(self.width * self.height * 2)
        for i in range(0, len(buffer), 2):
            buffer[i] = color >> 8
            buffer[i+1] = color & 0xFF
        
        # 發送到顯示器
        self.set_window(0, 0)
        self.write_data(buffer)
    
    def display_bin(self, filename, x=0, y=0):
        """顯示二進制圖像文件"""
        self.set_window(x, y)
        with open(filename, 'rb') as f:
            start_time = utime.ticks_ms()
            
            buf = memoryview(bytearray(os.stat(filename)[6]))
            f.readinto(buf)
            self.write_data(buf)
            
            end_time = utime.ticks_ms()
            ticks_time = utime.ticks_diff(end_time, start_time)
            print(f"Display time: {ticks_time}ms")
    
    def display_img_bin(self, filename, x=0, y=0):
        """顯示二進制圖像文件 (無計時)"""
        self.set_window(x, y)
        with open(filename, 'rb') as f:
            buf = memoryview(bytearray(os.stat(filename)[6]))
            f.readinto(buf)
            self.write_data(buf)


class ST7735(TFT):
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=240,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()
    
    def _colmod_value_for_bpp(self, bpp):
        if int(bpp) >= 3:
            return 0x06
        return 0x05
    
    def init(self):
        init_cmds = [
            (0x01, None),       # 軟復位
            (0x11, None),       # 退出睡眠模式
            (0xB1, b'\x01\x2C\x2D'),  # 幀率控制
            (0xB2, b'\x01\x2C\x2D'),
            (0xB3, b'\x01\x2C\x2D\x01\x2C\x2D'),
            (0xB4, b'\x07'),    # 反轉掃描
            (0xC0, b'\xA2\x02\x84'),
            (0xC1, b'\xC5'),
            (0xC2, b'\x0A\x00'),
            (0xC3, b'\x8A\x2A'),
            (0xC4, b'\x8A\xEE'),
            (0x36, self._get_madctl_cmd()),    # 內存訪問控制
            (0x3A, self._get_colmod_cmd()),
            (self._get_inversion_cmd(), None), # 顯示反轉
            (0x29, None)        # 開啟顯示
        ]
        
        for cmd, data in init_cmds:
            self.write_cmd_data(cmd, data)
            _sleep_ms(10)
        
        self.set_window(0, 0)
    
    def _get_madctl_cmd(self):
        """獲取內存訪問控制命令值"""
        # ST7735 MADCTL 位定義:
        # MY MX MV ML RGB MH - -
        rotation_settings = {
            0: 0x00,   # 正常方向
            90: 0x60,  # 旋轉90度
            180: 0xC0, # 旋轉180度
            270: 0xA0  # 旋轉270度
        }
        
        base = rotation_settings.get(self._rotation, 0x00)
        # 設置顏色順序 (RGB/BGR)
        if self._color_order == "BGR":
            base |= 0x08  # 設置BGR模式
        
        return bytes([base])
    
    def _get_inversion_cmd(self):
        """獲取顏色反轉命令"""
        return 0x21 if self._inverted else 0x20
    
    def _update_rotation(self):
        """更新旋轉設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_color_order(self):
        """更新顏色順序設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_inversion(self):
        """更新顏色反轉設置"""
        self.write_cmd(self._get_inversion_cmd())


class ST7789(TFT):
    """泛用 ST7789 驅動 — variant 0/1/2/3 對應不同初始化深度

    所有 ST7789 variant 的硬體 register 都相同，差別只在初始化參數。
    variant 選一個 profile，逐級疊加，由簡入繁測試：

      variant=0  極簡
        SLPOUT → COLMOD → MADCTL → INV → DISPON
        適用：Adafruit 庫、TFT_eSPI 相容面板、IC 內部 POR 已夠用的模組
        特點：不發任何電源/Porch/Gamma 指令，相容性最高

      variant=1  基本電源
        v0 + B2 Porch + 泛用電壓參數
        (B7=0x35, BB=0x19, C0=0x2C, C2=0x01, C3=0x12, C4/C6/D0)
        適用：淘寶常見 1.3/1.54/2.0/2.4 吋 ST7789 模組
        特點：Arduino_GFX 預設值，大部份面板可亮

      variant=2  完整時序
        v1 + B0 Panel Timing + Vernon 電壓參數
        (B7=0x75, BB=0x1A, C0=0x80, C2=0x01FF, C3=0x13)
        適用：ESP32-S3-Touch-LCD-2.8（Vernon 面板）、對時序敏感的面板
        特點：Waveshare 出廠值

      variant=3  完整校色
        v2 + E0/E1 Gamma Table
        適用：需要 gamma 校正才能正確顯示的面板
        特點：最完整初始化

    vcom 參數獨立於 variant，可單獨設置 0xBB 值（預設 0x1A）。

    其餘參數化 method：
      color_order → _get_madctl_cmd()   (RGB/BGR bit)
      invert      → _get_inversion_cmd() (0x20/0x21)
    """
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=320,
                 rotation=0, color_order="RGB", invert=False,
                 vcom=0x1A, variant=0,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self._vcom = vcom
        self._variant = variant
        self.init()

    def init(self):
        """根據 _variant 選擇 init profile"""
        v = self._variant

        _sleep_ms(10)
        self.write_cmd_data(0x11, None)  # Sleep Out
        _sleep_ms(120)
        self.write_cmd_data(0x36, self._get_madctl_cmd())
        self.write_cmd_data(0x3A, self._get_colmod_cmd())

        # ── variant 1+ 電源／Porch 參數 ──
        if v >= 1:
            self.write_cmd_data(0xB2, b'\x0C\x0C\x00\x33\x33')
            self.write_cmd_data(0xB7, b'\x75' if v >= 2 else b'\x35')
            self.write_cmd_data(0xBB, self._get_vcom_cmd())
            self.write_cmd_data(0xC0, b'\x80' if v >= 2 else b'\x2C')
            self.write_cmd_data(0xC2, b'\x01\xFF' if v >= 2 else b'\x01')
            self.write_cmd_data(0xC3, b'\x13' if v >= 2 else b'\x12')
            self.write_cmd_data(0xC4, b'\x20')
            self.write_cmd_data(0xC6, b'\x0F')
            self.write_cmd_data(0xD0, b'\xA4\xA1')

        # ── variant 2+ Panel Timing ──
        if v >= 2:
            self.write_cmd_data(0xB0, b'\x00\xE8')

        # ── variant 3+ Gamma ──
        if v >= 3:
            self.write_cmd_data(0xE0, b'\xD0\x0D\x14\x0D\x0D\x09\x38\x44\x4E\x3A\x17\x18\x2F\x30')
            self.write_cmd_data(0xE1, b'\xD0\x09\x0F\x08\x07\x14\x37\x44\x4D\x38\x15\x16\x2C\x2E')

        self.write_cmd(self._get_inversion_cmd())
        self.write_cmd_data(0x29, None)  # Display On
        _sleep_ms(50)
        self.set_window(0, 0)

    def _get_madctl_cmd(self):
        """內存訪問控制 (0x36) — 旋轉 + 顏色順序"""
        base = {0: 0x00, 90: 0x60, 180: 0xC0, 270: 0xA0}.get(self._rotation, 0x00)
        if self._color_order == "BGR":
            base |= 0x08
        return bytes([base])

    def _get_vcom_cmd(self):
        """VCOM 電壓 (0xBB) — 面板廠 tune"""
        return bytes([self._vcom])

    def _get_inversion_cmd(self):
        """顏色反轉 (0x20/0x21)"""
        return 0x21 if self._inverted else 0x20

    def _update_rotation(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_color_order(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_inversion(self):
        self.write_cmd(self._get_inversion_cmd())


class ST7796(TFT):
    """ST7796 320x480 驅動 (ESP32-S3 N16R8)"""
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=480, height=320,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()

    def init(self):
        _sleep_ms(10)
        self.write_cmd_data(0x01, None)  # SWRESET
        _sleep_ms(120)
        self.write_cmd_data(0x11, None)  # SLPOUT
        _sleep_ms(120)

        self.write_cmd_data(0xF0, b'\xC3')  # CSCON
        self.write_cmd_data(0xF0, b'\x96')  # CSCON
        self.write_cmd_data(0x36, self._get_madctl_cmd())
        self.write_cmd_data(0x3A, self._get_colmod_cmd())

        self.write_cmd_data(0xB7, b'\xC6')   # EM
        self.write_cmd_data(0xB4, b'\x01')   # DIC
        self.write_cmd_data(0xB6, b'\x80\x02\x3B')  # DFC
        self.write_cmd_data(0xE8, b'\x40\x8A\x00\x00\x29\x19\xA5\x33')  # DOCA
        self.write_cmd_data(0xC1, b'\x06')   # PWR2
        self.write_cmd_data(0xC2, b'\xA7')   # PWR3
        self.write_cmd_data(0xC5, b'\x18')   # VCMPCTL
        _sleep_ms(120)

        # Positive Gamma
        self.write_cmd_data(0xE0, b'\xF0\x09\x0B\x06\x04\x15\x2F\x54\x42\x3C\x17\x14\x18\x1B')
        # Negative Gamma
        self.write_cmd_data(0xE1, b'\xF0\x09\x0B\x06\x04\x03\x2D\x43\x42\x3B\x16\x14\x17\x1B')
        _sleep_ms(120)

        self.write_cmd_data(0xF0, b'\x3C')  # CSCON
        self.write_cmd_data(0xF0, b'\x69')  # CSCON
        _sleep_ms(120)

        self.write_cmd_data(0x29, None)  # DISPON
        _sleep_ms(120)
        self.set_window(0, 0)

    def _get_madctl_cmd(self):
        rotation_settings = {
            0: 0x00, 90: 0x60, 180: 0xC0, 270: 0xA0
        }
        base = rotation_settings.get(self._rotation, 0x00)
        if self._color_order == "BGR":
            base |= 0x08
        return bytes([base])

    def _get_inversion_cmd(self):
        return 0x21 if self._inverted else 0x20

    def _update_rotation(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_color_order(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_inversion(self):
        self.write_cmd(self._get_inversion_cmd())

class GC9A01(TFT):
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=240,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()
    
    def init(self):
        init_cmds = [
            (0xEF, None),       # 系統功能啟用
            (0xEB, b'\x14'),     # 調整內部電壓
            (0xFE, None),        # 切換命令頁
            (0xEF, None),        # 重複啟用系統
            (0xEB, b'\x14'),     # 電壓參數
            (0x84, b'\x40'),     # VCI電壓設定
            (0x85, b'\xFF'),     # VCOM電壓
            (0x86, b'\xFF'),     # VCOM偏移
            (0x87, b'\xFF'),     # 電源控制
            (0x88, b'\x0A'),     # 面板驅動電壓
            (0x89, b'\x21'),     # 時序控制
            (0x8A, b'\x00'),     # 預充電時間
            (0x8B, b'\x80'),     # 接口控制
            (0x8C, b'\x01'),     # 驅動能力
            (0x8D, b'\x01'),     # 預充電電流
            (0x8E, b'\xFF'),     # COM腳掃描
            (0x8F, b'\xFF'),     # COM腳配置
            (0xB6, b'\x00\x00'), # 顯示功能控制
            (0x3A, self._get_colmod_cmd()),
            (0x90, b'\x08\x08\x08\x08'),  # 框架速率控制
            (0xBD, b'\x06'),     # 命令保護
            (0xBC, b'\x00'),     # 接口模式
            (0xFF, b'\x60\x01\x04'), # Gamma校正
            (0xC3, b'\x13'),     # 電源控制1
            (0xC4, b'\x13'),     # 電源控制2
            (0xC9, b'\x22'),     # 電源控制3
            (0xBE, b'\x11'),     # 電壓補償
            (0xE1, b'\x10\x0E'), # 正極Gamma校正
            (0xDF, b'\x21\x0c\x02'), # 時序控制
            (0xF0, b'\x45\x09\x08\x08\x26\x2A'), # Gamma曲線設定
            (0xF1, b'\x43\x70\x72\x36\x37\x6F'), # Gamma參數
            (0xF2, b'\x45\x09\x08\x08\x26\x2A'), # Gamma曲線設定
            (0xF3, b'\x43\x70\x72\x36\x37\x6F'), # Gamma參數
            (0xED, b'\x1B\x0B'), # 電壓保護
            (0xAE, b'\x77'),     # 電源優化
            (0xCD, b'\x63'),     # 背光控制
            (0x70, b'\x07\x07\x04\x0E\x0F\x09\x07\x08\x03'), # 面板設定
            (0xE8, b'\x34'),     # 時序控制
            (0x62, b'\x18\x0D\x71\xED\x70\x70\x18\x0F\x71\xEF\x70\x70'), # Gamma校正
            (0x63, b'\x18\x11\x71\xF1\x70\x70\x18\x13\x71\xF3\x70\x70'), # Gamma校正
            (0x64, b'\x28\x29\xF1\x01\xF1\x00\x07'), 
            (0x66, b'\x3C\x00\xCD\x67\x45\x45\x10\x00\x00\x00'),
            (0x67, b'\x00\x3C\x00\x00\x00\x01\x54\x10\x32\x98'),
            (0x36, self._get_madctl_cmd()),  # 記憶體存取控制
            (0x74, b'\x10\x85\x80\x00\x00\x4E\x00'),
            (0x98, b'\x3e\x07'),
            (0x35, None),
            (self._get_inversion_cmd(), None),  # 顏色反轉
            (0x29, None),        # 開啟顯示
            (0x11, None),        # 退出睡眠模式 (必須在最後)
        ]
        
        for cmd, data in init_cmds:
            self.write_cmd_data(cmd, data)
            _sleep_ms(10)

        self.set_window(0, 0)
    
    def _get_madctl_cmd(self):
        """獲取內存訪問控制命令值"""
        # GC9A01 MADCTL 位定義可能有所不同
        rotation_settings = {
            0: 0x08,   # 正常方向
            90: 0x68,  # 旋轉90度
            180: 0xC8, # 旋轉180度
            270: 0xA8  # 旋轉270度
        }
        
        base = rotation_settings.get(self._rotation, 0x08)
        # 設置顏色順序 (RGB/BGR)
        if self._color_order == "BGR":
            base |= 0x08  # 設置BGR模式
        
        return bytes([base])
    
    def _get_inversion_cmd(self):
        """獲取顏色反轉命令"""
        return 0x21 if self._inverted else 0x20
    
    def _update_rotation(self):
        """更新旋轉設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_color_order(self):
        """更新顏色順序設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_inversion(self):
        """更新顏色反轉設置"""
        self.write_cmd(self._get_inversion_cmd())


class ILI9341(TFT):
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=320,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()
    
    def init(self):
        # 硬體復位序列 (TFT.__init__ 已透過 _bus.reset() 做過一次，此為額外二次確認)
        if self.rst is not None:
            self.rst.value(1)
            _sleep_ms(5)
            self.rst.value(0)
            _sleep_ms(20)
            self.rst.value(1)
            _sleep_ms(150)
        
        # ILI9341 初始化命令序列
        init_cmds = [
            (0xCF, b'\x00\xC1\x30'),   # 電源控制B
            (0xED, b'\x64\x03\x12\x81'),# 電源時序控制
            (0xE8, b'\x85\x00\x78'),    # 驅動時序控制A
            (0xCB, b'\x39\x2C\x00\x34\x02'), # 電源控制A
            (0xF7, b'\x20'),             # 泵比控制
            (0xEA, b'\x00\x00'),         # 驅動時序控制B
            (0xC0, b'\x23'),             # 電源控制1
            (0xC1, b'\x10'),             # 電源控制2
            (0xC5, b'\x3E\x28'),         # VCOM控制1
            (0xC7, b'\x86'),             # VCOM控制2
            (0x36, self._get_madctl_cmd()),  # 記憶體存取控制
            (0x3A, self._get_colmod_cmd()),
            (0xB1, b'\x00\x18'),         # 幀率控制
            (0xB6, b'\x08\x82\x27'),     # 顯示功能控制
            (0xF2, b'\x00'),             # 3G控制 (禁用)
            (0x26, b'\x01'),             # Gamma曲線設置
            (0xE0, b'\x0F\x31\x2B\x0C\x0E\x08\x4E\xF1\x37\x07\x10\x03\x0E\x09\x00'), # 正極Gamma校正
            (0xE1, b'\x00\x0E\x14\x03\x11\x07\x31\xC1\x48\x08\x0F\x0C\x31\x36\x0F'), # 負極Gamma校正
            (0x11, None),               # 退出睡眠模式
            (self._get_inversion_cmd(), None),  # 顏色反轉
            (0x29, None)                # 開啟顯示
        ]
        
        # 發送初始化命令
        for cmd, data in init_cmds:
            self.write_cmd_data(cmd, data)
            _sleep_ms(10)
        
        # 額外延時確保初始化完成
        _sleep_ms(120)
        self.set_window(0, 0, self.width - 1, self.height - 1)
    
    def _get_madctl_cmd(self):
        """獲取內存訪問控制命令值"""
        # ILI9341 MADCTL 位定義:
        # MY MX MV ML RGB MH - -
        rotation_settings = {
            0: 0x48,   # 正常方向
            90: 0x28,  # 旋轉90度
            180: 0x88, # 旋轉180度
            270: 0xE8  # 旋轉270度
        }
        
        base = rotation_settings.get(self._rotation, 0x48)
        # 設置顏色順序 (RGB/BGR)
        if self._color_order == "BGR":
            base |= 0x08  # 設置BGR模式
        
        return bytes([base])
    
    def _get_inversion_cmd(self):
        """獲取顏色反轉命令"""
        return 0x21 if self._inverted else 0x20
    
    def _update_rotation(self):
        """更新旋轉設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_color_order(self):
        """更新顏色順序設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_inversion(self):
        """更新顏色反轉設置"""
        self.write_cmd(self._get_inversion_cmd())
        
        
        
class GC9D01(TFT):
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=240,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()
    
    def _colmod_value_for_bpp(self, bpp):
        if int(bpp) >= 3:
            return 0x06
        return 0x05
    
    def init(self):
        # 根據您提供的初始化序列重新編寫
        init_cmds = [
            (0xFE, None),       # 切換命令頁
            (0xEF, None),       # 系統功能啟用
            
            # 一系列配置寄存器設置
            (0x80, b'\xFF'), (0x81, b'\xFF'), (0x82, b'\xFF'), (0x83, b'\xFF'),
            (0x84, b'\xFF'), (0x85, b'\xFF'), (0x86, b'\xFF'), (0x87, b'\xFF'),
            (0x88, b'\xFF'), (0x89, b'\xFF'), (0x8A, b'\xFF'), (0x8B, b'\xFF'),
            (0x8C, b'\xFF'), (0x8D, b'\xFF'), (0x8E, b'\xFF'), (0x8F, b'\xFF'),
            
            (0x3A, self._get_colmod_cmd()),
            (0xEC, b'\x01'),    # 未知功能設置
            
            # 複雜的寄存器配置
            (0x74, b'\x02\x0E\x00\x00\x00\x00\x00'),  # 時序控制
            (0x98, b'\x3E'), (0x99, b'\x3E'),         # 門控控制
            (0xB5, b'\x0D\x0D'),                      # 空白設置
            
            # 電源相關設置
            (0x60, b'\x38\x0F\x79\x67'),              # 電源控制1
            (0x61, b'\x38\x11\x79\x67'),              # 電源控制2  
            (0x64, b'\x38\x17\x71\x5F\x79\x67'),      # 電源控制3
            (0x65, b'\x38\x13\x71\x5B\x79\x67'),      # 電源控制4
            
            (0x6A, b'\x00\x00'),                      # 幀率控制
            (0x6C, b'\x22\x02\x22\x02\x22\x22\x50'),  # 接口控制
            
            # Gamma 校正設置 (很長的序列)
            (0x6E, b'\x03\x03\x01\x01\x00\x00\x0F\x0F\x0D\x0D\x0B\x0B\x09\x09'
                   b'\x00\x00\x00\x00\x0A\x0A\x0C\x0C\x0E\x0E\x10\x10\x00\x00'
                   b'\x02\x02\x04\x04'),
            
            (0xBF, b'\x01'),    # 功能控制
            (0xF9, b'\x40'),    # 功能設置
            
            # 更多配置
            (0x9B, b'\x3B'),    # VCOM 控制
            (0x93, b'\x33\x7F\x00'),  # 電源優化
            (0x7E, b'\x30'),    # 部分模式控制
            
            # 額外的時序設置
            (0x70, b'\x0D\x02\x08\x0D\x02\x08'),
            (0x71, b'\x0D\x02\x08'),
            (0x91, b'\x0E\x09'),
            
            # 電源控制
            (0xC3, b'\x19'), (0xC4, b'\x19'), (0xC9, b'\x3C'),
            
            # Gamma 曲線設定
            (0xF0, b'\x53\x15\x0A\x04\x00\x3E'),
            (0xF2, b'\x53\x15\x0A\x04\x00\x3A'),
            (0xF1, b'\x56\xA8\x7F\x33\x34\x5F'),
            (0xF3, b'\x52\xA4\x7F\x33\x34\xDF'),
            
            # 內存訪問控制 (將在後面根據旋轉重新設置)
            (0x36, self._get_madctl_cmd()),
            
            # 退出睡眠模式
            (0x11, None),
        ]
        
        # 執行初始化命令
        for cmd, data in init_cmds:
            self.write_cmd_data(cmd, data)
            _sleep_ms(5)
        
        # 等待200ms (根據您提供的Delay(200))
        _sleep_ms(200)
        
        # 開啟顯示
        self.write_cmd(0x29)
        _sleep_ms(50)
        
        # 設置窗口
        self.set_window(0, 0)
    
    def _get_madctl_cmd(self):
        """獲取內存訪問控制命令值"""
        # GC9D01 MADCTL 位定義:
        # MY: 行地址順序 (0: 從上到下, 1: 從下到上)
        # MX: 列地址順序 (0: 從左到右, 1: 從右到左)  
        # MV: 行/列交換 (0: 正常, 1: 交換)
        # ML: 垂直刷新順序
        # RGB: RGB/BGR順序 (0: RGB, 1: BGR)
        # MH: 水平刷新順序
        
        rotation_settings = {
            0: 0x00,   # 正常方向
            90: 0x60,  # 旋轉90度 (MV=1, MX=1)
            180: 0xC0, # 旋轉180度 (MY=1, MX=1)  
            270: 0xA0  # 旋轉270度 (MY=1, MV=1)
        }
        
        base = rotation_settings.get(self._rotation, 0x00)
        
        # 設置顏色順序 (RGB/BGR)
        if self._color_order == "BGR":
            base |= 0x08  # 設置BGR模式
        
        return bytes([base])
    
    def _get_inversion_cmd(self):
        """獲取顏色反轉命令"""
        return 0x21 if self._inverted else 0x20
    
    def _update_rotation(self):
        """更新旋轉設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_color_order(self):
        """更新顏色順序設置"""
        self.write_cmd_data(0x36, self._get_madctl_cmd())
    
    def _update_inversion(self):
        """更新顏色反轉設置"""
        self.write_cmd(self._get_inversion_cmd())
    
    def set_window(self, x0, y0, x1=None, y1=None):
        """設置顯示窗口"""
        if x1 is None:
            x1 = self.width - 1
        if y1 is None:
            y1 = self.height - 1
        
        # 確保坐標在顯示範圍內
        x0 = max(0, min(x0, self.width - 1))
        y0 = max(0, min(y0, self.height - 1))
        x1 = max(0, min(x1, self.width - 1))
        y1 = max(0, min(y1, self.height - 1))
        
        # 根據旋轉調整坐標映射
        if self._rotation == 0:
            col_start, col_end = x0, x1
            row_start, row_end = y0, y1
        elif self._rotation == 90:
            col_start, col_end = y0, y1
            row_start, row_end = x0, x1
        elif self._rotation == 180:
            col_start, col_end = self.width - 1 - x1, self.width - 1 - x0
            row_start, row_end = self.height - 1 - y1, self.height - 1 - y0
        elif self._rotation == 270:
            col_start, col_end = self.height - 1 - y1, self.height - 1 - y0
            row_start, row_end = self.width - 1 - x1, self.width - 1 - x0
        
        # 發送列地址設置
        self.write_cmd(0x2A)
        self.write_data(bytes([col_start >> 8, col_start & 0xFF, 
                             col_end >> 8, col_end & 0xFF]))
        
        # 發送行地址設置
        self.write_cmd(0x2B)
        self.write_data(bytes([row_start >> 8, row_start & 0xFF,
                             row_end >> 8, row_end & 0xFF]))
        
        # 開始內存寫入
        self.write_cmd(0x2C)


class NV3030B(TFT):
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=240,
                 rotation=0, color_order="BGR", invert=True,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()

    def set_window(self, x0, y0, x1=None, y1=None):
        if x1 is None:
            x1 = x0 + self.width - 1
        if y1 is None:
            y1 = y0 + self.height - 1

        if self._rotation in [90, 270]:
            x0, y0, x1, y1 = y0, x0, y1, x1

        y0_data = y0 + 20
        y1_data = y1 + 20

        self._win_x0 = int(x0)
        self._win_y0 = int(y0_data)
        self._win_x1 = int(x1)
        self._win_y1 = int(y1_data)

        # 統一走 adapter（可以是 SpiBusAdapter / lcd_bus / QSPI）
        self.write_cmd(0x2A)
        self.write_data(bytes([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        self.write_cmd(0x2B)
        self.write_data(bytes([y0_data >> 8, y0_data & 0xFF, y1_data >> 8, y1_data & 0xFF]))
        self.write_cmd(0x2C)

    def init(self):
        if self.rst is not None:
            self.rst.value(1)
            time.sleep_ms(10)
            self.rst.value(0)
            time.sleep_ms(10)
            self.rst.value(1)
            time.sleep_ms(50)

        self.write_cmd_data(0x36, b'\x08')

        self.write_cmd_data(0xFD, b'\x06\x08')
        self.write_cmd_data(0x61, b'\x07\x04')
        self.write_cmd_data(0x62, b'\x00\x44\x45')
        self.write_cmd_data(0x63, b'\x41\x07\x12\x12')
        self.write_cmd_data(0x64, b'\x37')
        self.write_cmd_data(0x65, b'\x09\x10\x21')
        self.write_cmd_data(0x66, b'\x09\x10\x21')
        self.write_cmd_data(0x67, b'\x20\x40')
        self.write_cmd_data(0x68, b'\x90\x4C\x7C\x66')
        self.write_cmd_data(0xB1, b'\x0F\x02\x01')
        self.write_cmd_data(0xB4, b'\x01')
        self.write_cmd_data(0xB5, b'\x02\x02\x0A\x14')
        self.write_cmd_data(0xB6, b'\x04\x01\x9F\x00\x02')
        self.write_cmd_data(0xDF, b'\x11')
        self.write_cmd_data(0xE2, b'\x13\x00\x00\x30\x33\x3F')
        self.write_cmd_data(0xE5, b'\x3F\x33\x30\x00\x00\x13')
        self.write_cmd_data(0xE1, b'\x00\x57')
        self.write_cmd_data(0xE4, b'\x58\x00')
        self.write_cmd_data(0xE0, b'\x01\x03\x0E\x0E\x0C\x15\x19')
        self.write_cmd_data(0xE3, b'\x1A\x16\x0C\x0F\x0E\x0D\x02\x01')
        self.write_cmd_data(0xE6, b'\x00\xFF')
        self.write_cmd_data(0xE7, b'\x01\x04\x03\x03\x00\x12')
        self.write_cmd_data(0xE8, b'\x00\x70\x00')
        self.write_cmd_data(0xEC, b'\x52')
        self.write_cmd_data(0xF1, b'\x01\x01\x02')
        self.write_cmd_data(0xF6, b'\x09\x10\x00\x00')
        self.write_cmd_data(0xFD, b'\xFA\xFC')

        self.write_cmd_data(0x3A, b'\x05')
        self.write_cmd_data(0x35, b'\x00')
        self.write_cmd_data(0x36, self._get_madctl_cmd())
        self.write_cmd(self._get_inversion_cmd())
        self.write_cmd(0x11)
        time.sleep_ms(200)
        self.write_cmd(0x29)
        time.sleep_ms(10)

        self.set_window(0, 0)

    def _get_madctl_cmd(self):
        rotation_settings = {
            0: 0x00,
            90: 0x60,
            180: 0xC0,
            270: 0xA0
        }

        base = rotation_settings.get(self._rotation, 0x00)
        if self._color_order == "BGR":
            base |= 0x08

        return bytes([base])

    def _get_inversion_cmd(self):
        return 0x21 if self._inverted else 0x20

    def _update_rotation(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_color_order(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_inversion(self):
        self.write_cmd(self._get_inversion_cmd())


class RM67162(TFT):
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=240, height=536,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()

    def _get_madctl_cmd(self):
        rotation_settings = {0:0x00, 90:0x60, 180:0xC0, 270:0xA0}
        base = rotation_settings.get(self._rotation, 0x00)
        if self._color_order == "BGR":
            base |= 0x08
        return bytes([base])

    def _update_rotation(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_color_order(self):
        self.write_cmd_data(0x36, self._get_madctl_cmd())

    def _update_inversion(self):
        self.write_cmd(0x21 if self._inverted else 0x20)

    def init(self):
        """RM67162 QSPI init — 參照 Waveshare C rm67162_qspi_init[]"""
        if self.rst is not None:
            self.rst.value(1)
            _sleep_ms(10)
            self.rst.value(0)
            _sleep_ms(300)
            self.rst.value(1)
            _sleep_ms(200)
        # 重試 3 次 (C: "Initialize multiple times to prevent init failure")
        for _ in range(3):
            self.write_cmd_data(0x11, None)      # Sleep Out
            _sleep_ms(120)
            self.write_cmd_data(0x36, self._get_madctl_cmd())
            self.write_cmd_data(0x3A, b'\x55')   # COLMOD 16-bit
            self.write_cmd_data(0x51, b'\x00')   # Brightness 0
            self.write_cmd_data(0x29, None)      # Display On
            _sleep_ms(20)
            self.write_cmd_data(0x51, b'\xD0')   # Brightness MAX


class SH8601(TFT):
    """Waveshare AMOLED 1.91" 536x240 QSPI (SH8601)"""
    def __init__(self, spi=None, dc=None, cs=None, rst=None, width=536, height=240,
                 rotation=0, color_order="RGB", invert=False,
                 pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None):
        super().__init__(spi=spi, dc=dc, cs=cs, rst=rst,
                         width=width, height=height,
                         pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel,
                         adapter=adapter)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()

    def init(self):
        if self.rst is not None:
            self.rst.value(1)
            _sleep_ms(10)
            self.rst.value(0)
            _sleep_ms(150)
            self.rst.value(1)
            _sleep_ms(200)
        self.write_cmd_data(0x11, None)
        _sleep_ms(120)
        self.write_cmd_data(0x36, b'\xF0')
        self.write_cmd_data(0x3A, b'\x55')
        self.write_cmd_data(0x2A, b'\x00\x00\x02\x17')
        self.write_cmd_data(0x2B, b'\x00\x00\x00\xEF')
        self.write_cmd_data(0x51, b'\xFF')
        self.write_cmd_data(0x29, None)