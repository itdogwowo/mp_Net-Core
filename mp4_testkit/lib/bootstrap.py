import jpeg
from machine import Pin, SPI
import gc
import os

from lib.buffer_hub import AtomicStreamHub
from lib.config_loader import load_config
from lib.media_source import compute_max_file_size, compute_max_frame_size, list_jpegs
from lib.pack_source import PackSource
from lib.sdio_mount import mount_from_config
from lib.sys_bus import SysBus


def _parse_pixel_format(raw):
    s = "" if raw is None else str(raw).strip()
    if not s:
        s = "RGB565_BE"
    tft_order = None
    if ":" in s:
        base, tail = s.split(":", 1)
        s = base.strip()
        tail = tail.strip().upper()
        if tail:
            tft_order = tail
    return s, tft_order


def _pack_candidates(assets_root, folder, raw):
    if raw is None:
        return []
    if isinstance(raw, bool):
        raw = 1 if raw else 0
    if isinstance(raw, int):
        if int(raw) == 1:
            return [assets_root + "/" + folder + ".jpk"]
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if not s.startswith("/"):
            s = assets_root + "/" + s
        return [s]
    return []


def build_bus():
    cfg = load_config()
    player_cfg = cfg.get("player", {}) or {}
    debug = bool(cfg.get("debug", False) or player_cfg.get("debug", False))
    if debug:
        print("[Config]", cfg.get("_config_path", "?"))

    sd_mount = mount_from_config(cfg)
    if debug and sd_mount:
        print("[SD]", sd_mount)

    assets_root = (cfg.get("assets_root", "/jpeg") or "/jpeg").rstrip("/")
    if assets_root in ("/sd", "/sdcard", "/SD", "/SDCARD"):
        if sd_mount:
            assets_root = sd_mount.rstrip("/")
        else:
            assets_root = "/jpeg"
            if debug:
                print("[SD] not mounted, fallback assets_root=/jpeg")
    print('assets_root : ',assets_root)
    tft_cfg = cfg.get("tft", {}) or {}
    jpeg_cfg = cfg.get("jpeg", {}) or {}
    layout = (cfg.get("display_Layout") or [{}])[0] or {}
    assets_pack = layout.get("assets_pack", None)
    if assets_pack is None:
        assets_pack = cfg.get("assets_pack", None)

    width = int(tft_cfg.get("width", layout.get("width", 240)))
    height = int(tft_cfg.get("height", layout.get("height", 240)))
    folder = layout.get("type", "background")
    folder_path = assets_root + "/" + folder
    depth_val = layout.get("depth", -1)
    depth = -1 if depth_val is None else int(depth_val)

    pixel_format, tft_order = _parse_pixel_format(jpeg_cfg.get("pixel_format", "RGB565_BE"))
    rotation = int(jpeg_cfg.get("rotation", 0))
    block = bool(jpeg_cfg.get("block", True))
    return_bytes = bool(jpeg_cfg.get("return_bytes", False))
    step_blocks = int(jpeg_cfg.get("step_blocks", 0) or 0)
    max_jpeg_bytes = int(jpeg_cfg.get("max_jpeg_bytes", 0) or 0)

    pace_ms = int(player_cfg.get("pace_ms", 0) or 0)
    pace_frames = int(player_cfg.get("pace_frames", 1) or 1)
    if pace_frames < 1:
        pace_frames = 1
    loop_play = bool(player_cfg.get("loop", True))
    pipeline_cfg = player_cfg.get("pipeline", {}) or {}
    pipeline_io_buffers = pipeline_cfg.get("io_buffers", None)
    pipeline_frame_buffers = pipeline_cfg.get("frame_buffers", None)
    pipeline_io_prefetch = pipeline_cfg.get("io_prefetch", None)
    pipeline_io_read_chunk = pipeline_cfg.get("io_read_chunk", None)
    pipeline_preload = pipeline_cfg.get("preload", None)
    pipeline_preload_limit = pipeline_cfg.get("preload_limit_bytes", None)
    io_buffers = None if pipeline_io_buffers is None else int(pipeline_io_buffers)
    frame_buffers = None if pipeline_frame_buffers is None else int(pipeline_frame_buffers)
    stats_cfg = player_cfg.get("stats", {}) or {}
    stats_enabled = bool(stats_cfg.get("enabled", False))
    stats_interval_ms = int(stats_cfg.get("interval_ms", 1000) or 1000)
    stats_frames_n = int(stats_cfg.get("frames_n", 60) or 60)

    if pixel_format in ("RGB565_BE", "RGB565", "RGB565_LE"):
        bytes_per_pixel = 2
    elif pixel_format in ("RGB888", "RGB888_BE", "RGB888_LE"):
        bytes_per_pixel = 3
    else:
        raise ValueError("Unsupported jpeg.pixel_format: {}".format(pixel_format))

    try:
        decoder = jpeg.Decoder(
            pixel_format=pixel_format,
            rotation=rotation,
            block=block,
            return_bytes=return_bytes,
        )
    except Exception as e:
        raise ValueError("jpeg.Decoder does not support pixel_format={}".format(pixel_format)) from e

    cache = None
    pack = None
    pack_candidates = _pack_candidates(assets_root, folder, assets_pack)

    for cand in pack_candidates:
        try:
            os.stat(cand)
        except Exception:
            continue
        try:
            pack = PackSource(cand, loop=loop_play)
            print("[Pack] using:", cand, "count:", pack.count, "max_size:", pack.max_size)
            if max_jpeg_bytes <= 0:
                max_jpeg_bytes = int(pack.max_size)
            paths = []
            break
        except Exception as e:
            pack = None
            print("[Pack] unavailable:", cand, "-> fallback folder. err:", e)
            if isinstance(assets_pack, str) and assets_pack:
                break

    if pack is None:
        try:
            paths = list_jpegs(folder_path)
        except OSError as e:
            raise OSError(
                "Assets folder not found: {} (assets_root={}, type={}). "
                "If using SD, ensure SDcard.enable is true and the folder exists. "
                "Or set assets_pack to a .jpk file.".format(folder_path, assets_root, folder)
            ) from e
        if not paths:
            raise OSError("No JPEG files in: " + folder_path)
        if depth > 0 and depth < len(paths):
            paths = paths[:depth]

        if max_jpeg_bytes <= 0:
            max_jpeg_bytes = compute_max_file_size(paths)

    if pack is None:
        preload_cfg = pipeline_preload
        preload_units = None
        preload_enabled = False
        if preload_cfg is None:
            preload_enabled = True
        elif isinstance(preload_cfg, bool):
            preload_enabled = bool(preload_cfg)
        else:
            try:
                preload_units = int(preload_cfg)
                preload_enabled = preload_units > 0
            except Exception:
                preload_enabled = False

    if pack is None and preload_enabled:
        if preload_units is not None:
            limit = int(max_jpeg_bytes) * int(preload_units)
        else:
            if pipeline_preload_limit is None:
                req_limit = -1
            else:
                req_limit = int(pipeline_preload_limit)
            if req_limit < 0:
                tmp_frame_hub_buffers = 3 if frame_buffers is None else frame_buffers
                tmp_io_hub_buffers = tmp_frame_hub_buffers if io_buffers is None else io_buffers
                mf = 0
                try:
                    mf = int(gc.mem_free())
                except Exception:
                    mf = 0
                cap = (mf * 25) // 100 if mf > 0 else 0
                target = int(max_jpeg_bytes) * int(tmp_io_hub_buffers) * 16
                limit = target if cap <= 0 else (cap if target > cap else target)
            else:
                limit = req_limit
        if limit < 0:
            limit = 0
        total = 0
        cache = []
        for i, p in enumerate(paths):
            sz = int(os.stat(p)[6])
            if sz <= 0:
                continue
            if limit and (total + sz) > limit:
                break
            b = bytearray(sz)
            with open(p, "rb") as f:
                n = f.readinto(b)
            if n is None:
                n = 0
            cache.append((i, memoryview(b), n))
            total += sz
            gc.collect()
        if not cache:
            cache = None
        if debug:
            print("[Preload] frames:", 0 if cache is None else len(cache), "bytes:", total)

    spi_cfg = tft_cfg.get("spi", {}) or {}
    pins_cfg = tft_cfg.get("pins", {}) or {}

    spi_id = int(spi_cfg.get("id", 1))
    spi_baudrate = int(spi_cfg.get("baudrate", 80_000_000))
    spi_sck = int(spi_cfg.get("sck", 8))
    spi_mosi = int(spi_cfg.get("mosi", 7))

    dc_pin = int(pins_cfg.get("dc", 13))
    cs_pin = int(pins_cfg.get("cs", 10))
    rst_pin = int(pins_cfg.get("rst", 14))

    tft_spi = SPI(spi_id, baudrate=spi_baudrate, sck=Pin(spi_sck), mosi=Pin(spi_mosi))

    driver_name = tft_cfg.get("driver", "GC9A01")
    disp_rotation = int(tft_cfg.get("rotation", 0))
    if tft_order is not None:
        color_order = tft_order
    else:
        color_order = tft_cfg.get("color_order", "RGB")
    invert = bool(tft_cfg.get("invert", True))

    tft_mod = __import__("lib.TFT", None, None, ["*"])
    driver_cls = getattr(tft_mod, driver_name)

    lcd = driver_cls(
        spi=tft_spi,
        dc=Pin(dc_pin, Pin.OUT),
        cs=Pin(cs_pin, Pin.OUT),
        rst=Pin(rst_pin, Pin.OUT),
        width=width,
        height=height,
        rotation=disp_rotation,
        color_order=color_order,
        invert=invert,
        pixel_format=pixel_format,
        bytes_per_pixel=bytes_per_pixel,
    )
    lcd.set_window(0, 0)

    bus = SysBus()
    bus.shared["config"] = cfg
    bus.shared["debug"] = debug
    bus.shared["width"] = width
    bus.shared["height"] = height
    bus.shared["frame_bytes"] = compute_max_frame_size(
        paths,
        default_bytes=width * height,
        bytes_per_pixel=bytes_per_pixel,
    )
    bus.shared["max_jpeg_bytes"] = max_jpeg_bytes
    bus.shared["jpeg_block"] = block
    bus.shared["jpeg_step_blocks"] = step_blocks
    bus.shared["pace_ms"] = pace_ms
    bus.shared["pace_frames"] = pace_frames
    bus.shared["loop_play"] = loop_play
    bus.shared["pipeline_io_buffers"] = io_buffers
    bus.shared["pipeline_frame_buffers"] = frame_buffers
    bus.shared["stats_enabled"] = stats_enabled
    bus.shared["stats_interval_ms"] = stats_interval_ms
    bus.shared["stats_frames_n"] = stats_frames_n
    bus.shared["engine_run"] = True
    bus.shared["core1_ready"] = False

    bus.set_service("lcd", lcd)
    bus.set_service("decoder", decoder)
    bus.set_service("paths", paths)
    if pack is not None:
        bus.set_service("pack", pack)
    if cache is not None:
        bus.set_service("jpeg_cache", cache)
        bus.shared["cache_active"] = True
        bus.shared["src_idx"] = len(cache)
    else:
        bus.shared["cache_active"] = False
        bus.shared["src_idx"] = 0

    frame_tail = 16
    io_tail = 16 + 16
    bus.shared["frame_tail"] = frame_tail
    bus.shared["io_tail"] = io_tail

    frame_hub_buffers = 3 if frame_buffers is None else frame_buffers
    io_hub_buffers = frame_hub_buffers if io_buffers is None else io_buffers

    frame_hub = AtomicStreamHub(bus.shared["frame_bytes"] + frame_tail, num_buffers=frame_hub_buffers)
    io_hub = AtomicStreamHub(max_jpeg_bytes + io_tail, num_buffers=io_hub_buffers)

    bus.set_service("frame_hub", frame_hub)
    bus.set_service("io_hub", io_hub)

    # Folder I/O jitter is higher than pack; keep queue fuller by default.
    default_prefetch = -1 if pack is None else -2
    raw_prefetch = default_prefetch if pipeline_io_prefetch is None else int(pipeline_io_prefetch)
    if raw_prefetch < 0:
        io_prefetch = io_hub_buffers + raw_prefetch
        if pipeline_io_prefetch is None and io_prefetch < 1 and io_hub_buffers > 0:
            io_prefetch = 1
    else:
        io_prefetch = raw_prefetch
    if io_prefetch > io_hub_buffers:
        io_prefetch = io_hub_buffers
    bus.shared["io_prefetch"] = io_prefetch

    io_read_chunk = 0 if pipeline_io_read_chunk is None else int(pipeline_io_read_chunk)
    if io_read_chunk < 0:
        io_read_chunk = 0
    bus.shared["io_read_chunk"] = io_read_chunk

    return bus
