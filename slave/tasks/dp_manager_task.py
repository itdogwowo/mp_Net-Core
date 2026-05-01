import time

from lib.task import Task
from lib.sys_bus import bus
from lib.fs_manager import fs
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
        return f"{int(frame_idx):03d}.jpeg"


class DpManagerTask(Task):
    def on_start(self):
        super().on_start()
        self._svc = ensure_dp_manager_service(bus)
        self._seen_epoch = None
        self._last_scan_count = None
        self._last_log_ms = 0

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
            try:
                self._svc["last_loaded"] = {"path": cfg_path, "ms": now}
            except Exception:
                pass
            return True
        except Exception as e:
            self._svc["last_err"] = str(e)
            self._svc["last_ms"] = now
            return False

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

        if hub.get_fill_level() >= 1:
            return

        i = int(self._svc.get("sch_i", 0) or 0)
        if i < 0 or i >= len(schedule):
            i = 0

        job = schedule[i]
        assets_root = str(self._svc.get("assets_root") or "")
        label = str(job.get("label") or "")
        frame = int(job.get("frame", 0) or 0)
        x = int(job.get("x", 0) or 0)
        y = int(job.get("y", 0) or 0)
        w = int(job.get("w", 0) or 0)
        h = int(job.get("h", 0) or 0)
        bpp = int(job.get("bpp", 2) or 2)
        label_id = int(job.get("label_id", 0) or 0)

        fmt = str(self._svc.get("frame_format") or "{frame:03d}.jpeg")
        filename = _fmt_frame(fmt, frame)
        path = _join(_join(assets_root, label), filename)

        wv = hub.get_write_view()
        if wv is None:
            return

        cap = int(len(wv)) - HDR_IN
        if cap <= 0:
            return

        n = 0
        try:
            with open(path, "rb") as f:
                n = f.readinto(wv[HDR_IN : HDR_IN + cap])
                if n is None:
                    n = 0
                try:
                    extra = f.read(1)
                    if extra:
                        raise ValueError("jpeg too large")
                except Exception:
                    pass
        except Exception as e:
            now = time.ticks_ms()
            self._svc["last_err"] = str(e)
            self._svc["last_ms"] = now
            if time.ticks_diff(now, int(self._last_log_ms or 0)) > 1000:
                self._last_log_ms = now
                try:
                    print(f"⚠️ [DP] load fail path={path} err={e}")
                except Exception:
                    pass
            return

        n = int(n or 0)
        if n <= 0:
            return

        seq = int(self._svc.get("seq", 1) or 1)
        pack_in_header(wv, n, seq=seq, label_id=label_id, x=x, y=y, w=w, h=h, bpp=bpp, flags=0, path_hash=0)
        hub.commit()

        self._svc["seq"] = (seq + 1) & 0xFFFF
        self._svc["sch_i"] = (i + 1) % len(schedule)
        self._svc["last_ms"] = time.ticks_ms()
        self._svc["last_err"] = ""

