"""
TFT 顯示驅動配置層 — 支援 SPI / QSPI / I80 / RGB / I2C

兩種呼叫方式:
  config(spi=..., dc=..., cs=..., rst=..., driver="...", ...)   ← 工廠式，明確傳參
  init_tft(bus)                                                  ← boot 模式，從 bus.shared['TFT'] 讀
"""

def config(spi, dc, cs, rst, driver="ST7789", width=240, height=320,
           rotation=0, color_order="RGB", invert=False,
           pixel_format="RGB565_BE", bytes_per_pixel=2, adapter=None,
           variant=None):
    """工廠函式 — 明確傳入 SPI / pin 物件"""
    from lib.hw.TFT import ST7789, ST7735, ST7796, GC9A01, GC9D01, ILI9341, NV3030B

    driver_map = {
        "ST7789":        ST7789,
        "ST7735":        ST7735,
        "GC9A01":        GC9A01,
        "GC9D01":        GC9D01,
        "ILI9341":       ILI9341,
        "NV3030B":       NV3030B,
        "ST7796":        ST7796,
        "ST7796_I80":    ST7796,   # I80 介面用 ST7796 driver
    }

    for lazy_drv in ("RM67162", "SH8601"):
        if driver == lazy_drv:
            try:
                mod = __import__("lib.hw.TFT", None, None, [lazy_drv])
                driver_map[lazy_drv] = getattr(mod, lazy_drv)
            except (ImportError, AttributeError):
                raise ValueError("{} not available — update lib/TFT.py on device".format(lazy_drv))

    driver_cls = driver_map.get(driver)
    if driver_cls is None:
        raise ValueError("Unsupported TFT driver: {}".format(driver))

    kwargs = {}
    if variant is not None:
        kwargs["variant"] = variant

    lcd = driver_cls(
        spi=spi,
        dc=dc,
        cs=cs,
        rst=rst,
        width=width,
        height=height,
        rotation=rotation,
        color_order=color_order,
        invert=invert,
        pixel_format=pixel_format,
        bytes_per_pixel=bytes_per_pixel,
        adapter=adapter,
        **kwargs,
    )

    return lcd


def init_tft(sysbus=None):
    """boot 模式 — 從 bus.shared['TFT'] 讀設定，由 bus service 解析 SPI / pin"""
    from lib.sys.sys_bus import bus
    from lib.sys.bus_adapter import SpiBusAdapter

    sysbus = sysbus or bus
    cfg = sysbus.shared.get("TFT") or {}
    if not cfg.get("enable"):
        return None
    cfg = dict(cfg)  # 複製，避免 pop 影響 bus.shared 原始 dict
    cfg.pop("enable", None)  # enable 已判斷過，不傳給 driver class

    spi_list = sysbus.get_service("spi_list") or []
    pin_by_label = sysbus.get_service("pin_by_label") or {}

    pins = cfg.pop("pins", {})
    dc  = pin_by_label.get(pins.get("dc", ""))
    cs  = pin_by_label.get(pins["cs"])
    rst = pin_by_label.get(pins["rst"])

    missing = []
    if cs  is None: missing.append("cs={}".format(pins["cs"]))
    if rst is None: missing.append("rst={}".format(pins["rst"]))
    if missing:
        raise ValueError("TFT pins not found: {}".format(", ".join(missing)))

    # ⚠️ 必須先開電源，RM67162 才能接收 init 命令
    bl = pin_by_label.get(pins.get("bl", ""))
    if bl is not None:
        bl.value(1)
        print("[tft_drv] power ON (GPIO={})".format(pins.get("bl", "")))
    else:
        print("[tft_drv] no power pin — display may not be powered")

    spi_id = cfg.pop("spi_id", 1)
    # 從 spi_list 找對應 host id；找不到 fallback 第一個
    spi = None
    if spi_list:
        spi_cfg = sysbus.shared.get("SPI") or {}
        for i, item in enumerate(spi_cfg.get("list", [])):
            if item.get("id") == spi_id and i < len(spi_list):
                spi = spi_list[i]
                break
        if spi is None:
            spi = spi_list[0]
    if spi is None:
        print("[tft_drv] no SPI bus available, skipping")
        return None

    fmt = cfg.get("pixel_format", "RGB565_BE")
    bpp = 3 if fmt.startswith("RGB888") else 2

    adapter = SpiBusAdapter(spi, dc, cs, rst)
    lcd = config(spi=spi, dc=dc, cs=cs, rst=rst,
                 bytes_per_pixel=bpp, adapter=adapter, **cfg)

    sysbus.register_service("lcd", lcd)
    sysbus.shared["tft_width"] = cfg["width"]
    sysbus.shared["tft_height"] = cfg["height"]
    sysbus.shared["tft_driver"] = cfg["driver"]

    # 全黑畫面 (整幀, TFT.show 含 flush, DMA queue 確保送出)
    black = bytearray(cfg["width"] * cfg["height"] * bpp)
    lcd.show(black)

    return lcd

def init_tft_i80(sysbus=None):
    """I80 boot 模式 — 適用 ST7796 + N16R8 (XL9555 控制 RST/背光)
    設定來源: bus.shared['TFT']
    """
    from lib.sys.sys_bus import bus
    from lib.sys.bus_adapter import I80BusAdapter

    sysbus = sysbus or bus
    cfg = sysbus.shared.get("TFT") or {}
    if not cfg.get("enable"):
        return None
    cfg = dict(cfg)
    cfg.pop("enable", None)  # enable 已判斷過，不傳給 driver class

    i80 = sysbus.get_service("i80_bus")
    if i80 is None:
        print("[tft_drv] no I80 bus available, skipping")
        return None

    pin_by_label = sysbus.get_service("pin_by_label") or {}
    pins = cfg.pop("pins", {})

    dcx = pin_by_label.get(pins.get("dcx", ""))

    # ── XL9555: LCD 復位 + 背光 (從 config 讀腳位) ──
    xl_cfg = cfg.pop("xl9555", {})
    xl = sysbus.get_service("xl9555")
    if xl and xl_cfg:
        rst_pin = xl_cfg.get("rst")
        bl_pin = xl_cfg.get("bl")
        import time
        if rst_pin is not None:
            xl.pin[rst_pin].init(1); xl.pin[rst_pin].value(0)
            time.sleep_ms(10)
            xl.pin[rst_pin].value(1)
            time.sleep_ms(10)
        if bl_pin is not None:
            xl.pin[bl_pin].init(1); xl.pin[bl_pin].value(1)
        print("[tft_drv] XL9555: rst={} bl={}".format(rst_pin, bl_pin))
    else:
        print("[tft_drv] XL9555 not available")

    adapter = I80BusAdapter(i80, dcx=dcx, rst=None)  # RST 由 XL9555 管理
    bpp = 3 if cfg.get("pixel_format", "").startswith("RGB888") else 2

    lcd = config(spi=None, dc=None, cs=None, rst=None,
                 bytes_per_pixel=bpp, adapter=adapter, **cfg)

    sysbus.register_service("lcd", lcd)
    sysbus.shared["tft_width"] = cfg["width"]
    sysbus.shared["tft_height"] = cfg["height"]
    sysbus.shared["tft_driver"] = cfg["driver"]

    # 全黑畫面
    black = bytearray(cfg["width"] * cfg["height"] * bpp)
    lcd.show(black)

    return lcd


def gpios(sysbus=None):
    """TFT 不直接擁有 GPIO（SPI 由 spi_drv、控制腳由 pin_drv 註冊）"""
    return {}
