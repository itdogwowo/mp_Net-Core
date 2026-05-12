import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, unpack_out_header_into


class DisplayTask(Task):
    def on_start(self):
        super().on_start()
        self._buf = ensure_dp_buffer_service(bus)
        self._lcd = None
        self._read_buf = None
        self._out_hdr = [0] * 9

    def _resolve_lcd(self):
        if self._lcd is not None:
            return self._lcd
        lcd = bus.get_service("lcd")
        if lcd is None:
            lcd = bus.get_service("tft")
        self._lcd = lcd
        return lcd

    def loop(self):
        if not self.running:
            return

        lcd = self._resolve_lcd()
        if lcd is None:
            return

        self._buf = bus.get_service("dp_buffer") or self._buf
        if not self._buf or not self._buf.get("enable", True):
            return

        hub = self._buf.get("out_hub")
        if hub is None:
            return

        hub_size = HDR_OUT + int(self._buf.get("max_frame_bytes", 0) or 0)
        if self._read_buf is None or len(self._read_buf) < hub_size:
            self._read_buf = bytearray(hub_size)

        if not hub.read_into(self._read_buf):
            return

        try:
            unpack_out_header_into(self._read_buf, self._out_hdr)
            payload_len = int(self._out_hdr[0])
            if payload_len <= 0:
                return
            seq = self._out_hdr[1]
            label_id = self._out_hdr[2]
            x = self._out_hdr[3]
            y = self._out_hdr[4]
            w = self._out_hdr[5]
            h = self._out_hdr[6]
            flags = self._out_hdr[7]
            fmt = self._out_hdr[8]
            payload = self._read_buf[HDR_OUT : HDR_OUT + payload_len]
            try:
                lcd.set_window(int(x), int(y), int(x) + int(w) - 1, int(y) + int(h) - 1)
            except Exception:
                try:
                    lcd.set_window(int(x), int(y))
                except Exception:
                    pass

            lcd.write_data(payload)

            self._buf["last_ms"] = time.ticks_ms()
            self._buf["last_err"] = ""
            self.success += 1
        except Exception as e:
            try:
                self._buf["last_err"] = str(e)
                self._buf["last_ms"] = time.ticks_ms()
            except Exception:
                pass
            self._lcd = None
