try:
    import ujson as json
except Exception:
    import json

from machine import Pin
from lib.sys_bus import bus


def load_dp_config(path="/dp_config.json"):
    candidates = [
        path,
        "./dp_config.json",
        "/sd/dp_config.json",
    ]
    for p in candidates:
        try:
            with open(p, "r") as f:
                return json.loads(f.read())
        except Exception:
            continue
    return {}


def init_lcd(dp_config_path="/dp_config.json"):
    cfg = load_dp_config(dp_config_path)

    tft_cfg = cfg.get("tft")
    if not tft_cfg:
        print("⚠️ [DPBoot] No tft config, skipping LCD init")
        return None

    width = int(tft_cfg.get("width", 240))
    height = int(tft_cfg.get("height", 320))

    spi_id = int(tft_cfg.get("spi", 2))
    spi_by_id = bus.get_service("spi_by_id") or {}
    tft_spi = spi_by_id.get(spi_id)
    if tft_spi is None:
        spi_list = bus.get_service("spi_list") or []
        for s in spi_list:
            if getattr(s, "id", None) == spi_id:
                tft_spi = s
                break
    if tft_spi is None:
        raise ValueError("SPI id={} not found in spi_list/spi_by_id".format(spi_id))

    pins_cfg = tft_cfg.get("pins") or {}
    dc_pin = int(pins_cfg.get("dc", 13))
    cs_pin = int(pins_cfg.get("cs", 10))
    rst_pin = int(pins_cfg.get("rst", 14))

    driver_name = tft_cfg.get("driver", "ST7789")
    disp_rotation = int(tft_cfg.get("rotation", 0))
    color_order = tft_cfg.get("color_order", "RGB")
    invert = bool(tft_cfg.get("invert", True))

    from lib.TFT import ST7789, ST7735, GC9A01, GC9D01, ILI9341
    driver_map = {
        "ST7789": ST7789,
        "ST7735": ST7735,
        "GC9A01": GC9A01,
        "GC9D01": GC9D01,
        "ILI9341": ILI9341,
    }
    driver_cls = driver_map.get(driver_name)
    if driver_cls is None:
        raise ValueError("Unsupported TFT driver: {}".format(driver_name))

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
        pixel_format=(cfg.get("jpeg") or {}).get("pixel_format", "RGB565_BE"),
        bytes_per_pixel=3 if (cfg.get("jpeg") or {}).get("pixel_format", "").startswith("RGB888") else 2,
    )
    lcd.set_window(0, 0)

    _fill_black_chunked(lcd, width, height)

    bus.register_service("lcd", lcd)
    bus.shared["tft_width"] = width
    bus.shared["tft_height"] = height

    print(f"🖥 [DPBoot] LCD {driver_name} {width}x{height} ready")

    _init_dp_pipeline(cfg, dp_config_path)

    return lcd


def _init_dp_pipeline(cfg, dp_config_path="/dp_config.json"):
    try:
        from lib.dp_manager_service import configure_from_dp_config
        from lib.dp_buffer_service import configure_for_layout

        svc = configure_from_dp_config(bus, cfg, dp_config_path=dp_config_path)
        layout = svc.get("layout") or []
        pixel_format = (svc.get("jpeg") or {}).get("pixel_format", "RGB565_BE")
        frame_bufs = int(bus.shared.get("pipeline_frame_buffers", 3) or 3)
        configure_for_layout(bus, layout, pixel_format=pixel_format, num_buffers=frame_bufs)
        print("✅ [DPBoot] Display pipeline configured (io={}, frame={})".format(
            int(bus.shared.get("pipeline_io_buffers", 3) or 3), frame_bufs))
    except Exception as e:
        print("⚠️ [DPBoot] Pipeline init failed:", e)


def _fill_black_chunked(lcd, width, height, chunk_rows=20):
    row_bytes = width * 2
    for y in range(0, height, chunk_rows):
        rows = min(chunk_rows, height - y)
        chunk = bytearray(row_bytes * rows)
        lcd.set_window(0, y, width - 1, y + rows - 1)
        lcd.write_data(chunk)
    lcd.set_window(0, 0)
