# tft_probe.py — 診斷 LCD/SPI/bus 的真實型態與能力
from lib.sys.sys_bus import bus

lcd = bus.get_service("lcd")
ba = getattr(lcd, "_bus", None)
spi = getattr(ba, "_spi", None) or getattr(lcd, "spi", None)
dc = getattr(ba, "_dc", None) or getattr(lcd, "dc", None)

print("=== LCD 物件 ===")
print("  lcd type:", type(lcd).__name__)
print("  lcd.width/height:", getattr(lcd, "width", "?"), getattr(lcd, "height", "?"))
print("  lcd._bus type:", type(ba).__name__)
print("  ba._dma flag:", getattr(ba, "_dma", "?"))
print("  ba._qspi flag:", getattr(ba, "_qspi", "?"))

print("\n=== SPI 物件 ===")
print("  spi type:", type(spi).__name__)
print("  has write:", hasattr(spi, "write"))
print("  has wait:", hasattr(spi, "wait"))
print("  has wait_all:", hasattr(spi, "wait_all"))
print("  has pending:", hasattr(spi, "pending"))
print("  has lane_count:", hasattr(spi, "lane_count"))
if hasattr(spi, "lane_count"):
    try:
        print("  lane_count():", spi.lane_count())
    except Exception as e:
        print("  lane_count() err:", e)

print("\n=== DC pin ===")
print("  dc type:", type(dc).__name__)
print("  dc value:", dc.value() if hasattr(dc, "value") else "?")

print("\n=== 判斷 ===")
is_dma = hasattr(spi, "wait") and hasattr(spi, "pending")
print("  DMA-capable spi:", is_dma)
if not is_dma:
    print("  ⚠️ spi 是 machine.SPI (非 DMA)，所有 DMA queue 邏輯無效！")
    print("     boot 時 lcd_bus fallback 了，需硬重啟讓 lcd_bus 成功初始化")
else:
    print("  ✓ spi 是 lcd_bus DMA，可走 DMA pipeline")

# === 真實 write 測試：送 1 byte 命令，看回傳 tid 類型 ===
print("\n=== write() 回傳值測試 ===")
try:
    dc.value(0)
    tid = spi.write(bytearray([0x2C]))
    print("  spi.write(1 byte) 回傳:", repr(tid), "type:", type(tid).__name__)
    if tid is not None:
        spi.wait(tid)
        print("  spi.wait(tid) OK")
    spi.wait_all()
    dc.value(1)
except Exception as e:
    print("  ERROR:", e)

# === pending() 測試 ===
print("\n=== pending() 測試 ===")
try:
    p = spi.pending()
    print("  pending():", p)
except Exception as e:
    print("  pending() ERROR:", e)
