import time

from lib.task import Task
from lib.sys_bus import bus
from lib.fs_manager import fs
from lib.log_service import get_log
from lib.dp_manager_service import (
    HDR_IN,
    ensure_dp_manager_service,
    load_dp_config,
    configure_from_dp_config,
    pack_in_header,
)


def _join(a, b):
    a = str(a or "")
    b = str(b or "")
    if not a:
        return b
    if not b:
        return a
    if a.endswith("/"):
        return a + b.lstrip("/")
    return a + "/" + b.lstrip("/")


def _fmt_frame(fmt, frame_idx):
    try:
        return str(fmt).format(frame=int(frame_idx), i=int(frame_idx), index=int(frame_idx))
    except Exception:
        return "{:03d}.jpeg".format(int(frame_idx))


def _yield():
    try:
        time.sleep_ms(0)
    except Exception:
        return


def _read_file_into(path, dst, max_len, chunk):
    if chunk <= 0:
        with open(path, "rb") as f:
            n = f.readinto(dst[:max_len])
        return 0 if n is None else int(n)

    mv = dst[:max_len]
    off = 0
    with open(path, "rb") as f:
        while off < max_len:
            n = f.readinto(mv[off: off + chunk])
            if not n:
                break
            off += int(n)
            _yield()
    return int(off)


