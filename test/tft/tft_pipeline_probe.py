# tft_pipeline_probe.py — decode/DMA 重疊驗證
#
# 目標：證明「fire DMA 不 wait → 做下一幀計算 → 再 wait」時，
#       decode 與 DMA 是否真正重疊（frame ≈ max(decode, transfer)）
#       而不是序列化（frame ≈ decode + transfer）。
#
# 背景：
#   - jpeg_player_task 現行 = 單 fb + 立即 wait → 序列化
#   - test_jpeg_full      = 雙 fb + fire-and-defer → 重疊（正確手法）
#   - PSRAM fb 走 C copy 路徑（spi_bus.c copy 分支每 chunk wait_queue_empty）
#     → spi.write 本身同步，fire 會吃掉整個傳輸時間
#
# 方法：不依賴真實 jpeg（可重現），用 fake_decode 模擬計算成本，
#       雙 fb + fire-and-defer 管線，對照 serial 模式。
#
#   work_ms : 假 decode 成本（CPU 忙碌，可調）
#   xfer    : 150KB DMA 傳輸 ≈ 19ms
#
# 預期（重疊成功時）：
#   serial    frame ≈ work_ms + xfer     （無重疊，恒成立）
#   pipeline  frame ≈ max(work_ms, xfer) （有重疊）
#
# 現況判讀（PSRAM fb + copy 同步）：
#   pipeline ≈ serial ≈ work_ms + 19ms → copy 路徑同步殺死重疊
#   → 需要 C double-buffer zero_buf（或內部 DMA bounce）
#
# 用法（soft reboot 後）：
#   import tft_pipeline_probe
#   tft_pipeline_probe.run_all()          # work = 0 / 15 / 50ms 三組
#   tft_pipeline_probe.run_all(work_ms=20)# 指定一組

import gc, time
from lib.sys.sys_bus import bus

_WARM = 3
_FRAMES = 30
_CHUNK = 32 * 1024


def _alloc_spiram(size):
    try:
        import heap_caps
        b = heap_caps.malloc(size, heap_caps.CAP_SPIRAM)
        if b is not None:
            return b
    except Exception:
        pass
    return bytearray(size)


def _free(buf):
    if isinstance(buf, memoryview):
        try:
            import heap_caps
            heap_caps.free(buf)
        except Exception:
            pass


def _fake_decode(fb, work_ms):
    """模擬 decode 成本：CPU 忙碌（不碰 fb 內容，純佔用），至少忙 work_ms。
    回傳實際耗時 us。"""
    t0 = time.ticks_us()
    if work_ms <= 0:
        return 0
    while time.ticks_diff(time.ticks_us(), t0) < work_ms * 1000:
        pass
    return time.ticks_diff(time.ticks_us(), t0)


def _ramwr_start(spi, dc, w, h):
    dc.value(0); spi.write(bytearray([0x2A]))
    dc.value(1); spi.write(bytes([0, 0, (w - 1) >> 8, (w - 1) & 0xFF]))
    dc.value(0); spi.write(bytearray([0x2B]))
    dc.value(1); spi.write(bytes([0, 0, (h - 1) >> 8, (h - 1) & 0xFF]))
    spi.wait_all()
    dc.value(0); spi.write(bytearray([0x2C])); spi.wait_all()
    dc.value(1)


def _ramwr_fast(spi, dc):
    dc.value(0); t = spi.write(bytearray([0x2C])); spi.wait(t)
    dc.value(1)


def _dma_fire(bus_adapter, mv):
    """async fire：分 chunk write_data_async，回傳 tid 列表（不 wait）。
    注意：PSRAM fb 走 C copy 路徑 → 每 chunk 內部 wait_queue_empty（同步）。"""
    off, rem = 0, len(mv)
    tids = []
    while rem > 0:
        n = _CHUNK if rem > _CHUNK else rem
        tid = bus_adapter.write_data_async(mv[off:off + n])
        if tid is not None:
            tids.append(tid)
        off += n
        rem -= n
    return tids


def _wait_tids(bus_adapter, tids):
    for t in tids:
        bus_adapter.wait(t)


