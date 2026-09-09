"""
GT1151Q — I2C Capacitive Touch Controller
I2C addr (common): 0x5D (7-bit) or 0x14 (configurable)
INT pin: GPIO 42 (active low)
"""

# Registers
GT_REG_STATUS     = const(0x814E)  # Touch status + points count
GT_REG_TOUCH1     = const(0x814F)  # Touch 1 data (5 bytes)
GT_REG_TOUCH2     = const(0x8154)  # Touch 2 data (5 bytes)
GT_REG_CONFIG     = const(0x8047)  # Config version
GT_REG_FW         = const(0x8140)  # FW version
GT_REG_SLEEP      = const(0x8040)  # Sleep mode
GT_REG_ID         = const(0x8140)  # Product ID + FW


class TouchPoint:
    def __init__(self):
        self.x = 0
        self.y = 0
        self.pressure = 0
        self.area = 0

    def __repr__(self):
        return "TouchPoint(x={}, y={}, pressure={}, area={})".format(
            self.x, self.y, self.pressure, self.area)


class GT1151Q:
    def __init__(self, i2c, addr=0x5D, int_pin=None):
        self._i2c = i2c
        self._addr = addr
        self._int = int_pin
        self._points = [TouchPoint(), TouchPoint()]
        self._ready = False

    def _write_reg16(self, reg16, data):
        buf = bytearray([reg16 >> 8, reg16 & 0xFF]) + bytes(data)
        self._i2c.writeto(self._addr, buf)

    def _read_reg16(self, reg16, length=1):
        self._i2c.writeto(self._addr, bytearray([reg16 >> 8, reg16 & 0xFF]))
        return self._i2c.readfrom(self._addr, length)

    def init(self):
        """初始化觸控晶片"""
        try:
            # 讀取產品 ID
            data = self._read_reg16(GT_REG_ID, 4)
            pid = data.decode('ascii')
            fw = self._read_reg16(GT_REG_CONFIG, 1)[0]
            pid = pid[:4]
            print("[GT1151Q] PID={} FW=0x{:02X} addr=0x{:02X}".format(pid, fw, self._addr))
            self._ready = True
        except Exception as e:
            print("[GT1151Q] init error: {}".format(e))
            return False
        return True

    def available(self):
        """檢查是否有觸摸資料"""
        if self._int is not None:
            return self._int.value() == 0  # INT active low
        return True

    def read_points(self):
        """讀取觸摸點 (回傳 list of TouchPoint)"""
        if not self._ready:
            return []

        try:
            status = self._read_reg16(GT_REG_STATUS, 2)
            if len(status) < 2:
                return []

            buf = status

            # 清除中斷
            self._write_reg16(GT_REG_STATUS, b'\x00\x00')
        except Exception:
            return []

        # 沒有觸摸
        if buf[1] & 0x80 == 0:
            return []

        count = buf[1] & 0x0F
        if count > 2:
            count = 2
        if count == 0:
            return []

        result = []
        try:
            for i in range(count):
                reg = GT_REG_TOUCH1 + i * 5
                tdata = self._read_reg16(reg, 5)
                if len(tdata) >= 5:
                    tp = TouchPoint()
                    # 格式: track_id(1) + x(2) + y(2) little-endian
                    tp.area = tdata[0]    # track ID
                    tp.x = tdata[1] | (tdata[2] << 8)
                    tp.y = tdata[3] | (tdata[4] << 8)
                    result.append(tp)
        except Exception:
            pass

        return result
