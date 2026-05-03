import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, pack_out_header


class DpBufferTask(Task):
    def on_start(self):
        super().on_start()
        self._svc = ensure_dp_buffer_service(bus)
        self._fps_window_t0 = 0
        self._fps_window_count = 0
        self._fps_start_ms = 0
        self._fb_buf = None
        self._fb = None
        self._fb_group = -1

    def _ensure_fb(self):
        w = int(bus.shared.get("tft_width", 240) or 240)
        h = int(bus.shared.get("tft_height", 320) or 320)
        bpp = int((self._svc.get("pixel_format") or "RGB565_BE").startswith("RGB888") and 3 or 2)
        needed = w * h * bpp
        if self._fb_buf is not None and len(self._fb_buf) >= needed:
            return True
        try:
            import framebuf
            self._fb_buf = bytearray(needed)
            self._fb = framebuf.FrameBuffer(self._fb_buf, w, h, framebuf.RGB565)
            return True
        except Exception as e:
            self._svc["last_err"] = "fb alloc: " + str(e)
            self._svc["last_ms"] = time.ticks_ms()
            return False

    def _reset_fb(self):
        if self._fb is not None:
            self._fb.fill(0)
        self._fb_group = -1

    def _tick_fps(self):
        self._fps_window_count += 1
        total_frames = int(self._svc.get("frames", 0) or 0)
        if self._fps_start_ms == 0 and total_frames > 0:
            self._fps_start_ms = time.ticks_ms()

        now = time.ticks_ms()
        if self._fps_window_t0 == 0:
            self._fps_window_t0 = now
            return

        dt = time.ticks_diff(now, self._fps_window_t0)
        interval = int(bus.shared.get("fps_stats_interval", 1000) or 1000)
        if dt < interval:
            return

        fps_window = self._fps_window_count
        self._svc["fps_window"] = fps_window

        if self._fps_start_ms > 0:
            total_elapsed = time.ticks_diff(now, self._fps_start_ms)
            if total_elapsed > 0:
                fps_cumulative = total_frames * 1000 // total_elapsed
                self._svc["fps_total"] = fps_cumulative
            else:
                self._svc["fps_total"] = 0

        self._fps_window_t0 = now
        self._fps_window_count = 0

    def loop(self):
        if not self.running:
            return

        self._svc = bus.get_service("dp_buffer") or self._svc
        if not self._svc or not self._svc.get("enable", True):
            self._fb_group = -1
            return

        pending = self._svc.get("pending")
        if not pending:
            return

        hub = self._svc.get("out_hub")
        if hub is None:
            return

        wv = hub.get_write_view()
        if wv is None:
            return

        payload_len = int(pending.get("payload_len", 0) or 0)
        if payload_len <= 0:
            self._svc["pending"] = None
            return

        payload = wv[HDR_OUT : HDR_OUT + payload_len]
        group = int(pending.get("frame_group", 0) or 0)
        x = int(pending.get("x", 0) or 0)
        y = int(pending.get("y", 0) or 0)
        w = int(pending.get("w", 0) or 0)
        h = int(pending.get("h", 0) or 0)

        blend_mode = str(bus.shared.get("jpeg_blend_mode", "interleave") or "interleave")

        if blend_mode == "blit":
            if not self._ensure_fb():
                self._svc["pending"] = None
                return

            if group != self._fb_group:
                self._flush_blit(wv)
                self._reset_fb()
                self._fb_group = group
                if not self._ensure_fb():
                    self._svc["pending"] = None
                    return

            try:
                import framebuf
                layer_fb = framebuf.FrameBuffer(payload, w, h, framebuf.RGB565)
                label_id = int(pending.get("label_id", 0) or 0)
                dp = bus.get_service("dp_manager")
                layout = (dp.get("layout") or []) if dp else []
                key = -1
                if label_id < len(layout):
                    key = int(layout[label_id].get("key", -1))
                self._fb.blit(layer_fb, x, y, key)
            except Exception as e:
                self._svc["last_err"] = "blit: " + str(e)
                self._svc["last_ms"] = time.ticks_ms()
                self._svc["pending"] = None
                return

            self._svc["pending"] = None
            self._svc["last_err"] = ""
            self._svc["last_ms"] = time.ticks_ms()
            self._tick_fps()
            return

        payload = wv[HDR_OUT : HDR_OUT + payload_len]

        hook = self._svc.get("hook", None)
        if bool(self._svc.get("hook_enable", False)) and hook is not None:
            info = dict(pending)
            info["pixel_format"] = str(self._svc.get("pixel_format", "RGB565_BE"))
            try:
                res = hook(payload, info)
                if res is not None:
                    if int(len(res)) != payload_len:
                        raise ValueError("hook payload length mismatch")
                    payload[:] = memoryview(res)[:payload_len]
            except Exception as e:
                self._svc["last_err"] = str(e)
                self._svc["last_ms"] = time.ticks_ms()
                self._svc["pending"] = None
                return

        pack_out_header(
            wv,
            payload_len,
            seq=int(pending.get("seq", 0) or 0),
            label_id=int(pending.get("label_id", 0) or 0),
            x=int(pending.get("x", 0) or 0),
            y=int(pending.get("y", 0) or 0),
            w=int(pending.get("w", 0) or 0),
            h=int(pending.get("h", 0) or 0),
            flags=3,
            fmt_code=int(pending.get("fmt_code", 0) or 0),
        )
        hub.commit()

        self._svc["pending"] = None
        self._svc["frames"] = int(self._svc.get("frames", 0) or 0) + 1
        self._svc["last_done"] = {"seq": int(pending.get("seq", 0) or 0), "ms": time.ticks_ms()}
        self._svc["last_err"] = ""
        self._svc["last_ms"] = time.ticks_ms()

        self._tick_fps()

    def _flush_blit(self, wv):
        if self._fb_buf is None or self._fb_group < 0:
            return
        w = int(bus.shared.get("tft_width", 240) or 240)
        h = int(bus.shared.get("tft_height", 320) or 320)
        bpp = int((self._svc.get("pixel_format") or "RGB565_BE").startswith("RGB888") and 3 or 2)
        frame_bytes = w * h * bpp
        payload = wv[HDR_OUT : HDR_OUT + frame_bytes]
        payload[:] = memoryview(self._fb_buf)[:frame_bytes]
        pack_out_header(wv, frame_bytes, seq=0, label_id=0, x=0, y=0, w=w, h=h, flags=3, fmt_code=0)
        hub = self._svc.get("out_hub")
        if hub:
            hub.commit()
        self._svc["frames"] = int(self._svc.get("frames", 0) or 0) + 1
        self._svc["last_done"] = {"ms": time.ticks_ms()}
        self._svc["last_ms"] = time.ticks_ms()

    def on_stop(self):
        super().on_stop()
        self._fb_buf = None
        self._fb = None
        self._fb_group = -1
