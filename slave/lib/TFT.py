import os
import gc
import machine ,time

def _sleep_ms(_ms):
    return

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
    def __init__(self, spi, dc, cs, rst, width, height, pixel_format="RGB565_BE", bytes_per_pixel=2):
        self.spi = spi
        self.dc = dc
        self.cs = cs
        self.rst = rst
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self.bytes_per_pixel = int(bytes_per_pixel)
        self._rotation = 0
        self._color_order = "RGB"  # 預設顏色順序
        self._inverted = False     # 顏色反轉狀態
        self.write_chunk = 0
        
        # 初始化引腳
        self.dc.init(machine.Pin.OUT, value=0)
        self.cs.init(machine.Pin.OUT, value=1)
        self.rst.init(machine.Pin.OUT, value=1)
        
        self.reset()
        _sleep_ms(100)
    
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
        """硬體重置顯示器"""
        self.rst(0)
        _sleep_ms(50)
        self.rst(1)
        _sleep_ms(50)
    
    def write_cmd(self, cmd):
        """寫入命令到顯示器"""
        self.dc(0)
        self.cs(0)
        self.spi.write(bytearray([cmd]))
        self.cs(1)
    
    def write_data(self, data):
        """寫入數據到顯示器"""
        self.dc(1)
        self.cs(0)
        self.spi.write(data)
        self.cs(1)
    
    def write_cmd_data(self, cmd, data):
        """同時寫入命令和數據"""
        self.write_cmd(cmd)
        if data:
            self.write_data(data)
    
    def set_window(self, x0, y0, x1=None, y1=None):
        """設置顯示區域窗口"""
        if x1 is None:
            x1 = x0 + self.width - 1
        if y1 is None:
            y1 = y0 + self.height - 1
        
        # 根據旋轉調整座標
        if self._rotation in [90, 270]:
            x0, y0, x1, y1 = y0, x0, y1, x1
        
        self.write_cmd(0x2A)  # 列地址設置
        self.write_data(bytes([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        
        self.write_cmd(0x2B)  # 行地址設置
        self.write_data(bytes([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))
        
        self.write_cmd(0x2C)  # 內存寫入
    
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
    def __init__(self, spi, dc, cs, rst, width, height, rotation=0, color_order="RGB", invert=False, pixel_format="RGB565_BE", bytes_per_pixel=2):
        super().__init__(spi, dc, cs, rst, width, height, pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel)
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
    def __init__(self, spi, dc, cs, rst, width, height, rotation=0, color_order="RGB", invert=False, pixel_format="RGB565_BE", bytes_per_pixel=2):
        super().__init__(spi, dc, cs, rst, width, height, pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()
    
    def init(self):
        init_cmds = [
            (0x01, None),       # 軟復位
            (0x11, None),       # 退出睡眠模式
            (0x3A, self._get_colmod_cmd()),
            (0x36, self._get_madctl_cmd()),    # 內存訪問控制
            (0xB2, b'\x0C\x0C\x00\x33\x33'),
            (0xB7, b'\x35'),    # 門控制
            (0xBB, b'\x19'),    # VCOM設置
            (0xC0, b'\x2C'),    # LCM控制
            (0xC2, b'\x01'),
            (0xC3, b'\x12'),
            (0xC4, b'\x20'),
            (0xC6, b'\x0F'),
            (self._get_inversion_cmd(), None), # 顏色反轉
            (0xD0, b'\xA4\xA1'),
            (0x29, None)        # 開啟顯示
        ]
        
        for cmd, data in init_cmds:
            self.write_cmd_data(cmd, data)
            _sleep_ms(10)
        
        self.set_window(0, 0)
    
    def _get_madctl_cmd(self):
        """獲取內存訪問控制命令值"""
        # ST7789 MADCTL 位定義:
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


class ST7789T3(ST7789):
    """ST7789T3 變體驅動，可能有一些特定的初始化參數"""
    def __init__(self, spi, dc, cs, rst, width=240, height=240, rotation=0, color_order="RGB", invert=False, pixel_format="RGB565_BE", bytes_per_pixel=2):
        super().__init__(spi, dc, cs, rst, width, height, rotation, color_order, invert, pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel)
    
    def init(self):
        # ST7789T3 可能有不同的初始化序列
        init_cmds = [
            (0x01, None),       # 軟復位
            (0x11, None),       # 退出睡眠模式
            (0x3A, self._get_colmod_cmd()),
            (0x36, self._get_madctl_cmd()),    # 內存訪問控制
            (0xB2, b'\x0C\x0C\x00\x33\x33'),   # 門控制
            (0xB7, b'\x35'),    # 門控制
            (0xBB, b'\x1F'),    # VCOM設置 (T3可能不同)
            (0xC0, b'\x2C'),    # LCM控制
            (0xC2, b'\x01'),
            (0xC3, b'\x12'),
            (0xC4, b'\x20'),
            (0xC6, b'\x0F'),
            (self._get_inversion_cmd(), None), # 顏色反轉
            (0xD0, b'\xA4\xA1'),
            (0x29, None)        # 開啟顯示
        ]
        
        for cmd, data in init_cmds:
            self.write_cmd_data(cmd, data)
            _sleep_ms(10)
        
        self.set_window(0, 0)


class GC9A01(TFT):
    def __init__(self, spi, dc, cs, rst, width, height, rotation=0, color_order="RGB", invert=False, pixel_format="RGB565_BE", bytes_per_pixel=2):
        super().__init__(spi, dc, cs, rst, width, height, pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel)
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
    def __init__(self, spi, dc, cs, rst, width=240, height=320, rotation=0, color_order="RGB", invert=False, pixel_format="RGB565_BE", bytes_per_pixel=2):
        super().__init__(spi, dc, cs, rst, width, height, pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel)
        self._rotation = rotation
        self._color_order = color_order.upper()
        self._inverted = invert
        self.init()
    
    def init(self):
        # 硬體復位序列
        self.rst(1)
        _sleep_ms(5)
        self.rst(0)
        _sleep_ms(20)
        self.rst(1)
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
    def __init__(self, spi, dc, cs, rst, width=240, height=240, rotation=0, color_order="RGB", invert=False, pixel_format="RGB565_BE", bytes_per_pixel=2):
        super().__init__(spi, dc, cs, rst, width, height, pixel_format=pixel_format, bytes_per_pixel=bytes_per_pixel)
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
