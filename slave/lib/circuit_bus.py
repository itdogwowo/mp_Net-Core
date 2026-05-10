import struct
import time
from lib.sys_bus import bus
from lib.buffer_hub import AtomicStreamHub


class CircuitBus:
    def __init__(self, io, label="CIRCUIT", rx_hub=None):
        self.io = io
        self.label = label
        self.connected = io is not None
        self._decode_ctx = {}

        buf_cfg = bus.shared.get("Buffer", {}) or {}
        buf_size = buf_cfg.get("size", 4096)
        self._buf = bytearray(buf_size)
        self.rx_hub = rx_hub
        self._drop_buf = bytearray(min(2048, buf_size))
        self._hub_off = 2
        if self.rx_hub is None:
            rx_buffers = int(buf_cfg.get("rx_hub_buffers", 0) or 0)
            if rx_buffers > 0:
                self.rx_hub = AtomicStreamHub(buf_size + self._hub_off, num_buffers=rx_buffers)
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

                struct.pack_into("<H", view, 0, n)
                self.rx_hub.commit()
        except Exception:
            return

    def write(self, data: bytes):
        if not self.connected or self.io is None:
            return False
        try:
            return self._send_all(data)
        except Exception:
            self.connected = False
            return False

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
