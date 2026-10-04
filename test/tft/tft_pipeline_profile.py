# tft_pipeline_profile.py — 剖析 pipeline 的 34.5ms 到底花在哪四段
#
# 一次 fire 後量三段的實際耗時：
#   fire     : _fire() 本身（async 應 <1ms；若 ~19ms = C fire 同步 → 大問題）
#   decode   : _fake_decode busy-wait（驗證 20ms 是否真的 20ms）
#   wait     : decode 後等 DMA 的剩餘時間（0 = 完全重疊；~14ms = DMA 沒在 decode 期間跑）
#
# 判讀：
#   wait ≈ 0     → 重疊完美，問題在 fire 或 set_window 開銷
#   wait ≈ 14ms  → DMA 在 decode 期間沒跑完（背景傳輸被延遲）
#   fire ≈ 19ms  → C 層 fire 仍是同步的（spi_bus.c 有問題）

import gc, time
from lib.sys.sys_bus import bus
from tft_dma_bench import _fill_rainbow

_CHUNK = 32 * 1024


def _fake_decode(work_ms):
    if work_ms <= 0:
        return
    t0 = time.ticks_us()
    while time.ticks_diff(time.ticks_us(), t0) < work_ms * 1000:
        pass


def _alloc_spiram(size):
    try:
        import heap_caps
        b = heap_caps.malloc(size, heap_caps.CAP_SPIRAM)
        if b is not None:
            return b
    except Exception:
        pass
    return bytearray(size)


def _fire(ba, mv):
    """tft_test_tool 同款：32KB 分段 write_data_async，回傳最後 tid"""
    off, rem = 0, len(mv)
    last = None
    while rem > 0:
        n = _CHUNK if rem > _CHUNK else rem
        tid = ba.write_data_async(mv[off:off + n])
        if tid is not None:
            last = tid
        off += n
        rem -= n
    return last


def run(work_ms=20, frames=30):
    lcd = bus.get_service("lcd")
    if lcd is None:
        print("❌ lcd not on bus")
        return
    ba = getattr(lcd, "_bus", None)
    spi = getattr(ba, "_spi", None) or getattr(lcd, "spi", None)
    dc = getattr(ba, "_dc", None) or getattr(lcd, "dc", None)
    w = int(bus.shared.get("tft_width", getattr(lcd, "width", 240)))
    h = int(bus.shared.get("tft_height", getattr(lcd, "height", 320)))
    size = w * h * 2

    fb = _alloc_spiram(size)
    _fill_rainbow(fb, w, h, 0)
    mv = fb if isinstance(fb, memoryview) else memoryview(fb)[:size]

    print("=" * 56)
    print("TFT pipeline profile (present 結構, work={}ms, {} frames)".format(work_ms, frames))
    print("=> 拆解 RAMWR / fire / decode / wait 四段")
    print("=" * 56)

    def ramwr_fast():
        dc.value(0); t = spi.write(bytearray([0x2C])); spi.wait(t)
        dc.value(1)

    def fire():
        off, rem = 0, len(mv)
        last = None
        while rem > 0:
            n = 32768 if rem > 32768 else rem
            tid = ba.write_data_async(mv[off:off + n])
            if tid is not None:
                last = tid
            off += n
            rem -= n
        return last

    # warmup
    lcd.begin_display()
    ramwr_fast()
    tid = fire()
    if tid is not None:
        ba.wait(tid)

    acc_ram = acc_fire = acc_decode = acc_wait = 0
    for i in range(frames):
        t1 = time.ticks_us()
        ramwr_fast()                             # RAMWR（polling wait）
        t2 = time.ticks_us()
        acc_ram += time.ticks_diff(t2, t1)

        t3 = time.ticks_us()
        tid = fire()                             # fire 5×32KB（async 進 queue）
        t4 = time.ticks_us()
        acc_fire += time.ticks_diff(t4, t3)

        t5 = time.ticks_us()
        _fake_decode(work_ms)                    # 20ms — DMA 應在背景跑完
        t6 = time.ticks_us()
        acc_decode += time.ticks_diff(t6, t5)

        t7 = time.ticks_us()
        if tid is not None:
            ba.wait(tid)                         # 等剩餘
        t8 = time.ticks_us()
        acc_wait += time.ticks_diff(t8, t7)

    n = frames
    print("  平均 RAMWR : {:>7}us".format(acc_ram // n))
    print("  平均 fire  : {:>7}us   (5×32KB enqueue；阻塞=queue 滿)".format(acc_fire // n))
    print("  平均 decode: {:>7}us".format(acc_decode // n))
    print("  平均 wait  : {:>7}us   (0=完全重疊；大=DMA 沒藏進 decode)".format(acc_wait // n))
    print("  合計       : {:>7}us".format((acc_ram + acc_fire + acc_decode + acc_wait) // n))
    print("-" * 56)
    print("判讀：")
    print("  wait≈0 且 fire≈4.5ms → DMA 已藏進 decode，剩餘是 Python 分段開銷")
    print("  fire 阻塞（≈14ms）   → queue 滿，C fire 或等待邏輯卡住")
    print("  wait≈9ms             → DMA 在 decode 期間沒跑完")
    print("done.")