def _time_serial(spi, dc, ba, fb, mv, work_ms, frames):
    """serial：decode → fire → wait（jpeg_player_task 現行行為）"""
    for _ in range(_WARM):
        _fake_decode(fb, work_ms)
        _ramwr_fast(spi, dc)
        _wait_tids(ba, _dma_fire(ba, mv))
    gc.collect()
    t0 = time.ticks_us()
    for _ in range(frames):
        _fake_decode(fb, work_ms)
        _ramwr_fast(spi, dc)
        _wait_tids(ba, _dma_fire(ba, mv))
    return time.ticks_diff(time.ticks_us(), t0) // frames


def _time_pipeline(spi, dc, ba, fb_a, fb_b, mv_a, mv_b, work_ms, frames):
    """pipeline：fire(fb_dma) 不 wait → decode(fb_decode) → swap → wait（test_jpeg_full）"""
    # 初始：fb_a 先解一幀，fire
    _fake_decode(fb_a, work_ms)
    _ramwr_fast(spi, dc)
    cur = mv_a
    for _ in range(_WARM + frames):
        tids = _dma_fire(ba, cur)          # 1. fire 上一幀（不 wait）
        if cur is mv_a:                    #    decode 進另一塊
            _fake_decode(fb_b, work_ms)
            nxt = mv_b
        else:
            _fake_decode(fb_a, work_ms)
            nxt = mv_a
        _wait_tids(ba, tids)               # 2. 等上幀 DMA 完成
        cur = nxt                          # 3. swap
        _ramwr_fast(spi, dc)
    gc.collect()
    # 正式計時（warmup 後）
    t0 = time.ticks_us()
    for _ in range(frames):
        tids = _dma_fire(ba, cur)
        if cur is mv_a:
            _fake_decode(fb_b, work_ms)
            nxt = mv_b
        else:
            _fake_decode(fb_a, work_ms)
            nxt = mv_a
        _wait_tids(ba, tids)
        cur = nxt
        _ramwr_fast(spi, dc)
    return time.ticks_diff(time.ticks_us(), t0) // frames


def run_all(work_ms=None, frames=_FRAMES):
    lcd = bus.get_service("lcd")
    if lcd is None:
        print("❌ lcd not on bus — run boot.py first")
        return
    ba = getattr(lcd, "_bus", None)
    spi = getattr(ba, "_spi", None) or getattr(lcd, "spi", None)
    dc = getattr(ba, "_dc", None) or getattr(lcd, "dc", None)
    if spi is None or dc is None:
        print("❌ need spi/dc")
        return

    w = int(bus.shared.get("tft_width", getattr(lcd, "width", 240)))
    h = int(bus.shared.get("tft_height", getattr(lcd, "height", 320)))
    bpp = int(getattr(lcd, "bytes_per_pixel", 2))
    fb_size = w * h * bpp

    print("=" * 62)
    print("TFT decode/DMA overlap probe  ({}x{} {}KB/frame)".format(w, h, fb_size // 1024))
    print("xfer ≈ 19ms (PSRAM fb, C copy path)")
    print("=" * 62)

    fb_a = _alloc_spiram(fb_size)
    fb_b = _alloc_spiram(fb_size)
    mv_a = fb_a[:fb_size] if isinstance(fb_a, memoryview) else memoryview(fb_a)[:fb_size]
    mv_b = fb_b[:fb_size] if isinstance(fb_b, memoryview) else memoryview(fb_b)[:fb_size]
    print("dual fb: {} + {} ({}KB total)\n".format(
        "PSRAM" if isinstance(fb_a, memoryview) else "bytearray",
        "PSRAM" if isinstance(fb_b, memoryview) else "bytearray",
        (len(fb_a) + len(fb_b)) // 1024))

    _ramwr_start(spi, dc, w, h)
    works = [work_ms] if work_ms is not None else [0, 15, 50]

    print("{:<8} {:>12} {:>12}   overlap?".format("work_ms", "serial", "pipeline"))
    print("-" * 62)
    for wms in works:
        s = _time_serial(spi, dc, ba, fb_a, mv_a, wms, frames)
        p = _time_pipeline(spi, dc, ba, fb_a, fb_b, mv_a, mv_b, wms, frames)
        saved = s - p
        overlap = "✓ 重疊 ({:>4}ms 省)".format(saved // 1000) if saved > 3000 else "✗ 無重疊"
        print("{:<8} {:>10}ms {:>10}ms   {}".format(wms, s // 1000, p // 1000, overlap))
    print("-" * 62)
    print("判讀：pipeline ≈ max(work, xfer) → 重疊成功；≈ serial → copy 路徑同步，需 C double-buffer")

    _free(fb_a)
    _free(fb_b)
    print("done.")
