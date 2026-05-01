import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, unpack_out_header


class DisplayTask(Task):
    def on_start(self):
        super().on_start()
        self._buf = ensure_dp_buffer_service(bus)
        self._lcd = None
        self._swap_buf = None

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

        rv = hub.get_read_view()
        if rv is None:
            return

        try:
            payload_len, seq, label_id, x, y, w, h, flags, fmt = unpack_out_header(rv)
            payload_len = int(payload_len)
            if payload_len <= 0:
                hub.release_read()
                return
            payload = rv[HDR_OUT : HDR_OUT + payload_len]
            try:
                lcd.set_window(int(x), int(y), int(x) + int(w) - 1, int(y) + int(h) - 1)
            except Exception:
                try:
                    lcd.set_window(int(x), int(y))
                except Exception:
                    pass

            pf = str(self._buf.get("pixel_format") or "")
            if pf.endswith("_LE") and (payload_len & 1) == 0:
                if self._swap_buf is None or len(self._swap_buf) < payload_len:
                    self._swap_buf = bytearray(payload_len)
                sb = self._swap_buf
                j = 0
                while j < payload_len:
                    b0 = payload[j]
                    sb[j] = payload[j + 1]
                    sb[j + 1] = b0
                    j += 2
                lcd.write_data(memoryview(sb)[:payload_len])
            else:
                lcd.write_data(payload)

            self._buf["last_ms"] = time.ticks_ms()
            self._buf["last_err"] = ""
        except Exception as e:
            try:
                self._buf["last_err"] = str(e)
                self._buf["last_ms"] = time.ticks_ms()
            except Exception:
                pass
            self._lcd = None
        finally:
            try:
                hub.release_read()
            except Exception:
                pass