class DpManagerTask(Task):
    log_schema = ["jpeg_in_fill"]

    def on_start(self):
        super().on_start()
        self._svc = ensure_dp_manager_service(bus)
        self._seen_epoch = int(self._svc.get("cfg_epoch", 0) or 0)
        self._last_scan_count = None
        self._last_log_ms = 0
        self._last_group = -1

    def _ensure_loaded(self):
        cfg_path = str(self._svc.get("dp_config_path") or "/dp_config.json")
        now = time.ticks_ms()
        scan_n = None
        try:
            m = getattr(fs, "manifest", None) or {}
            scan_n = int(len(m))
        except Exception:
            scan_n = None
        if self._last_scan_count != scan_n:
            self._last_scan_count = scan_n

        epoch = int(self._svc.get("cfg_epoch", 0) or 0)
        if self._seen_epoch == epoch and self._svc.get("jpeg_in") is not None and (self._svc.get("schedule") or []):
            return True
        try:
            dp = load_dp_config(cfg_path)
            configure_from_dp_config(bus, dp, dp_config_path=cfg_path, service_name="dp_manager")
            self._svc = bus.get_service("dp_manager") or self._svc
            self._seen_epoch = int(self._svc.get("cfg_epoch", 0) or 0)
            self._last_group = -1
            try:
                self._svc["last_loaded"] = {"path": cfg_path, "ms": now}
            except Exception:
                pass
            return True
        except Exception as e:
            self._svc["last_err"] = str(e)
            self._svc["last_ms"] = now
            return False

    def _fill_hub(self):
        src = self._svc
        hub = src.get("jpeg_in")
        schedule = src.get("schedule") or []
        if hub is None or not schedule:
            return 0

        io_bufs = int(bus.shared.get("pipeline_io_buffers", 3) or 3)
        io_prefetch = io_bufs - 2
        if io_prefetch < 1:
            io_prefetch = 1
        if io_prefetch > io_bufs:
            io_prefetch = io_bufs
        try:
            if int(hub.get_fill_level() or 0) >= io_prefetch:
                return 0
        except Exception:
            pass

        filled = 0
        while filled < 3:
            try:
                if int(hub.get_fill_level() or 0) >= io_prefetch:
                    break
            except Exception:
                pass
            wv = hub.get_write_view()
            if wv is None:
                break
            cap = int(len(wv)) - HDR_IN
            if cap <= 0:
                break

            i = int(src.get("sch_i", 0) or 0)
            if i < 0 or i >= len(schedule):
                i = 0

            job = schedule[i]
            pack = job.get("pack_source")

            if pack is not None:
                group = int(job.get("frame_group", 0) or 0)
                label_id = int(job.get("label_id", 0) or 0)
                x = int(job.get("x", 0) or 0)
                y = int(job.get("y", 0) or 0)
                w = int(job.get("w", 0) or 0)
                h = int(job.get("h", 0) or 0)
                bpp = int(job.get("bpp", 2) or 2)
                try:
                    _idx, n, _dt = pack.read_next_into(wv[HDR_IN:], cap)
                except Exception:
                    _idx, n, _dt = None, 0, 0
                if _idx is None:
                    src["enable"] = False
                    bus.shared["jpeg_player"] = {"playing": False, "paused": False}
                    src["sch_i"] = 0
                    src["last_ms"] = time.ticks_ms()
                    self._last_group = -1
                    get_log().info("⏹ [DP] Pack ended")
                    return filled
                n = int(n or 0)
                if n <= 0:
                    break
                seq = int(src.get("seq", 1) or 1)
                pack_in_header(wv, n, seq=seq, label_id=label_id, x=x, y=y, w=w, h=h, bpp=bpp, flags=group, path_hash=int(_idx or 0))
                hub.commit()
                self.success += 1
                src["seq"] = (seq + 1) & 0xFFFF
                filled += 1
                next_i = i + 1
                if next_i >= len(schedule):
                    if not self.fcache_get("jpeg_loop", True):
                        src["enable"] = False
                        return filled
                    next_i = 0
                    self._last_group = -1
                src["sch_i"] = next_i
                self._last_group = group
                continue
            break

        if filled > 0:
            src["last_err"] = ""
            src["last_ms"] = time.ticks_ms()
        return filled

    def loop(self):
        if not self.running:
            return
        self._svc = bus.get_service("dp_manager") or self._svc
        if not self._svc or not self._svc.get("enable", True):
            return

        if not self._ensure_loaded():
            return

        hub = self._svc.get("jpeg_in")
        schedule = self._svc.get("schedule") or []
        if hub is None or not schedule:
            return
        try:
            self._lw_ex(0, int(hub.get_fill_level() or 0) + 1)
        except Exception:
            pass

        if self._fill_hub() > 0:
            return

        src = self._svc
        wv = hub.get_write_view()
        if wv is None:
            return
        cap = int(len(wv)) - HDR_IN
        if cap <= 0:
            return

        i = int(src.get("sch_i", 0) or 0)
        if i < 0 or i >= len(schedule):
            i = 0
        job = schedule[i]
        pack = job.get("pack_source")
        group = int(job.get("frame_group", 0) or 0)

        if pack is not None:
            label_id = int(job.get("label_id", 0) or 0)
            x = int(job.get("x", 0) or 0)
            y = int(job.get("y", 0) or 0)
            w = int(job.get("w", 0) or 0)
            h = int(job.get("h", 0) or 0)
            bpp = int(job.get("bpp", 2) or 2)
            try:
                _idx, n, _dt = pack.read_next_into(wv[HDR_IN:], cap)
            except Exception:
                _idx, n, _dt = None, 0, 0
            if _idx is None:
                src["enable"] = False
                bus.shared["jpeg_player"] = {"playing": False, "paused": False}
                src["sch_i"] = 0
                src["last_ms"] = time.ticks_ms()
                self._last_group = -1
                get_log().info("⏹ [DP] Pack ended")
                return
            n = int(n or 0)
            if n <= 0:
                return
            seq = int(src.get("seq", 1) or 1)
            pack_in_header(wv, n, seq=seq, label_id=label_id, x=x, y=y, w=w, h=h, bpp=bpp, flags=group, path_hash=int(_idx or 0))
            hub.commit()
            self.success += 1
            src["seq"] = (seq + 1) & 0xFFFF

            next_i = i + 1
            if next_i >= len(schedule):
                if not self.fcache_get("jpeg_loop", True):
                    src["enable"] = False
                    bus.shared["jpeg_player"] = {"playing": False, "paused": False}
                    src["sch_i"] = 0
                    src["last_ms"] = time.ticks_ms()
                    self._last_group = -1
                    get_log().info("⏹ [DP] Loop disabled, playback ended")
                    return
                next_i = 0
                self._last_group = -1
            src["sch_i"] = next_i
            src["last_ms"] = time.ticks_ms()
            self._last_group = group
            src["last_err"] = ""
            return

        assets_root = str(src.get("assets_root") or "")
        label = str(job.get("label") or "")
        frame = int(job.get("frame", 0) or 0)
        x = int(job.get("x", 0) or 0)
        y = int(job.get("y", 0) or 0)
        w = int(job.get("w", 0) or 0)
        h = int(job.get("h", 0) or 0)
        bpp = int(job.get("bpp", 2) or 2)
        label_id = int(job.get("label_id", 0) or 0)

        fmt = str(src.get("frame_format") or "{frame:03d}.jpeg")
        filename = _fmt_frame(fmt, frame)
        path = _join(_join(assets_root, label), filename)

        n = 0
        try:
            chunk = int(bus.shared.get("dp_io_read_chunk", 2048) or 2048)
            if chunk < 0:
                chunk = 0
            n = _read_file_into(path, wv[HDR_IN:], cap, chunk)
        except Exception as e:
            now = time.ticks_ms()
            src["last_err"] = str(e)
            src["last_ms"] = now
            if time.ticks_diff(now, int(self._last_log_ms or 0)) > 1000:
                self._last_log_ms = now
                try:
                    get_log().warn("⚠️ [DP] load fail path={} err={}".format(path, e))
                except Exception:
                    pass
            return

        n = int(n or 0)
        if n <= 0:
            return

        seq = int(src.get("seq", 1) or 1)
        pack_in_header(wv, n, seq=seq, label_id=label_id, x=x, y=y, w=w, h=h, bpp=bpp, flags=group, path_hash=0)
        hub.commit()
        self.success += 1
        src["seq"] = (seq + 1) & 0xFFFF

        next_i = i + 1
        if next_i >= len(schedule):
            if not self.fcache_get("jpeg_loop", True):
                src["enable"] = False
                bus.shared["jpeg_player"] = {"playing": False, "paused": False}
                src["sch_i"] = 0
                src["last_ms"] = time.ticks_ms()
                self._last_group = -1
                get_log().info("⏹ [DP] Loop disabled, playback ended")
                return
            next_i = 0
            self._last_group = -1
        src["sch_i"] = next_i
        src["last_ms"] = time.ticks_ms()
        self._last_group = group
        src["last_err"] = ""
