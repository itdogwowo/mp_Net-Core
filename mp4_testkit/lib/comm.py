import struct

from lib.buffer_hub import AtomicStreamHub
from lib.proto import StreamParser, SOF, CUR_VER, ADDR_BROADCAST, HDR_LEN, CRC_LEN


class UartBus:
    def __init__(self, uart, *, label="UART", buf_size=4096, rx_buffers=8, drop_on_full=1):
        self.uart = uart
        self.label = label
        self.rx_hub = AtomicStreamHub(int(buf_size) + 2, num_buffers=int(rx_buffers))
        self._buf = bytearray(int(buf_size))
        self._drop_on_full = int(drop_on_full) or 0

    def write(self, data):
        return self.uart.write(data)

    def poll(self):
        while True:
            n = self.uart.readinto(self._buf)
            if not n:
                return
            w = self.rx_hub.get_write_view()
            if w is None:
                if not self._drop_on_full:
                    return
                continue
            if n > len(self._buf):
                n = len(self._buf)
            w[0] = n & 0xFF
            w[1] = (n >> 8) & 0xFF
            w[2:2 + n] = self._buf[:n]
            self.rx_hub.commit()


class Comms:
    def __init__(self, buses, *, max_len=8192, decode_budget_slots=32):
        self._buses = list(buses or [])
        self._parsers = {}
        self._handlers = {}
        self._on_packet = None
        self._max_len = int(max_len) if int(max_len) > 0 else 8192
        self._decode_budget_slots = int(decode_budget_slots) if int(decode_budget_slots) > 0 else 1
        max_hub = 0
        for b in self._buses:
            hub = getattr(b, "rx_hub", None)
            if hub is not None and int(getattr(hub, "size", 0) or 0) > max_hub:
                max_hub = int(hub.size)
        if max_hub <= 0:
            max_hub = self._max_len + 2
        self._read_buf = bytearray(max_hub)
        self._tx_buf = bytearray(self._max_len + HDR_LEN + CRC_LEN)

    def on(self, cmd_int, handler):
        self._handlers[int(cmd_int) & 0xFFFF] = handler

    def on_packet(self, handler):
        self._on_packet = handler

    def send(self, bus, cmd, payload=b"", addr=ADDR_BROADCAST):
        if payload is None:
            payload = b""
        ln = len(payload)
        if ln > self._max_len:
            return False

        struct.pack_into("<2sBHHH", self._tx_buf, 0, SOF, CUR_VER, int(addr) & 0xFFFF, int(cmd) & 0xFFFF, ln)
        mv = memoryview(self._tx_buf)
        mv[HDR_LEN:HDR_LEN + ln] = payload

        import ubinascii as binascii
        crc_val = binascii.crc32(mv[2:HDR_LEN], 0)
        crc_val = binascii.crc32(mv[HDR_LEN:HDR_LEN + ln], crc_val)
        struct.pack_into("<I", self._tx_buf, HDR_LEN + ln, crc_val & 0xFFFFFFFF)

        total = HDR_LEN + ln + CRC_LEN
        bus.write(mv[:total])
        return True

    def poll(self, *, decode_budget_slots=None):
        max_slots = self._decode_budget_slots if decode_budget_slots is None else int(decode_budget_slots)
        if max_slots <= 0:
            max_slots = 1
        used = 0
        mv = memoryview(self._read_buf)
        for b in self._buses:
            if used >= max_slots:
                return
            if hasattr(b, "poll"):
                b.poll()
            hub = getattr(b, "rx_hub", None)
            if hub is None:
                continue
            p = self._parsers.get(id(b))
            if p is None:
                p = StreamParser(max_len=self._max_len)
                self._parsers[id(b)] = p

            while True:
                if used >= max_slots:
                    return
                if not hub.read_into(self._read_buf):
                    break
                ln = self._read_buf[0] | (self._read_buf[1] << 8)
                if ln <= 0:
                    continue
                data = mv[2:2 + ln]
                p.feed(data)
                ctx_extra = getattr(b, "_decode_ctx", None) or {}
                for ver, addr, cmd, payload in p.pop():
                    if self._on_packet is not None:
                        self._on_packet(ver, addr, cmd, payload, b, self)
                    h = self._handlers.get(int(cmd) & 0xFFFF)
                    if h is not None:
                        ctx = {
                            "transport": getattr(b, "label", "Bus"),
                            "bus": b,
                            "comm": self,
                            "ver": int(ver),
                            "addr": int(addr),
                            "cmd": int(cmd),
                        }
                        ctx.update(ctx_extra)
                        h(ctx, payload)
                    used += 1


def init_comms_from_config(bus, cfg):
    if not isinstance(cfg, dict):
        return None

    buf_cfg = cfg.get("Buffer", {}) or {}
    if "Buffer" not in bus.shared:
        bus.shared["Buffer"] = dict(buf_cfg) if isinstance(buf_cfg, dict) else {}
    if "Buffer" not in cfg:
        cfg["Buffer"] = bus.shared.get("Buffer", {}) or {}

    bcfg = bus.shared.get("Buffer", {}) or {}
    buf_size = int(bcfg.get("size", 4096) or 4096)
    rx_buffers = int(bcfg.get("rx_hub_buffers", 8) or 8)
    decode_slots = int(bcfg.get("decode_budget_slots", 32) or 32)

    uart_cfg = cfg.get("UART", {}) or {}
    if not int(uart_cfg.get("enable", 0) or 0):
        return None

    import machine

    buses = []
    for item in (uart_cfg.get("list", []) or []):
        uid = int(item.get("id", 1) or 1)
        baud = int(item.get("baudrate", 115200) or 115200)
        gpio = item.get("GPIO", {}) or {}
        tx = gpio.get("tx", None)
        rx = gpio.get("rx", None)
        uart = None
        try:
            uart = machine.UART(
                uid,
                baudrate=baud,
                bits=8,
                parity=None,
                stop=1,
                tx=machine.Pin(tx) if tx is not None else None,
                rx=machine.Pin(rx) if rx is not None else None,
                timeout=0,
                timeout_char=0,
            )
        except TypeError:
            uart = machine.UART(
                uid,
                baudrate=baud,
                bits=8,
                parity=None,
                stop=1,
                tx=tx,
                rx=rx,
                timeout=0,
                timeout_char=0,
            )
        if uart is None:
            continue
        label = "CIRCUIT-UART{}".format(uid)
        buses.append(UartBus(uart, label=label, buf_size=buf_size, rx_buffers=rx_buffers))

    if not buses:
        return None

    comm = Comms(buses, max_len=buf_size, decode_budget_slots=decode_slots)
    bus.set_service("comm", comm)
    return comm
