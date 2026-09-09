"""
spi_drv.py — SPI 匯流排管理

設定來源: bus.shared["SPI"]  ({enable, list})
產物:    bus.register_service("spi_list", [SPI_obj, ...])

支援 lcd_bus (DMA) 與 machine.SPI fallback。
"""
from machine import Pin, SPI
from lib.sys.sys_bus import bus

try:
    import lcd_bus
    _LCD_BUS = True
except ImportError:
    _LCD_BUS = False


def _make_machine_spi(item, gpio, data):
    """fallback — 用 machine.SPI 建單線 SPI bus, 先嘗試釋放舊佔用"""
    sid = item["id"]
    # soft reset 後 SPI host 可能殘留, 嘗試 deinit
    try:
        old = SPI(sid)
        old.deinit()
    except:
        pass
    sck = gpio.get("sck")
    mosi = gpio.get("mosi")
    miso = gpio.get("miso")
    # lcd_bus 模式用 data_pins 當 data line (單線 = data_pins[0]);
    # fallback 到 machine.SPI 時需把它對應到 mosi
    if mosi is None and data:
        mosi = data[0]
    return SPI(
        sid,
        baudrate=item.get("baudrate", 80000000),
        polarity=item.get("polarity", 0),
        phase=item.get("phase", 0),
        sck=Pin(sck) if sck is not None else None,
        mosi=Pin(mosi) if mosi is not None else None,
        miso=Pin(miso) if miso is not None else None,
    )


def init_spi(sysbus=None):
    """讀 bus.shared['SPI'] → 建 SPI → 註冊 'spi_list'"""
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("SPI") or {}
    if not cfg.get("enable"):
        return []

    spi_list = []
    # 🔧 force_machine: 1 = 強制用 machine.SPI (同步、中斷路線), 不走 lcd_bus DMA。
    #    給「只要穩定交貨、不要 DMA 撕裂風險」的場景用 (APA102 等)。
    force_machine = cfg.get("force_machine", 0)
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        data = item.get("data_pins")
        # 容忍跨語言空值寫法: 空 list / null / 缺省 一律視為未設定。
        # (空 list 若不轉 None, 會被丟給 lcd_bus 當「0 條資料線」報錯)
        if data is not None and len(data) == 0:
            data = None

        if _LCD_BUS and not force_machine:
            if data is not None:
                d = data
            else:
                mosi = gpio.get("mosi")
                d = (mosi,) if mosi is not None else None
            try:
                spi = lcd_bus.SPIBus(
                    data=d, clk=gpio["sck"],
                    freq=item.get("baudrate", 80000000),
                    host=item["id"],
                )
            except Exception as e:
                try:
                    from lib.sys.log_service import get_log
                    get_log().warn(
                        "[spi_drv] SPI{} lcd_bus fail: {} → machine.SPI fallback "
                        "(無 DMA queue，效能會大幅下降)".format(item["id"], e))
                except Exception:
                    print("[spi_drv] SPI{} lcd_bus fail: {} → machine.SPI".format(item["id"], e))
                # 嘗試釋放 lcd_bus 可能殘留的佔用
                if 'spi' in locals():
                    try: spi.deinit()
                    except: pass
                # 🔧 fallback 也要防護: 失敗 (例: SPI host 殘留佔用) 只跳過這條,
                #    不再讓例外往上拋把整個 boot 搞掛。
                try:
                    spi = _make_machine_spi(item, gpio, data)
                except Exception as e2:
                    try:
                        from lib.sys.log_service import get_log
                        get_log().error("[spi_drv] SPI{} fallback fail: {}".format(item["id"], e2))
                    except Exception:
                        print("[spi_drv] SPI{} fallback fail: {}".format(item["id"], e2))
                    continue
        else:
            try:
                spi = _make_machine_spi(item, gpio, data)
            except Exception as e:
                try:
                    from lib.sys.log_service import get_log
                    get_log().error("[spi_drv] SPI{} init fail: {}".format(item["id"], e))
                except Exception:
                    print("[spi_drv] SPI{} init fail: {}".format(item["id"], e))
                continue

        spi_list.append(spi)

    sysbus.register_service("spi_list", spi_list)
    return spi_list


def gpios(sysbus=None):
    """回傳此 driver 用的 {gpio: label}，供 boot 統一 claim/validate"""
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("SPI") or {}
    if not cfg.get("enable"):
        return {}

    result = {}
    for item in cfg.get("list", []):
        gpio = item.get("GPIO", {})
        sid = item.get("id", "?")
        for name in ("sck", "mosi", "miso"):
            pin = gpio.get(name)
            if pin is not None:
                result[pin] = "spi{}_{}".format(sid, name)
        data = item.get("data_pins")
        if data:
            for i, d in enumerate(data):
                result[d] = "spi{}_d{}".format(sid, i)
    return result
