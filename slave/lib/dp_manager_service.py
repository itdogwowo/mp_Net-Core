try:
    import ujson as json
except Exception:
    import json

try:
    import ustruct as _struct
except Exception:
    import struct as _struct

from lib.buffer_hub import AtomicStreamHub


HDR_IN = 32
IN_FMT = "<IHHhhHHHHI"


def _pack_into(buf, offset, *args):
    _struct.pack_into(IN_FMT, buf, offset, *args)


def _unpack_from(buf, offset=0):
    return _struct.unpack_from(IN_FMT, buf, offset)


def _u32(buf, off):
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def _u16(buf, off):
    return buf[off] | (buf[off + 1] << 8)


def _i16(buf, off):
    v = buf[off] | (buf[off + 1] << 8)
    return v - 0x10000 if v >= 0x8000 else v


def unpack_in_header_into(buf, out):
    out[0] = _u32(buf, 0)
    out[1] = _u16(buf, 4)
    out[2] = _u16(buf, 6)
    out[3] = _i16(buf, 8)
    out[4] = _i16(buf, 10)
    out[5] = _u16(buf, 12)
    out[6] = _u16(buf, 14)
    out[7] = _u16(buf, 16)
    out[8] = _u16(buf, 18)
    out[9] = _u32(buf, 20)


def ensure_dp_manager_service(bus, name="dp_manager"):
    svc = bus.get_service(name)
    if svc is not None:
        return svc


    svc = {
        "api": 1,
        "enable": True,
        "dp_config_path": "/dp_config.json",
        "assets_root": "",
        "frame_format": "{frame:03d}.jpeg",
        "jpeg": {"pixel_format": "RGB565_BE", "rotation": 0, "block": True, "step_blocks": 1, "max_jpeg_bytes": 49152},
        "layout": [],
        "schedule": [],
        "sch_i": 0,
        "seq": 1,
        "cfg_epoch": 0,
        "jpeg_in": None,
        "inflight": 0,
        "last_err": "",
        "last_ms": 0,
        "last_loaded": None,
    }
    bus.register_service(name, svc)
    return svc


def _dir_name(path):
    if not path:
        return ""
    path = str(path)
    i = path.rfind("/")
    if i < 0:
        return ""
    return path[:i]


def _norm_path(p):
    if not p:
        return ""
    p = str(p)
    if p.endswith("/"):
        return p[:-1]
    return p


def _extract_layout(dp):
    layout = dp.get("display_Layout") or dp.get("layout") or []
    if not isinstance(layout, list):
        return []
    return layout


def _label_of_item(item):
    if not isinstance(item, dict):
        return ""
    return str(item.get("label") or item.get("type") or "")


def _rect_of_item(item):
    if not isinstance(item, dict):
        return 0, 0, 0, 0, 1
    x = int(item.get("x", 0) or 0)
    y = int(item.get("y", 0) or 0)
    w = int(item.get("width", item.get("w", item.get("W", 0) or 0)) or 0)
    h = int(item.get("height", item.get("h", item.get("H", 0) or 0)) or 0)
    depth = int(item.get("depth", 1) or 1)
    if depth <= 0:
        depth = 1
    return x, y, w, h, depth


def _bpp(pixel_format):
    pf = str(pixel_format or "")
    if pf.startswith("RGB565") or pf == "CbYCrY":
        return 2
    if pf == "RGB888":
        return 3
    return 2


def load_dp_config(path):
    with open(path, "r") as f:
        return json.loads(f.read())


