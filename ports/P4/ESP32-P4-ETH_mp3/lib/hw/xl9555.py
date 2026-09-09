"""
XL9555 — I2C 16-bit GPIO Expander (PCA9555 compatible)
API 完全相容 machine.Pin 風格，中斷除外。

用法:
  xl = XL9555(i2c, addr=0x20)

  # Pin API (完全相容 machine.Pin):
  xl[0].init(Pin.OUT)           # 設為 output
  xl[0].value(0)                # 設 LOW，立即 I2C
  xl[0].value(1)                # 設 HIGH，立即 I2C
  print(xl[0].value())          # 讀取

  # 批次操作 (改緩衝，一次 show):
  xl[0] = 1                     # buffer = HIGH
  xl[1] = 0                     # buffer = LOW
  xl.show()                     # 同步寫入晶片
"""

_reg_input0  = 0
_reg_input1  = 1
_reg_output0 = 2
_reg_output1 = 3
_reg_config0 = 6
_reg_config1 = 7

# 獨立 Pin 常數 (不受 machine.Pin 在不同 port 不同值的影響)
PIN_OUT = 1
PIN_IN = 0


class _Pin:
    """單一 pin 代理 — 相容 machine.Pin"""
    PULL_UP = 1
    PULL_DOWN = 0

    def __init__(self, parent, index):
        self._xl = parent
        self._i = index
        self._port = 0 if index < 8 else 1
        self._bit = index % 8
        self._mask = 1 << self._bit

    def value(self, v=None):
        """value() → 讀取; value(v) → 寫入 (立即 I2C)"""
        if v is None:
            val = self._xl._i2c.readfrom_mem(
                self._xl._addr, _reg_input0 + self._port, 1)[0]
            return (val >> self._bit) & 1
        port = self._port
        if v:
            self._xl._out[port] |= self._mask
        else:
            self._xl._out[port] &= ~self._mask
        self._xl._i2c.writeto_mem(
            self._xl._addr, _reg_output0 + port,
            bytearray([self._xl._out[port] & 0xFF]))

    def init(self, mode=-1, pull=-1):
        """init(mode=PIN_OUT) 或 init(mode=PIN_IN, pull=PIN_PULL_UP)
           也相容 machine.Pin.OUT/Pin.IN (自動偵測)"""
        port = self._port
        # 自含常數: OUT=1, 也相容 machine.Pin 各種值
        if mode >= 1:
            self._xl._cfg[port] &= ~self._mask
        elif mode == 0:
            self._xl._cfg[port] |= self._mask
            if pull == 1:  # PULL_UP
                self._xl._out[port] |= self._mask
                self._xl._i2c.writeto_mem(
                    self._xl._addr, _reg_output0,
                    bytearray([self._xl._out[0] & 0xFF, self._xl._out[1] & 0xFF]))
        if mode >= 0:
            self._xl._i2c.writeto_mem(
                self._xl._addr, _reg_config0,
                bytearray([self._xl._cfg[0] & 0xFF, self._xl._cfg[1] & 0xFF]))


class _Port:
    def __init__(self, parent):
        self._xl = parent
    def __getitem__(self, idx):
        return self._xl._out[idx]
    def __setitem__(self, idx, val):
        self._xl._out[idx] = val & 0xFF


class XL9555:
    def __init__(self, i2c, addr=0x20):
        self._i2c = i2c
        self._addr = addr
        self._out = bytearray([0xFF, 0xFF])
        self._cfg = bytearray([0xFF, 0xFF])
        self.pin = [_Pin(self, i) for i in range(16)]
        self.port = _Port(self)

    def __getitem__(self, index):
        return self.pin[index]

    def __setitem__(self, index, value):
        p = self.pin[index]
        if value:
            self._out[p._port] |= p._mask
        else:
            self._out[p._port] &= ~p._mask

    def show(self):
        self._i2c.writeto_mem(self._addr, _reg_output0,
            bytearray([self._out[0] & 0xFF, self._out[1] & 0xFF]))
