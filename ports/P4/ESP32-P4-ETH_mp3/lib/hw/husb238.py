"""
HUSB238 — I2C USB Type-C Power Delivery (PD) Sink Controller
I2C addr (fixed): 0x08 (7-bit)

參考: Adafruit HUSB238 Arduino / CircuitPython 函式庫 (MIT)
將 PD 協商交給 HUSB238 硬體，主控只要透過 I2C 查詢 / 下達需求電壓即可。

用法:
  husb = HUSB238(i2c)                      # addr 預設 0x08

  # 讀取
  husb.is_attached()                       # 是否有 PD 源
  husb.available_voltages()                # 源支援的電壓清單 [5,9,12,15,18,20]
  husb.voltage                             # 目前協商到的電壓 (V) 或 None
  husb.current                             # 目前協商到的電流 (A, float)
  husb.response                            # 上次請求回應碼 (0..5)

  # 設定 (具高階封裝)
  husb.request_voltage(9)                  # 請求 9V;成功回 True
  husb.reset()                             # 硬體 Hard Reset
  husb.get_source_capabilities()           # 重新讀取源能力
"""

import time

# ════════════════════════════════════════════════════════
# 暫存器位址 (Register Map)
# ════════════════════════════════════════════════════════
_HUSB238_I2CADDR = const(0x08)

_REG_PD_STATUS0 = const(0x00)  # 源電壓/電流
_REG_PD_STATUS1 = const(0x01)  # 附加狀態 / CC 方向 / 回應碼 / 5V 合約
_REG_SRC_PDO_5V = const(0x02)  # 各 PDO: bit7=是否偵測到, bit0-3=電流
_REG_SRC_PDO_9V = const(0x03)
_REG_SRC_PDO_12V = const(0x04)
_REG_SRC_PDO_15V = const(0x05)
_REG_SRC_PDO_18V = const(0x06)
_REG_SRC_PDO_20V = const(0x07)
_REG_SRC_PDO = const(0x08)     # bit4-7 = 選擇的 PDO
_REG_GO_COMMAND = const(0x09)  # bit0=Request, bit2=GetSrcCap, bit4=Reset

# ════════════════════════════════════════════════════════
# 查表 (PDO code ↔ 物理量)
# ════════════════════════════════════════════════════════
# PD_STATUS0 高 4 bit (voltage setting code) → 電壓 (V)
#   注意: 這組 code 與 SRC_PDO 選擇用的 code 不同 (見 _VOLTAGE_TO_PDO)
_PDO_TO_VOLTAGE = (
    None,  # 0 Unattached
    5,     # 1
    9,     # 2
    12,    # 3
    15,    # 4
    18,    # 5
    20,    # 6
)

# SRC_PDO 選擇用的 PDO code (寫入 _REG_SRC_PDO bit4-7) ↔ 電壓 (V)
#   這組 code 為 non-contiguous，故用 dict
_VOLTAGE_TO_PDO = {
    5: 0b0001,
    9: 0b0010,
    12: 0b0011,
    15: 0b1000,
    18: 0b1001,
    20: 0b1010,
}

# 電壓 → SRC_PDO 暫存器位址 (查「是否偵測到 / 對應電流」用)
_VOLTAGE_TO_REG = {
    5: _REG_SRC_PDO_5V,
    9: _REG_SRC_PDO_9V,
    12: _REG_SRC_PDO_12V,
    15: _REG_SRC_PDO_15V,
    18: _REG_SRC_PDO_18V,
    20: _REG_SRC_PDO_20V,
}

# PD_STATUS0 低 4 bit (current setting code) → 電流 (A)
_PDO_TO_CURRENT = (
    0.5, 0.7, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25,
    2.5, 2.75, 3.0, 3.25, 3.5, 4.0, 4.5, 5.0,
)

# PD_STATUS1 bit3-5 回應碼 → 字串
_RESPONSE_CODES = (
    "NO RESPONSE",            # 0
    None,                     # 1 Success
    None,                     # 2 Reserved
    "INVALID COMMAND OR ARG", # 3
    "COMMAND NOT SUPPORTED",  # 4
    "TRANSACTION FAILED",     # 5 (No GoodCRC)
)

# PD_STATUS1 bit0-1 → 5V 預設合約電流
_5V_CONTRACT_CURRENT = ("DEFAULT", "1.5A", "2.4A", "3.0A")