def configure_from_dp_config(bus, dp, *, dp_config_path=None, service_name="dp_manager"):
    svc = ensure_dp_manager_service(bus, name=service_name)

    dp = dp if isinstance(dp, dict) else {}
    dp_jpeg = dp.get("jpeg") if isinstance(dp.get("jpeg"), dict) else {}

    assets_root = dp.get("assets_root") or dp.get("root_path") or ""
    if not assets_root and dp_config_path:
        assets_root = _dir_name(dp_config_path)
    assets_root = _norm_path(assets_root)

    frame_format = dp.get("frame_format") or dp.get("jpeg_frame_format") or svc.get("frame_format") or "{frame:03d}.jpeg"

    jpeg_cfg = dict(svc.get("jpeg") or {})
    for k in ("pixel_format", "rotation", "block", "step_blocks", "max_jpeg_bytes"):
        if k in dp_jpeg:
            jpeg_cfg[k] = dp_jpeg.get(k)

    pixel_format = jpeg_cfg.get("pixel_format") or "RGB565_LE"
    bpp = _bpp(pixel_format)

    player_cfg = dp.get("player") if isinstance(dp.get("player"), dict) else {}
    loop_play = bool(player_cfg.get("loop", True))
    blend_mode = str(player_cfg.get("blend_mode", "interleave") or "interleave")
    if blend_mode not in ("interleave", "blit"):
        blend_mode = "interleave"

    pipeline_cfg = player_cfg.get("pipeline") if isinstance(player_cfg.get("pipeline"), dict) else {}
    io_bufs = int(pipeline_cfg.get("io_buffers", 3) or 3)
    frame_bufs = int(pipeline_cfg.get("frame_buffers", 3) or 3)
    if io_bufs < 1:
        io_bufs = 3
    if io_bufs > 6:
        io_bufs = 6
    if frame_bufs < 1:
        frame_bufs = 3
    if frame_bufs > 4:
        frame_bufs = 4
    bus.shared["pipeline_io_buffers"] = io_bufs
    bus.shared["pipeline_frame_buffers"] = frame_bufs

    layout = _extract_layout(dp)
    items = []
    max_jpeg_bytes = int(jpeg_cfg.get("max_jpeg_bytes", 0) or 0)

    for it in layout:
        label = _label_of_item(it)
        if not label:
            continue
        x, y, w, h, depth = _rect_of_item(it)
        if int(w) <= 0 or int(h) <= 0:
            continue

        level = int(it.get("level", 0) or 0)

        item = {
            "label": label,
            "level": level,
            "x": int(x),
            "y": int(y),
            "w": int(w),
            "h": int(h),
            "depth": int(depth),
            "bpp": int(bpp),
            "key": int(it.get("blit_key", -1) or -1),
        }

        use_pack = bool(it.get("assets_pack", 0))
        item["assets_pack"] = 1 if use_pack else 0
        if use_pack:
            pack_path = assets_root + "/" + label + ".jpk"
            try:
                import os as _os
                from lib.pack_source import PackSource
                _os.stat(pack_path)
                pack = PackSource(pack_path, loop=loop_play)
                print("[Pack] using:", pack_path, "count:", pack.count, "max_size:", pack.max_size)
                item["pack_source"] = pack
                item["depth"] = int(pack.count)
                if max_jpeg_bytes <= 0:
                    max_jpeg_bytes = int(pack.max_size)
            except Exception as e:
                print("[Pack] unavailable:", pack_path, "-> fallback folder. err:", e)
                item["pack_source"] = None
                if depth <= 0:
                    item["depth"] = 1
        else:
            item["pack_source"] = None

        items.append(item)

    items.sort(key=lambda it: it["level"])

    if max_jpeg_bytes <= 0:
        max_jpeg_bytes = 49152

    schedule = []
    max_frames = max((it["depth"] for it in items), default=0)
    for fi in range(max_frames):
        for label_i, it in enumerate(items):
            if fi < it["depth"]:
                schedule.append({
                    "label": it["label"],
                    "label_id": int(label_i),
                    "frame": int(fi),
                    "x": int(it["x"]),
                    "y": int(it["y"]),
                    "w": int(it["w"]),
                    "h": int(it["h"]),
                    "bpp": int(it["bpp"]),
                    "pack_source": it.get("pack_source"),
                    "frame_group": int(fi),
                })

    jpeg_in = AtomicStreamHub(HDR_IN + max_jpeg_bytes, num_buffers=io_bufs)

    svc["dp_config_path"] = str(dp_config_path or svc.get("dp_config_path") or "/dp_config.json")
    svc["assets_root"] = assets_root
    svc["frame_format"] = str(frame_format)
    svc["jpeg"] = jpeg_cfg
    svc["layout"] = items
    svc["schedule"] = schedule
    svc["sch_i"] = 0
    svc["jpeg_in"] = jpeg_in
    svc["inflight"] = 0
    svc["last_err"] = ""
    svc["cfg_epoch"] = (int(svc.get("cfg_epoch", 0) or 0) + 1) & 0xFFFF

    pace = int(player_cfg.get("pace_ms", 0) or 0)
    if pace > 0:
        bus.shared["jpeg_pace_ms"] = pace
    bus.shared["jpeg_loop"] = loop_play
    bus.shared["jpeg_blend_mode"] = blend_mode

    fps_cfg = player_cfg.get("fps_stats")
    if isinstance(fps_cfg, dict):
        fps_enable = bool(fps_cfg.get("enabled", True))
        fps_interval = int(fps_cfg.get("interval_ms", 1000) or 1000)
    else:
        fps_enable = True
        fps_interval = 1000
    bus.shared["fps_stats_enabled"] = fps_enable
    bus.shared["fps_stats_interval"] = fps_interval

    print("[DP] blend={} pace_ms={} loop={} fps_stats={} interval={}".format(
        blend_mode, pace, loop_play, fps_enable, fps_interval))
    return svc


def pack_in_header(buf, payload_len, *, seq=0, label_id=0, x=0, y=0, w=0, h=0, bpp=2, flags=0, path_hash=0):
    _pack_into(buf, 0, int(payload_len), int(seq), int(label_id), int(x), int(y), int(w), int(h), int(bpp), int(flags), int(path_hash))


def unpack_in_header(buf):
    return _unpack_from(buf, 0)
