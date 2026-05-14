import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, unpack_out_header_into


class DisplayTask(Task):
    log_schema = ["fps_window", "fps_total", "disp_src_fill"]

    def on_start(self):
        super().on_start()
        self._buf = ensure_dp_buffer_service(bus)
        self._lcd = None
        self._out_hdr = [0] * 9
        self._last_x = -1
        self._last_y = -1
        self._last_w = -1
        self._last_h = -1

        self._fps_window_t0 = 0
        self._fps_window_count = 0
        self._fps_start_ms = 0
        self._fps_total_frames = 0

    def _resolve_lcd(self):
        if self._lcd is not None:
            return self._lcd
        lcd = bus.get_service("lcd")
        if lcd is None:
            lcd = bus.get_service("tft")
        self._lcd = lcd
        return lcd

    def _tick_fps(self):
        self._fps_window_count += 1
        self._fps_total_frames += 1

        now = time.ticks_ms()
        if self._fps_start_ms == 0 and self._fps_total_frames > 0:
            self._fps_start_ms = now

        if self._fps_window_t0 == 0:
            self._fps_window_t0 = now
            return

        dt = time.ticks_diff(now, self._fps_window_t0)
        interval = int(self.fcache_get("fps_stats_interval", 1000, ttl_ms=3000) or 1000)
        if dt < interval:
            return

        fps_window = self._fps_window_count
        self._lw_ex(0, fps_window)

        if self._fps_start_ms > 0:
            total_elapsed = time.ticks_diff(now, self._fps_start_ms)
            if total_elapsed > 0:
                fps_cumulative = self._fps_total_frames * 1000 // total_elapsed
                self._lw_ex(1, fps_cumulative)
            else:
                self._lw_ex(1, 0)
        else:
            self._lw_ex(1, 0)

        self._fps_window_t0 = now
        self._fps_window_count = 0

    def loop(self):
        if not self.running:
            return

        lcd = self._resolve_lcd()
        if lcd is None:
            return

        self._buf = bus.get_service("dp_buffer") or self._buf
        if not self._buf or not self._buf.get("enable", True):
            return

        use_jpeg_out = str(self._buf.get("pixel_format") or "").startswith("RGB888")
        hub = self._buf.get("jpeg_out") if use_jpeg_out else self._buf.get("out_hub")
        if hub is None:
            return
        try:
            self._lw_ex(2, int(hub.get_fill_level() or 0) + 1)
        except Exception:
            pass

        rv = hub.get_read_view()
        if rv is None:
            return

        try:
            unpack_out_header_into(rv, self._out_hdr)
            payload_len = int(self._out_hdr[0])
            if payload_len <= 0:
                return
            x = int(self._out_hdr[3])
            y = int(self._out_hdr[4])
            w = int(self._out_hdr[5])
            h = int(self._out_hdr[6])
            payload = rv[HDR_OUT : HDR_OUT + payload_len]
            if x != self._last_x or y != self._last_y or w != self._last_w or h != self._last_h:
                try:
                    lcd.set_window(x, y, x + w - 1, y + h - 1)
                except Exception:
                    try:
                        lcd.set_window(x, y)
                    except Exception:
                        pass
                self._last_x = x
                self._last_y = y
                self._last_w = w
                self._last_h = h

            lcd.write_data(payload)

            self._buf["last_ms"] = time.ticks_ms()
            self._buf["last_err"] = ""
            self.success += 1
            self._tick_fps()
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
