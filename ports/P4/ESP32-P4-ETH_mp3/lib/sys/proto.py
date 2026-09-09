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

SOF = b"NC"
CUR_VER = 4
ADDR_BROADCAST = 0xFFFF

# ── 協議負載上限 (唯一真相源) ──
# 8192 是「純負載」(payload) 位元組數, 不含 header 與 CRC。
# StreamParser 內部會自動加 HDR_LEN(9) + CRC_LEN(4) = 13 位元組去建立緩衝,
# 所以單幀實際最大長度 = MAX_PAYLOAD + 13。所有 StreamParser 建立點都應引用此值,
# 不要各自寫死數字 (以前 app.py 用 Buffer.size*2、web_ui 用 4096*4, 已收斂)。
MAX_PAYLOAD = 8192

# ── 傳輸層 buffer 約定 (與 MAX_PAYLOAD 正交, 唯一真相源) ──
# RX_BUF_SIZE: 接收端每個 slot 的大小 (net_bus + circuit_bus 共用)。
#   ⚠️ 一幀必須能完整塞進一個 slot，不能拆幀。FILE_CHUNK(4KB data) 幀 =
#     HDR(9) + file_id(2) + offset(4) + data(4096) + CRC(4) = 4115。
#   取 4115（剛好一幀）——不是越大越好；扛消費延遲靠「多插槽」(u8_rx_slots)，不靠單槽變大。
# SEND_CAP: socket 每次 send 的分段上限 (lwIP TCP_SND_BUF ≈ 4~5.7KB, 見性能文檔)。
#  單次 send 超過會阻塞等 ACK 造成 8KB 懸崖, 4KB 是發送端甜蜜點。
RX_BUF_SIZE = 4115
SEND_CAP = 4096

HDR_LEN = 9
CRC_LEN = 4

# ── pack 的共享預分配 buffer ──
# pack 內核不再用 header + payload + crc 的 bytes 拼接 (每幀分配+複製, 佔協議成本 78%),
# 改寫進這塊模組級 buffer, 永久重用, 零分配零複製。惰性按需擴充。
_pack_buf = None     # bytearray
_pack_mv = None      # memoryview(_pack_buf)
_pack_cap = 0


class Proto:
    @staticmethod
    def crc32_update(data, crc=0):
        return binascii.crc32(data, crc)

    @staticmethod
    def pack(cmd: int, payload: bytes = b"", addr: int = ADDR_BROADCAST):
        """封裝一個 NC4 幀。

        ⚠️ 生命週期契約: 回傳值是「指向共享 buffer 的 memoryview」,
           下一次呼叫 pack() 會覆蓋它。呼叫端必須「立即消費」(送出/寫入),
           不可跨下一次 pack() 持有。專案內所有呼叫點都是 send(pack(...)) 立即消費,
           已通過審計 (不存在持有兩個 pack 結果的場景)。

        內核: 寫進模組級 _pack_buf (struct.pack_into + 切片賦值), 不做 bytes 拼接。
        效能: 較舊版 (header+payload+crc 拼接) 快 ~17x (協議開銷 -79% → -22%)。"""
        global _pack_buf, _pack_mv, _pack_cap
        if payload is None:
            payload = b""
        ln = len(payload)
        total = HDR_LEN + ln + CRC_LEN
        # 惰性配 / 不夠大才重配 (正常只配一次, 之後全程重用)
        if _pack_buf is None or _pack_cap < total:
            _pack_cap = total + 512   # 預留成長空間, 避免頻繁重配
            _pack_buf = bytearray(_pack_cap)
            _pack_mv = memoryview(_pack_buf)
        b = _pack_mv
        # 1. header (9B): SOF + ver + addr + cmd + payload_len
        struct.pack_into("<2sBHHH", b, 0, SOF, CUR_VER, addr, cmd, ln)
        # 2. payload (直接寫進 buffer, 不建新 bytes)
        if ln:
            b[HDR_LEN:HDR_LEN + ln] = payload
        # 3. CRC32 (ver..payload_end, 同舊版範圍 header[2:])
        crc_val = Proto.crc32_update(b[2:HDR_LEN + ln], 0) & 0xFFFFFFFF
        struct.pack_into("<I", b, HDR_LEN + ln, crc_val)
        return b[:total]


