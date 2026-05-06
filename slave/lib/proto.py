import struct

# --- 跨平台兼容墊片 (PC 與 MicroPython 共享) ---
import sys

# 檢測是否為 MicroPython
IS_MICROPYTHON = (sys.implementation.name == 'micropython')

if not IS_MICROPYTHON:
    # 這裡是 PC (Standard Python) 環境
    # 定義模擬的 MicroPython 裝飾器
    class micropython:
        @staticmethod
        def viper(f): return f
        @staticmethod
        def native(f): return f
    
    # 定義模擬的 Viper 類型關鍵字，防止 NameError
    ptr8 = bytes
    ptr16 = bytes
    int32 = int
    uint16 = int
else:
    # 這裡是 MicroPython 環境
    import micropython
    import ubinascii as binascii

if not IS_MICROPYTHON:
    import binascii

# --- 協議常量 ---
SOF = b"NL"
CUR_VER = 4
ADDR_BROADCAST = 0xFFFF
MAX_LEN_DEFAULT = 8192

HDR_FMT = "<2sBHHH"
HDR_LEN = 9 # struct.calcsize(HDR_FMT)
CRC_FMT = "<I"
CRC_LEN = 4

class Proto:
    @staticmethod
    def crc32_update(data, crc=0):
        return binascii.crc32(data, crc)

    @staticmethod
    def pack(cmd: int, payload: bytes = b"", addr: int = ADDR_BROADCAST) -> bytes:
        if payload is None: payload = b""
        ln = len(payload)
        header = struct.pack(HDR_FMT, SOF, CUR_VER, addr, cmd, ln)
        crc_val = Proto.crc32_update(header[2:], 0)
        crc_val = Proto.crc32_update(payload, crc_val)
        return header + payload + struct.pack(CRC_FMT, crc_val)

class StreamParser:
    def __init__(self, max_len=MAX_LEN_DEFAULT):
        self.max_len = max_len
        self._buf = bytearray(max_len + HDR_LEN + CRC_LEN)
        self._mv = memoryview(self._buf)
        self._start = 0
        self._end = 0
        
    def feed(self, data):
        if not data:
            return
        ln = len(data)
        cap = len(self._buf)
        if ln > cap:
            self._start = 0
            self._end = 0
            return

        free = cap - self._end
        if free < ln and self._start:
            keep = self._end - self._start
            if keep:
                self._mv[:keep] = self._mv[self._start:self._end]
            self._start = 0
            self._end = keep
            free = cap - self._end

        if free < ln:
            self._start = 0
            self._end = 0
            return

        self._mv[self._end:self._end + ln] = data
        self._end += ln

    def pop(self):
        while (self._end - self._start) >= HDR_LEN:
            idx = self._buf.find(SOF, self._start, self._end)
            if idx < 0:
                self._start = 0
                self._end = 0
                return

            if idx != self._start:
                self._start = idx
                if (self._end - self._start) < HDR_LEN:
                    return

            sof, ver, addr, cmd, ln = struct.unpack_from(HDR_FMT, self._buf, self._start)

            if ver != CUR_VER or ln > self.max_len:
                self._start += 1
                continue

            total_len = HDR_LEN + ln + CRC_LEN
            if (self._end - self._start) < total_len:
                return

            payload_start = self._start + HDR_LEN
            payload_end = payload_start + ln
            crc_received = struct.unpack_from(CRC_FMT, self._buf, payload_end)[0]

            crc_start = self._start + 2
            crc_len = payload_end - crc_start
            crc_calc = Proto.crc32_update(self._mv[crc_start:payload_end], 0)
            if (crc_calc & 0xFFFFFFFF) == crc_received:
                payload = self._mv[payload_start:payload_end]
                self._start += total_len
                if self._start == self._end:
                    self._start = 0
                    self._end = 0
                yield ver, addr, cmd, payload
            else:
                self._start += 1
