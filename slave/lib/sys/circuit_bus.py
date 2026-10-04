import struct
import time
from lib.sys.sys_bus import bus
from lib.sys.buffer_hub import AtomicStreamHub
from lib.sys.proto import RX_BUF_SIZE


class CircuitBus:
    def __init__(self, io, label="CIRCUIT", rx_hub=None):
        self.io = io
        self.label = label
        self.connected = io is not None
        self._decode_ctx = {}

        buf_cfg = bus.shared.get("Buffer", {}) or {}
        buf_size = RX_BUF_SIZE
        self._buf = bytearray(buf_size)
        self.rx_hub = rx_hub
        self._drop_buf = bytearray(min(2048, buf_size))
        self._hub_off = 2
        if self.rx_hub is None:
            slots = int(buf_cfg.get("u8_rx_slots", 8) or 0)
            if slots > 0:
                slots = min(slots, 16)
                self.rx_hub = AtomicStreamHub(buf_size + self._hub_off, num_buffers=slots)
        self.cache_hub = None  # 消費端緩存(rx_hub 鏡像),首次 read_into() 時惰性建立一次,永久重用
        self._drop_on_full = int(buf_cfg.get("drop_on_full", 0) or 0)
        self._drain_reads = int(buf_cfg.get("drain_reads", 1) or 0)
        if self._drain_reads <= 0:
            self._drain_reads = 1
        self._send_retry = int(buf_cfg.get("send_retry", 64) or 0)
        if self._send_retry <= 0:
            self._send_retry = 64

    def poll(self, **extra_ctx):
        if not self.connected or self.io is None:
            return
        if self.rx_hub is None:
            return

        try:
            if extra_ctx:
                self._decode_ctx = extra_ctx

            buf_cfg = bus.shared.get("Buffer", {}) or {}
            dr = int(buf_cfg.get("drain_reads", self._drain_reads) or 0)
            if dr <= 0:
                dr = 1
            self._drain_reads = dr

            for _ in range(dr):
                view = self.rx_hub.get_write_view()
                if view is None:
                    if not self._drop_on_full:
                        break
                    try:
                        if hasattr(self.io, "readinto"):
                            self.io.readinto(self._drop_buf)
                        elif hasattr(self.io, "read"):
                            self.io.read(len(self._drop_buf))
                    except Exception:
                        pass
                    continue

                pv = memoryview(view)[self._hub_off:]
                n = 0
                try:
                    if hasattr(self.io, "readinto"):
                        n = self.io.readinto(pv)
                    elif hasattr(self.io, "read"):
                        raw_bytes = self.io.read(len(pv))
                        if raw_bytes:
                            n = len(raw_bytes)
                            pv[:n] = raw_bytes
                    else:
                        n = 0
                except Exception:
                    n = 0

                if n is None or n <= 0:
                    break

                self._commit(view, n)
        except Exception:
            return

    def _commit(self, view, n):
        """提交一筆資料到 rx_hub(解碼器讀);若 cache_hub 已建立則同時鏡像複製一份
        (消費端 read_into 讀)。cache_hub 為 None 時只做原本 commit,零成本。"""
        struct.pack_into("<H", view, 0, n)
        self.rx_hub.commit()
        cache = self.cache_hub
        if cache is not None:
            cview = cache.get_write_view()
            if cview is not None:
                take = 2 + n  # 含 2-byte 長度標頭(mv[a:b]=mv[c:d] 兩邊長度必須相同)
                cview[:take] = view[:take]
                cache.commit()

    def read_into(self, target):
        """消費端直接讀取:複製本 bus 緩存(cache_hub)的下一筆原始資料到 target。
        回傳實際長度(不含 2-byte 長度標頭);無資料回 0。
        cache_hub 首次呼叫時建立一次,之後永久重用(存在 self.cache_hub)。
        cache_hub 與 rx_hub 各自 SPSC:解碼器讀 rx_hub,本方法讀 cache_hub,
        互不影響;不碰底層 io,不影響 poll()。"""
        cache = self.cache_hub
        if cache is None:
            buf_cfg = bus.shared.get("Buffer", {}) or {}
            size = RX_BUF_SIZE + self._hub_off
            slots = int(buf_cfg.get("u8_rx_slots", 2) or 0)
            slots = min(slots, 4)
            cache = AtomicStreamHub(size, num_buffers=slots)
            self.cache_hub = cache
        view = cache.get_read_view()
        if view is None:
            return 0
        ln = view[0] | (view[1] << 8)
        n = 0
        if ln > 0:
            src = memoryview(view)[2:2 + ln]
            n = min(ln, len(target))
            target[:n] = src[:n]
        cache.release_read()
        return n

    def write(self, data: bytes):
        if not self.connected or self.io is None:
            return False
        try:
            return self._send_all(data)
        except Exception:
            self.connected = False
            return False

    def write_to(self, dst_mac, data: bytes):
        """定向送 —— **共用線不需要實體位址**，靠幀頭 addr 過濾。

        `dst_mac` 是射頻層憑證（UART/WS 沒有這種東西 → 一律 None，忽略）。
        存在只為了讓「解碼鏈的延後發射」用同一段程式碼服務所有管子
        （見 `tasks/bus_decode.py` 的 `_send_deferred`）。
        """
        return self.write(data)

    def inject(self, frame):
        """把一幀餵進**本管的 rx_hub** → 它會像「剛從這條線收到」一樣走解碼鏈。

        ★ 這是「本機發起」的入口（vBus 的用途）。2026-10 從
          `ScheduleTask._inject()` 搬過來 —— 它是**通用的**（任何 bus 的 rx_hub
          都能被注入），不該是 scheduler 的私有方法；放在 bus 上就不必再傳 `cb`。
        ★ 與 `tasks/bus_decode.py` 的 `_TxOut` / `defer` 的分工：
            `inject` = 寫進**自己**的 hub（單一 bus 的操作）
            `defer`  = **跨 bus** 的排程（需要輪詢點，所以放解碼鏈）
        """
        hub = self.rx_hub
        if hub is None:
            return False
        n = len(frame)
        if n > RX_BUF_SIZE:
            print("[{}] 注入幀過長 {}>{}，跳過".format(self.label, n, RX_BUF_SIZE))
            return False
        view = hub.get_write_view()
        if view is None:
            return False        # 解碼端消化不及 → 掉（不重送）
        struct.pack_into("<H", view, 0, n)
        off = self._hub_off
        view[off:off + n] = frame
        hub.commit()
        return True

    def _send_all(self, data):
        mv = memoryview(data)
        ln = len(mv)
        off = 0
        retry = 0
        while off < ln:
            try:
                n = self.io.write(mv[off:])
                if n is None:
                    n = 0
                if n > 0:
                    off += n
                    retry = 0
                    continue
            except Exception:
                self.connected = False
                return False
            retry += 1
            if retry >= self._send_retry:
                self.connected = False
                return False
            try:
                time.sleep_ms(0)
            except Exception:
                try:
                    time.sleep(0)
                except Exception:
                    pass
        return True
