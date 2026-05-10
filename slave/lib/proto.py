import struct

import sys

IS_MICROPYTHON = (sys.implementation.name == 'micropython')

if not IS_MICROPYTHON:
    class micropython:
        @staticmethod
        def viper(f): return f
        @staticmethod
        def native(f): return f
    ptr8 = bytes
    ptr16 = bytes
    int32 = int
    uint16 = int
else:
    import micropython
    import ubinascii as binascii

if not IS_MICROPYTHON:
    import binascii

SOF = b"NL"
CUR_VER = 4
ADDR_BROADCAST = 0xFFFF
MAX_LEN_DEFAULT = 8192

HDR_FMT = "<2sBHHH"
HDR_LEN = 9
CRC_FMT = "<I"
CRC_LEN = 4


@micropython.viper
def _viper_compact(buf, start: int, end: int, keep: int):
    p = ptr8(buf)
    s = int(p) + start
    d = int(p)
    for i in range(keep):
        ptr8(d)[i] = ptr8(s)[i]


@micropython.viper
def _viper_append(buf, src, end: int, n: int):
    p = ptr8(buf)
    s = ptr8(src)
    d = int(p) + end
    for i in range(n):
        ptr8(d)[i] = s[i]


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
                _viper_compact(self._buf, self._start, self._end, keep)
            self._start = 0
            self._end = keep
            free = cap - self._end

        if free < ln:
            self._start = 0
            self._end = 0
            return

        _viper_append(self._buf, data, self._end, ln)
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
            crc_calc = Proto.crc32_update(self._buf[crc_start:payload_end], 0)
            if (crc_calc & 0xFFFFFFFF) == crc_received:
                payload = self._buf[payload_start:payload_end]
                self._start += total_len
                if self._start == self._end:
                    self._start = 0
                    self._end = 0
                yield ver, addr, cmd, payload
            else:
                self._start += 1
