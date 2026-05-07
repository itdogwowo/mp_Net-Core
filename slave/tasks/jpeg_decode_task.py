import time

from lib.task import Task
from lib.sys_bus import bus
from lib.dp_manager_service import HDR_IN, unpack_in_header
from lib.dp_buffer_service import HDR_OUT, ensure_dp_buffer_service, configure_for_layout

_JOB_HUB = 0
_JOB_RV = 1
_JOB_JPEG = 2
_JOB_PAYLOAD = 3
_JOB_SEQ = 4
_JOB_LABEL = 5
_JOB_X = 6
_JOB_Y = 7
_JOB_W = 8
_JOB_H = 9
_JOB_BPP = 10
_JOB_FLAGS = 11
_JOB_PATH = 12
_JOB_FMT = 13

_PEND_SEQ = 0
_PEND_LABEL = 1
_PEND_X = 2
_PEND_Y = 3
_PEND_W = 4
_PEND_H = 5
_PEND_BPP = 6
_PEND_PAYLOAD = 7
_PEND_FRAME_GROUP = 8
_PEND_FMT = 9


class JpegDecodeTask(Task):
    def on_start(self):
        super().on_start()
        self._dp = None
        self._buf = ensure_dp_buffer_service(bus)
        self._decoder = None
        self._job = None
        self._last_idle_log_ms = 0
        self._seen_epoch = None
        self._pending_list = [0] * 10

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
        if self._seen_epoch == epoch and self._buf.get("out_hub") is not None:
            return True
        self._seen_epoch = epoch
        try:
            frame_bufs = int(bus.shared.get("pipeline_frame_buffers", 3) or 3)
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
            payload_len, seq, label_id, x, y, w, h, bpp, flags, path_hash = unpack_in_header(rv)
            payload_len = int(payload_len)
            if payload_len <= 0:
                hub.release_read()
                return None
            jpeg_data = rv[HDR_IN : HDR_IN + payload_len]
            self._job = [
                hub,
                rv,
                jpeg_data,
                payload_len,
                int(seq),
                int(label_id),
                int(x),
                int(y),
                int(w),
                int(h),
                int(bpp),
                int(flags),
                int(path_hash),
                0,
            ]
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

        out_hub = self._buf.get("out_hub")
        if out_hub is None:
            return

        if not self._ensure_decoder():
            return

        if self._buf.get("pending") is not None:
            return

        if self._job is None:
            self._pick_job(dp)
            if self._job is None:
                return

        job = self._job
        w = int(job[_JOB_W])
        h = int(job[_JOB_H])
        bpp = int(job[_JOB_BPP])
        frame_bytes = w * h * bpp

        wv = out_hub.get_write_view()
        if wv is None:
            return
        if int(len(wv)) < HDR_OUT + frame_bytes:
            self._buf["last_err"] = "out buffer too small"
            self._buf["last_ms"] = time.ticks_ms()
            return

        fb = wv[HDR_OUT : HDR_OUT + frame_bytes]
        step_blocks = int((dp.get("jpeg") or {}).get("step_blocks", 1) or 1)
        if step_blocks < 0:
            step_blocks = 0

        try:
            done = bool(self._decoder.decode_into(job[_JOB_JPEG], fb, blocks=step_blocks))
        except Exception as e:
            self._buf["last_err"] = str(e)
            self._buf["last_ms"] = time.ticks_ms()
            try:
                if job[_JOB_HUB] is not None:
                    job[_JOB_HUB].release_read()
            except Exception:
                pass
            self._job = None
            return

        if not done:
            return

        pl = self._pending_list
        pl[_PEND_SEQ] = int(job[_JOB_SEQ])
        pl[_PEND_LABEL] = int(job[_JOB_LABEL])
        pl[_PEND_X] = int(job[_JOB_X])
        pl[_PEND_Y] = int(job[_JOB_Y])
        pl[_PEND_W] = int(job[_JOB_W])
        pl[_PEND_H] = int(job[_JOB_H])
        pl[_PEND_BPP] = int(job[_JOB_BPP])
        pl[_PEND_PAYLOAD] = int(frame_bytes)
        pl[_PEND_FRAME_GROUP] = int(job[_JOB_FLAGS])
        pl[_PEND_FMT] = int(job[_JOB_FMT])

        self._buf["pending"] = {
            "seq": pl[_PEND_SEQ],
            "label_id": pl[_PEND_LABEL],
            "x": pl[_PEND_X],
            "y": pl[_PEND_Y],
            "w": pl[_PEND_W],
            "h": pl[_PEND_H],
            "bpp": pl[_PEND_BPP],
            "payload_len": pl[_PEND_PAYLOAD],
            "frame_group": pl[_PEND_FRAME_GROUP],
            "fmt_code": pl[_PEND_FMT],
        }
        self._buf["last_ms"] = time.ticks_ms()
        self._buf["last_err"] = ""

        try:
            if job[_JOB_HUB] is not None:
                job[_JOB_HUB].release_read()
        except Exception:
            pass
        self._job = None

