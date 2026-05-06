import machine
import micropython
import array
import time

class PCA9685:
    def __init__(self, i2c, address=0x40, n=16, invert=False):
        self.i2c = i2c
        self.address = address
        self.n = n
        self.invert = invert  # 支援邏輯反轉 (共陽極)
        
        # 核心數據區 (H: uint16)
        self._buf = array.array('H', [0] * n)
        # I2C 傳輸緩衝區
        self.reg_buf = bytearray(n * 4)
        
        self.setup()
        self.show()

    def setup(self):
        try:
            self.i2c.writeto_mem(self.address, 0x00, b'\x00')
            self.freq(200)
        except Exception as e:
            print(f"PCA9685 Init Error: {e}")

    def freq(self, freq):
        try:
            prescale = int(25000000.0 / 4096.0 / freq + 0.5)
            self.i2c.writeto_mem(self.address, 0x00, b'\x10') 
            self.i2c.writeto_mem(self.address, 0xFE, bytearray([prescale]))
            self.i2c.writeto_mem(self.address, 0x00, b'\xa1')
            time.sleep_us(500)
        except Exception as e:
            print(f"PCA9685 Freq Error: {e}")

    # ========================================
    # 內部 LED 類 - 保留並優化你的語法糖
    # ========================================
    class LED:
        def __init__(self, controller, index):
            self.controller = controller
            self.index = index
        
        @property
        def duty(self):
            return self.controller._buf[self.index]
        
        @duty.setter
        def duty(self, value):
            self.controller._buf[self.index] = value

        def __setitem__(self, sub_index, value):
            """ 支援 led[i][0] = val 語法 """
            # 在 PWM 模式下，通常只有亮度(V)一個維度，
            # 但這裡保留接口，以便你未來擴充 HSV 邏輯
            if sub_index == 2 or sub_index == -1: # 假設 2 是 V (Value/Brightness)
                self.controller._buf[self.index] = value
            else:
                # 其他層次可以根據需求定義，目前直接設為 duty
                self.controller._buf[self.index] = value

    def __getitem__(self, index):
        """ 返回一個 LED 物件，支援 led[i][2] = 4095 """
        if 0 <= index < self.n:
            return self.LED(self, index)
        raise IndexError("Out of range")
    
    
    @property
    def buf(self):
        """獲取緩衝區引用"""
        return self._buf

    @buf.setter
    def buf(self, value):
        """
        防呆 Setter: 支援列表、bytearray 或 array.array 直賦
        """
        if isinstance(value, array.array) and value.typecode == 'H':
            self._buf = value
        elif isinstance(value, (bytearray, bytes)):
            # 零拷貝技術：將 8-bit 字節流視為 16-bit 數據
            self._buf = memoryview(value).cast('H')
        else:
            # 強制轉換列表或其他序列
            self._buf = array.array('H', value)
    
    
    def fill(self, val):
        """快速填充所有通道 (0-4095)"""
        for i in range(self.n):
            self._buf[i] = val

    # ========================================
    # 核心計算與傳輸
    # ========================================
    @micropython.viper
    def _prepare_reg_buf(self):
        p_buf = ptr16(self._buf)
        p_reg = ptr8(self.reg_buf)
        n = int(self.n)
        is_inv = int(self.invert)
        
        for i in range(n):
            val = p_buf[i]
            
            # --- 邏輯反轉處理 ---
            if is_inv:
                val = 4095 - val
            
            # 邊界限制
            if val < 0: val = 0
            if val > 4095: val = 4095
                
            idx = i << 2 
            
            # PCA9685 寄存器填充
            if val <= 0:
                p_reg[idx] = 0;   p_reg[idx+1] = 0
                p_reg[idx+2] = 0; p_reg[idx+3] = 0x10 # Full OFF
            elif val >= 4095:
                p_reg[idx] = 0;   p_reg[idx+1] = 0x10 # Full ON
                p_reg[idx+2] = 0; p_reg[idx+3] = 0
            else:
                p_reg[idx] = 0;   p_reg[idx+1] = 0
                p_reg[idx+2] = val & 0xFF
                p_reg[idx+3] = val >> 8

    def show(self):
        try:
            self._prepare_reg_buf()
            self.i2c.writeto_mem(self.address, 0x06, self.reg_buf)
        except Exception as e:
            print(f"PCA9685 Show Error: {e}")

# ==================== 用法範例 ====================
# i2c = machine.I2C(0, scl=machine.Pin(9), sda=machine.Pin(8))
# pwm_board = PCA9685极速版(i2c)

# 1. 單個設置
# pwm_board[0] = 2048
# pwm_board.show()

# 2. 批量序列設置
# pwm_board.buf = [4095, 2048, 1024, 0] * 4
# pwm_board.show()

# 3. 極速流傳輸 (例如從 DMA 或文件讀取)
# raw_bytes = bytearray(32) # 16通道 * 2字節
# f.readinto(raw_bytes)
# pwm_board.buf = raw_bytes # 自動轉換
# pwm_board.show()

# ==================== 使用示範 ====================
# pca = PCA9685極速版(i2c, invert=True) # 如果是共陽極，設為 True

# 1. 你的經典用法：
# pca[0][2] = 4095  # 設置第 0 個 LED 的亮度
# pca.show()

# 2. 批量用法：
# for i in range(16):
#     pca[i].duty = 2048
# pca.show()
