# tft_pipeline_visual.py — 可視化 decode/DMA 重疊驗證（tft_test_tool 同款穩妥寫法）
#
# 顯示路徑完全照 tft_test_tool（證明可出畫面）：
#   每幀 set_window() 完整 API + 手動 32KB 分段 fire + wait(last) + flush()
#
# 兩種模式：
#   run()           — pipeline：fire(prev, async) → fake_decode → wait(prev)（重疊）
#   run(serial=1)   — serial：decode → fire → 立即 wait（無重疊，對照組）
#
# 判讀（眼睛看螢幕 + 數字）：
#   work=20ms 時 serial ≈ 37ms（卡），pipeline ≈ 20ms（順）
#   → 螢幕上看見彩虹連續滾動 = 過

import gc, time
from lib.sys.sys_bus import bus
from tft_dma_bench import _fill_rainbow

_WARM = 5
_CHUNK = 32 * 1024


def _setup():
    lcd = bus.get_service("lcd")
    if lcd is None:
        raise RuntimeError("LCD not on bus — did boot.py run?")
    ba = getattr(lcd, "_bus", None)
    w = int(bus.shared.get("tft_width", getattr(lcd, "width", 240)))
    h = int(bus.shared.get("tft_height", getattr(lcd, "height", 320)))
    return lcd, ba, w, h


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


def _free(buf):
    if isinstance(buf, memoryview):
        try:
            import heap_caps
            heap_caps.free(buf)
        except Exception:
            pass


def _build_pool(w, h, pool):
    fbs = []
    size = w * h * 2
    for p in range(pool):
        fb = _alloc_spiram(size)
        _fill_rainbow(fb, w, h, p)
        fbs.append(fb if isinstance(fb, memoryview) else memoryview(fb)[:size])
    return fbs


def _fire(ba, mv):
    """手動 32KB 分段（tft_dma_bench / tft_test_tool 同款，證明顯示正常）。
    每段 write_data_async（C 每筆 1 chunk，PSRAM async 直送），回傳最後 tid。
    ⚠ 不用單次 150KB write — 實測 C 單次大 write 分 chunk 只送 ~1/3（bug，待查）。"""
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


def _ramwr_fast(spi, dc):
    """RAMWR（begin_display 已設窗，每幀只需 RAMWR 重啟寫入）"""
    dc.value(0); t = spi.write(bytearray([0x2C])); spi.wait(t)
    dc.value(1)


def run(work_ms=0, frames=120, pool=6, serial=0, present=1):
    lcd, ba, w, h = _setup()
    size = w * h * 2
    spi = getattr(ba, "_spi", None) or getattr(lcd, "spi", None)
    dc = getattr(ba, "_dc", None) or getattr(lcd, "dc", None)

    print("=" * 60)
    print("TFT pipeline VISUAL test  (mode={} work={}ms present={})".format(
        "serial" if serial else "pipeline", work_ms, present))
    print("=> 螢幕應看見彩虹條紋滾動（連續、不撕裂）")
    print("=" * 60)

    pool_fbs = _build_pool(w, h, pool)
    print("pool: {} 幀彩虹 ({}KB)\n".format(pool, pool * size // 1024))

    def play_serial():
        # decode → RAMWR+fire → 立即 wait（現行 jpeg_player 行為）
        lcd.begin_display()
        for _ in range(_WARM):
            _fake_decode(work_ms)
            _ramwr_fast(spi, dc)
            tid = _fire(ba, pool_fbs[0])
            if tid is not None:
                ba.wait(tid)
        times = []
        for i in range(frames):
            t0 = time.ticks_us()
            _fake_decode(work_ms)
            _ramwr_fast(spi, dc)
            tid = _fire(ba, pool_fbs[i % pool])
            if tid is not None:
                ba.wait(tid)
            times.append(time.ticks_diff(time.ticks_us(), t0))
            if i % 20 == 0:
                print("  frame {}/{}  last={}us".format(i, frames, times[-1]))
        ba.flush()
        return times

    def play_present():
        # 理想重疊：RAMWR+fire(先) → decode（DMA 藏進 decode）→ 才 wait
        # fire 單次 spi.write，C 自動 async 分 chunk；8-deep queue 5 chunk 全進不阻塞
        lcd.begin_display()
        for _ in range(_WARM):
            _ramwr_fast(spi, dc)
            tid = _fire(ba, pool_fbs[0])
            _fake_decode(work_ms)
            if tid is not None:
                ba.wait(tid)
        times = []
        for i in range(frames):
            t0 = time.ticks_us()
            _ramwr_fast(spi, dc)
            tid = _fire(ba, pool_fbs[i % pool])   # fire（~1ms，async 進 queue）
            _fake_decode(work_ms)                 # 20ms — 期間 DMA 背景送整幀
            if tid is not None:
                ba.wait(tid)                      # 應 ≈0（decode 已蓋完）
            times.append(time.ticks_diff(time.ticks_us(), t0))
            if i % 20 == 0:
                print("  frame {}/{}  last={}us".format(i, frames, times[-1]))
        ba.flush()
        return times

    def play_pipeline():
        # fire(prev, async) → decode(cur) → wait(prev) → set_window → fire(cur)
        lcd.set_window(0, 0, w - 1, h - 1)
        tid = _fire(ba, pool_fbs[0])
        for _ in range(_WARM):
            _fake_decode(work_ms)
            if tid is not None:
                ba.wait(tid)
            lcd.set_window(0, 0, w - 1, h - 1)
            tid = _fire(ba, pool_fbs[1 % pool])
        times = []
        for i in range(frames):
            t0 = time.ticks_us()
            _fake_decode(work_ms)               # decode 期間背景 DMA 送上一幀
            if tid is not None:
                ba.wait(tid)                    # 等上一幀傳完
            lcd.set_window(0, 0, w - 1, h - 1)  # 同步屏障（上一幀已完）
            tid = _fire(ba, pool_fbs[(i + 1) % pool])   # fire 下一幀（async）
            times.append(time.ticks_diff(time.ticks_us(), t0))
            if i % 20 == 0:
                print("  frame {}/{}  last={}us".format(i, frames, times[-1]))
        if tid is not None:
            ba.wait(tid)
        ba.flush()
        return times

    if serial:
        times = play_serial()
    elif present:
        times = play_present()
    else:
        times = play_pipeline()

    warm = times[5:] if len(times) > 5 else times
    avg = sum(warm) // len(warm)
    fps = 1e6 / avg if avg else 0
    print("\n  {}  avg={}us  {:.0f}fps".format(
        "serial  " if serial else "pipeline", avg, fps))
    print("  螢幕檢視：彩虹應連續滾動無撕裂 = PASS")

    for fb in pool_fbs:
        _free(fb)
    print("done.")
