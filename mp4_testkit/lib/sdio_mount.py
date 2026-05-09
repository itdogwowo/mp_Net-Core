import os


def _exists(p):
    try:
        os.stat(p)
        return True
    except Exception:
        return False


def mount_from_config(cfg):
    sd_cfg = cfg.get("SDcard", None)
    if not isinstance(sd_cfg, dict):
        return ""
    if not bool(sd_cfg.get("enable", False)):
        return ""

    path = sd_cfg.get("phat", None) or sd_cfg.get("path", None) or sd_cfg.get("mount", None) or "/sd"
    if not isinstance(path, str) or not path:
        path = "/sd"
    if _exists(path):
        return path

    try:
        import machine
    except Exception as e:
        print("SD mount: machine import failed:", e)
        return ""

    try:
        c = sd_cfg.get("config", {}) or {}
        g = sd_cfg.get("GPIO", {}) or {}

        sd = machine.SDCard(
            slot=int(c.get("slot", 2)),
            width=int(c.get("width", 4)),
            sck=int(g.get("sck")),
            cmd=int(g.get("cmd")),
            data=tuple(int(x) for x in (g.get("data") or ())),
            freq=int(c.get("freq", 20_000_000)),
        )
        os.mount(sd, path)
        return path
    except Exception as e:
        print("SD card init error:", e)
        return ""