class StreamParser:
    def __init__(self, max_len=MAX_PAYLOAD):
        self.max_len = max_len
        self._buf = bytearray(max_len + HDR_LEN + CRC_LEN)
        self._mv = memoryview(self._buf)   # 零複製切片: pop 不再每幀建新 bytes
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
                # compact: 把未消費段搬到開頭。
                # memoryview slice 賦值 = C 層 memmove, 比 viper 逐 byte 迴圈快。
                self._mv[:keep] = self._mv[self._start:self._end]
            self._start = 0
            self._end = keep
            free = cap - self._end

        if free < ln:
            self._start = 0
            self._end = 0
            return

        # append: memoryview slice 賦值 (C 層 memmove, 最快, 替代 viper 逐 byte 複製)
        self._mv[self._end:self._end + ln] = data
        self._end += ln

    def pop_frame(self):
        """解出單幀, 回傳 (ver, addr, cmd, payload_mv) 或 None。

        payload 是 _buf 的 memoryview (零拷貝), 下次 feed()/pop_frame() 前有效
        (consume-before-next-pop)。非 generator — 供熱路徑 (handle_stream) 連續
        呼叫, 免每幀配置 generator 物件 + bytes 副本 (原 pop() 慢的 ~80% 成本來自
        這兩者引發的 GC churn)。正確性與 pop() 完全一致 (同 SOF 重同步/VER/LEN/CRC32)。"""
        buf = self._buf
        mv = self._mv
        while (self._end - self._start) >= HDR_LEN:
            idx = buf.find(SOF, self._start, self._end)
            if idx < 0:
                self._start = 0
                self._end = 0
                return None
            if idx != self._start:
                self._start = idx
                if (self._end - self._start) < HDR_LEN:
                    return None

            s = self._start
            ver = buf[s + 2]
            addr = buf[s + 3] | (buf[s + 4] << 8)
            cmd = buf[s + 5] | (buf[s + 6] << 8)
            ln = buf[s + 7] | (buf[s + 8] << 8)

            if ver != CUR_VER or ln > self.max_len:
                self._start += 1
                continue

            total_len = HDR_LEN + ln + CRC_LEN
            if (self._end - self._start) < total_len:
                return None

            payload_start = s + HDR_LEN
            payload_end = payload_start + ln
            crc_received = buf[payload_end] | (buf[payload_end + 1] << 8) | (buf[payload_end + 2] << 16) | (buf[payload_end + 3] << 24)
            crc_calc = binascii.crc32(mv[s + 2:payload_end], 0)
            if (crc_calc & 0xFFFFFFFF) == crc_received:
                payload = mv[payload_start:payload_end]  # 零拷貝 view (非 bytes 副本)
                s += total_len
                if s == self._end:
                    self._start = 0
                    self._end = 0
                else:
                    self._start = s
                return ver, addr, cmd, payload
            else:
                self._start += 1
        return None

    def pop(self):
        """生成器 (相容介面): 逐幀 yield (ver, addr, cmd, payload_bytes)。

        payload 是 bytes 副本 (可跨 feed() 安全持有, 無生命週期陷阱)。內部包
        pop_frame() + bytes 拷貝; 較慢 (每幀多一次 generator 物件 + bytes 配置),
        保留給正確性測試 / 需跨幀持有 payload 者。熱路徑請用 pop_frame()。"""
        while True:
            r = self.pop_frame()
            if r is None:
                return
            ver, addr, cmd, payload_mv = r
            yield ver, addr, cmd, bytes(payload_mv)
