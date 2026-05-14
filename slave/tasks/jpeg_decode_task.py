import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_manager_service import HDR_IN, unpack_in_header_into
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, configure_for_layout, pack_out_header


class JpegDecodeTask(Task):
    log_schema = ["jpeg_out_fill", "jpeg_in_fill"]

    def on_start(self):
        super().on_start()
        self._dp = None
        self._buf = ensure_dp_buffer_service(bus)
        self._decoder = None
        self._job = None
        self._last_idle_log_ms = 0
        self._seen_epoch = None
        self._in_hdr = [0] * 10

    def _resolve_dp(self):
        if self._dp is not None:
            return self._dp
        self._dp = bus.get_service("dp_manager")
        return self._dp

    def _ensure_decoder(self):
        if self._decoder is not None:
            return True
        try:
            import jpeg

            dp = self._resolve_dp()
            cfg = {} if dp is None else (dp.get("jpeg") or {})
            pixel_format = cfg.get("pixel_format") or "RGB565_BE"
            rotation = int(cfg.get("rotation", 0) or 0)
            block = bool(cfg.get("block", True))
            self._decoder = jpeg.Decoder(pixel_format=pixel_format, rotation=rotation, block=block, return_bytes=False)
            return True
        except Exception as e:
            try:
                bus.shared.setdefault("task_errors", {})["jpeg_decode"] = str(e)
            except Exception:
                pass
            return False

    def _ensure_buf_config(self, dp):
        epoch = int(dp.get("cfg_epoch", 0) or 0)
        if self._seen_epoch == epoch and self._buf.get("jpeg_out") is not None:
            return True
        self._seen_epoch = epoch
        try:
            frame_bufs = int(bus.shared.get("pipeline_frame_buffers", 3) or 3)
            try:
                tm = bus.shared.get("test_mode") or {}
                if tm.get("enabled") and tm.get("active") == "jpeg_decode":
                    sub = tm.get("jpeg_decode") or {}
                    frame_bufs = int(sub.get("frame_buffers") or frame_bufs)
            except Exception:
                pass
            configure_for_layout(bus, dp.get("layout") or [], pixel_format=(dp.get("jpeg") or {}).get("pixel_format") or "RGB565_BE", num_buffers=frame_bufs)
            self._buf = bus.get_service("dp_buffer") or self._buf
            return True
        except Exception as e:
            self._buf["last_err"] = str(e)
            self._buf["last_ms"] = time.ticks_ms()
            return False

    def _pick_job(self, dp):
        hub = dp.get("jpeg_in")
        if hub is None:
            return None
        rv = hub.get_read_view()
        if rv is None:
            return None
        try:
            unpack_in_header_into(rv, self._in_hdr)
            payload_len = int(self._in_hdr[0])
            if payload_len <= 0:
                hub.release_read()
                return None
            seq = int(self._in_hdr[1])
            label_id = int(self._in_hdr[2])
            x = int(self._in_hdr[3])
            y = int(self._in_hdr[4])
            w = int(self._in_hdr[5])
            h = int(self._in_hdr[6])
            bpp = int(self._in_hdr[7])
            flags = int(self._in_hdr[8])
            path_hash = int(self._in_hdr[9])
            jpeg_data = rv[HDR_IN : HDR_IN + payload_len]
            self._job = {
                "hub": hub,
                "rv": rv,
                "jpeg_data": jpeg_data,
                "payload_len": payload_len,
                "seq": seq,
                "label_id": label_id,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "bpp": bpp,
                "flags": flags,
                "path_hash": path_hash,
                "fmt_code": 0,
            }
            return self._job
        except Exception:
            try:
                hub.release_read()
            except Exception:
                pass
            return None

    def loop(self):
        if not self.running:
            return

        dp = self._resolve_dp()
        if dp is None or not dp.get("enable", True):
            return

        if not self._ensure_buf_config(dp):
            return

        jpeg_out = self._buf.get("jpeg_out")
        if jpeg_out is None:
            return
        try:
            self._lw_ex(0, int(jpeg_out.get_fill_level() or 0) + 1)
        except Exception:
            pass
        try:
            hub_in = dp.get("jpeg_in")
            if hub_in is not None:
                self._lw_ex(1, int(hub_in.get_fill_level() or 0) + 1)
        except Exception:
            pass

        if not self._ensure_decoder():
            return

        if self._job is None:
            self._pick_job(dp)
            if self._job is None:
                return

        job = self._job
        w = int(job["w"])
        h = int(job["h"])
        bpp = int(job["bpp"])
        frame_bytes = w * h * bpp

        wv = jpeg_out.get_write_view()
        if wv is None:
            try:
                if job["hub"] is not None:
                    job["hub"].release_read()
            except Exception:
                pass
            self._job = None
            return
        if int(len(wv)) < HDR_OUT + frame_bytes:
            self._buf["last_err"] = "jpeg_out buffer too small"
            self._buf["last_ms"] = time.ticks_ms()
            try:
                if job["hub"] is not None:
                    job["hub"].release_read()
            except Exception:
                pass
            self._job = None
            return

        fb = wv[HDR_OUT : HDR_OUT + frame_bytes]
        step_blocks = (dp.get("jpeg") or {}).get("step_blocks", 1)
        if step_blocks is None:
            step_blocks = 1
        step_blocks = int(step_blocks)
        if step_blocks < 0:
            step_blocks = 0

        try:
            done = bool(self._decoder.decode_into(job["jpeg_data"], fb, blocks=step_blocks))
        except Exception as e:
            self._buf["last_err"] = str(e)
            self._buf["last_ms"] = time.ticks_ms()
            try:
                if job["hub"] is not None:
                    job["hub"].release_read()
            except Exception:
                pass
            self._job = None
            return

        if not done:
            return

        pack_out_header(
            wv,
            frame_bytes,
            seq=int(job["seq"]),
            label_id=int(job["label_id"]),
            x=int(job["x"]),
            y=int(job["y"]),
            w=int(job["w"]),
            h=int(job["h"]),
            flags=int(job.get("flags", 0) or 0),
            fmt_code=int(job.get("fmt_code", 0) or 0),
        )
        jpeg_out.commit()

        self._buf["last_ms"] = time.ticks_ms()
        self._buf["last_err"] = ""

        try:
            if job["hub"] is not None:
                job["hub"].release_read()
        except Exception:
            pass
        self._job = None
        self.success += 1