class HUSB238:
    """HUSB238 USB PD Sink 控制器驅動。"""

    def __init__(self, i2c, addr=_HUSB238_I2CADDR):
        self._i2c = i2c
        self._addr = addr
        # 單位元讀寫用緩衝 (避免每次配置)
        self._b = bytearray(1)

    # ════════════════════════════════════════════════════════
    # 底層 I2C 暫存器讀寫
    # ════════════════════════════════════════════════════════
    def _read_u8(self, reg):
        """讀單一 8-bit 暫存器"""
        return self._i2c.readfrom_mem(self._addr, reg, 1)[0]

    def _write_u8(self, reg, val):
        """寫單一 8-bit 暫存器"""
        self._b[0] = val & 0xFF
        self._i2c.writeto_mem(self._addr, reg, self._b)

    def _read_bits(self, reg, width, shift):
        """讀 reg 並取出 bit[shift : shift+width]"""
        return (self._read_u8(reg) >> shift) & ((1 << width) - 1)

    def _write_bits(self, reg, value, width, shift):
        """讀改寫: 只動 reg 的 bit[shift : shift+width], 其餘保留"""
        mask = ((1 << width) - 1) << shift
        cur = self._read_u8(reg)
        new = (cur & ~mask) | ((value << shift) & mask)
        self._write_u8(reg, new)

    # ════════════════════════════════════════════════════════
    # PD_STATUS1 (0x01) — 狀態
    # ════════════════════════════════════════════════════════
    def is_attached(self):
        """是否已接上 PD 源 (bit6)"""
        return bool(self._read_bits(_REG_PD_STATUS1, 1, 6))

    def get_cc_direction(self):
        """CC 方向: False=CC1, True=CC2 (bit7)"""
        return bool(self._read_bits(_REG_PD_STATUS1, 1, 7))

    def get_response(self):
        """上次 PD 請求的回應碼 (0..5) (bit3-5)"""
        return self._read_bits(_REG_PD_STATUS1, 3, 3)

    def get_response_str(self):
        """回應碼對應字串 (成功回 None)"""
        code = self.get_response()
        if 0 <= code < len(_RESPONSE_CODES):
            return _RESPONSE_CODES[code]
        return "UNKNOWN"

    def get_5v_contract_v(self):
        """5V 合約電壓是否協商成功 (bit2)"""
        return bool(self._read_bits(_REG_PD_STATUS1, 1, 2))

    def get_5v_contract_a(self):
        """5V 預設合約電流字串 (bit0-1): 'DEFAULT'/'1.5A'/'2.4A'/'3.0A'"""
        code = self._read_bits(_REG_PD_STATUS1, 2, 0)
        return _5V_CONTRACT_CURRENT[code] if code < 4 else "UNKNOWN"

    # ════════════════════════════════════════════════════════
    # PD_STATUS0 (0x00) — 目前協商結果
    # ════════════════════════════════════════════════════════
    def get_src_voltage_code(self):
        """目前源電壓 code (PD_STATUS0 bit4-7)"""
        return self._read_bits(_REG_PD_STATUS0, 4, 4)

    def get_src_current_code(self):
        """目前源電流 code (PD_STATUS0 bit0-3)"""
        return self._read_bits(_REG_PD_STATUS0, 4, 0)

    @property
    def voltage(self):
        """目前協商到的電壓 (V, int)；未附加回 None"""
        code = self.get_src_voltage_code()
        if 0 <= code < len(_PDO_TO_VOLTAGE):
            return _PDO_TO_VOLTAGE[code]
        return None

    @property
    def current(self):
        """目前協商到的電流 (A, float)"""
        code = self.get_src_current_code()
        if 0 <= code < len(_PDO_TO_CURRENT):
            return _PDO_TO_CURRENT[code]
        return None

    @property
    def response(self):
        """同 get_response() — 相容 CircuitPython 風格"""
        return self.get_response()

    # ════════════════════════════════════════════════════════
    # SRC_PDO_5V..20V (0x02..0x07) — 源能力查詢
    # ════════════════════════════════════════════════════════
    def is_voltage_detected(self, voltage):
        """指定電壓是否被源支援 (讀對應 PDO reg 的 bit7)"""
        reg = _VOLTAGE_TO_REG.get(voltage)
        if reg is None:
            return False
        return bool(self._read_bits(reg, 1, 7))

    def current_detected(self, voltage):
        """指定電壓對應的最大電流 (A, float)；不支援回 None"""
        reg = _VOLTAGE_TO_REG.get(voltage)
        if reg is None:
            return None
        code = self._read_bits(reg, 4, 0)
        if 0 <= code < len(_PDO_TO_CURRENT):
            return _PDO_TO_CURRENT[code]
        return None

    def available_voltages(self):
        """回傳源支援的電壓清單 [5,9,12,15,18,20] (子集)"""
        out = []
        for v in (5, 9, 12, 15, 18, 20):
            if self.is_voltage_detected(v):
                out.append(v)
        return out

    def available_capabilities(self):
        """回傳 [(電壓, 電流A), ...] 完整能力表"""
        out = []
        for v in (5, 9, 12, 15, 18, 20):
            if self.is_voltage_detected(v):
                out.append((v, self.current_detected(v)))
        return out

    # ════════════════════════════════════════════════════════
    # SRC_PDO (0x08) — 選擇目標 PDO
    # ════════════════════════════════════════════════════════
    def get_selected_pd(self):
        """目前寫入的 PDO 選擇 code (SRC_PDO bit4-7)"""
        return self._read_bits(_REG_SRC_PDO, 4, 4)

    def select_pd(self, voltage):
        """選擇目標 PDO (僅寫暫存器，不下 GO 命令)"""
        code = _VOLTAGE_TO_PDO.get(voltage)
        if code is None:
            raise ValueError("Invalid voltage: {}V".format(voltage))
        self._write_bits(_REG_SRC_PDO, code, 4, 4)

    # ════════════════════════════════════════════════════════
    # GO_COMMAND (0x09) — 觸發動作
    # ════════════════════════════════════════════════════════
    def request_pd(self):
        """送出 PD 請求 (GO bit0 = 0b00001)"""
        self._write_u8(_REG_GO_COMMAND, 0b00001)

    def get_source_capabilities(self):
        """要求重新讀取源能力 (GO bit2 = 0b00100)"""
        self._write_bits(_REG_GO_COMMAND, 0b00100, 5, 0)

    def reset(self):
        """Hard Reset (GO bit4 = 0b10000)"""
        self._write_bits(_REG_GO_COMMAND, 0b10000, 5, 0)

    # ════════════════════════════════════════════════════════
    # 高階封裝
    # ════════════════════════════════════════════════════════
    def request_voltage(self, voltage, settle_ms=10):
        """高階: 選 PDO + 送請求 + 等待 + 查回應。

        回傳 True=成功, False=失敗 (可用 get_response_str() 查原因)。
        """
        if voltage not in _VOLTAGE_TO_PDO:
            raise ValueError("Invalid voltage: {}V".format(voltage))
        self.select_pd(voltage)
        self.request_pd()
        time.sleep_ms(settle_ms)
        return self.get_response() == 1  # 1 = Success

    def dump_regs(self):
        """除錯用: 一次讀回 0x00~0x09 暫存器原始值 + 解析摘要"""
        regs = {}
        for r in range(0x00, 0x0A):
            try:
                regs[r] = self._read_u8(r)
            except Exception as e:
                regs[r] = None
        lines = []
        names = {
            0x00: "PD_STATUS0", 0x01: "PD_STATUS1",
            0x02: "PDO_5V", 0x03: "PDO_9V", 0x04: "PDO_12V",
            0x05: "PDO_15V", 0x06: "PDO_18V", 0x07: "PDO_20V",
            0x08: "SRC_PDO(sel)", 0x09: "GO_CMD",
        }
        for r in range(0x00, 0x0A):
            v = regs[r]
            if v is None:
                lines.append("0x{:02X} {:<12} ERR".format(r, names[r]))
            else:
                lines.append("0x{:02X} {:<12} 0x{:02X} (0b{:08b})".format(
                    r, names[r], v, v))
        print("── HUSB238 @0x{:02X} register dump ──".format(self._addr))
        for l in lines:
            print(l)
        # 解析摘要
        s0 = regs.get(0x00) or 0
        s1 = regs.get(0x01) or 0
        print("  attached={} cc2={} resp={} volt_code={} curr_code={}".format(
            bool(s1 & 0x40), bool(s1 & 0x80), (s1 >> 3) & 0x07,
            (s0 >> 4) & 0x0F, s0 & 0x0F))
        pdo_detect = [(regs.get(r) or 0) & 0x80 for r in range(0x02, 0x08)]
        print("  PDO detect bit7:", pdo_detect)
        return regs

    def status(self):
        """一次讀回常用狀態 dict (減少 I2C 往返: 6 次讀)"""
        s1 = self._read_u8(_REG_PD_STATUS1)
        s0 = self._read_u8(_REG_PD_STATUS0)
        v_code = (s0 >> 4) & 0x0F
        i_code = s0 & 0x0F
        resp = (s1 >> 3) & 0x07
        return {
            "attached": bool(s1 & 0x40),
            "cc2": bool(s1 & 0x80),
            "response": resp,
            "response_str": _RESPONSE_CODES[resp] if resp < len(_RESPONSE_CODES) else "UNKNOWN",
            "voltage": _PDO_TO_VOLTAGE[v_code] if v_code < len(_PDO_TO_VOLTAGE) else None,
            "current": _PDO_TO_CURRENT[i_code] if i_code < len(_PDO_TO_CURRENT) else None,
            "5v_contract": _5V_CONTRACT_CURRENT[s1 & 0x03],
        }
