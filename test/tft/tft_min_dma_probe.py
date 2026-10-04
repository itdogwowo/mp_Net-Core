# tft_min_dma_probe.py — 最小消歧測試
#
# 目的：分離「120ms/幀」到底是
#   (A) 測試記憶體設計 bug（fb_mode="dma" + prebuilt pool=6 = 900KB 塞爆 heap → fallback PSRAM）
#   還是
#   (B) C 模組分 chunk 路徑 bug（enqueue_raw / spi_wait_free_slot / spi_wait_queue_empty）
#
# 方法：全部 direct 直送 + interval=0（不 sleep），低記憶體足跡（不建 pool）。
#   T1 fast-ref  fb_mode="dma" pool=1  — heap_caps.malloc(150KB, CAP_DMA) 成功 →
#                                         內部 DRAM DMA 直讀（真正的 DMA 路徑）
#   T2 ram-ref   fb_mode="ram" pool=1  — heap bytearray（若 GC heap 在 PSRAM → 走 C copy 路徑）
#   T4 bounce    — 手動 8KB 內部 CAP_DMA bounce，逐 chunk refill + spi.write：
#                                         保證「內部單筆 enqueue + Python 級管線」，
#                                         純測 C 機械（不依賴大塊 CAP_DMA 能否成功）
#
# 判讀：
#   T1 或 T4 ≈ 15-20ms（理論線速 15.36ms@80MHz） → C 機械沒問題；120ms 是記憶體設計問題
#   T1/T4 ≈ 120ms                               → C 機械有問題，往 spi_bus.c 查
#   T2 ≈ 120ms 但 T4 快                         → C PSRAM copy 路徑太慢（序列化），
#                                                  需要 C double-buffer 或 Python 換 bounce
#
# 用法（soft reboot 後，boot.py 已完成 LCD 初始化）：
#   import tft_min_dma_probe
#   tft_min_dma_probe.run_all(frames=50)

import gc, time
from lib.sys.sys_bus import bus

_THEORY_US = 153600 * 8 * 1000000 // 80000000   # 240x320x2 @ 80MHz 線速 = 15,360us
_WARM = 5


# ═══════════════ heap / 分配診斷 ═══════════════

def _heap_diag(tag=""):
    try:
        import heap_caps
        for name, caps in (("DMA   ", heap_caps.CAP_DMA),
                           ("SPIRAM", heap_caps.CAP_SPIRAM)):
            try:
                print("    {} {}: free={}KB".format(
                    tag, name,
                    heap_caps.get_free_size(caps) // 1024))
            except Exception:
                pass
    except Exception as e:
        print("    heap_caps unavailable: {}".format(e))


def _alloc_one(size, fb_mode):
    """單一 fb 分配，回傳 (buf, kind)。
    kind: "heap_caps:DMA" | "heap_caps:SPIRAM" | "bytearray"（=MicroPython GC heap）
    ⚠ 實測（v1.29.0-preview）：heap_caps.malloc 失敗是回傳 None（不是 raise），
    也可能 raise MemoryError — 兩種都要 fallback bytearray。"""
    try:
        import heap_caps
        caps = heap_caps.CAP_DMA if fb_mode == "dma" else heap_caps.CAP_SPIRAM
        b = heap_caps.malloc(size, caps)     # 成功 → memoryview
        if b is None:
            return bytearray(size), "bytearray"
        return b, "heap_caps:" + fb_mode.upper()
    except Exception:
        pass
    return bytearray(size), "bytearray"


def _free(buf, kind):
    if kind.startswith("heap_caps"):
        try:
            import heap_caps
            heap_caps.free(buf)
        except Exception:
            pass


# ═══════════════ direct 直送（照 tft_dma_bench direct 手法） ═══════════════

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


def fire_direct(spi, mv, fb_size, chunk):
    """mv[off:off+n] 直送（C 依 buffer 位置自動選單筆/copy 路徑）+ pending>=2 退讓"""
    off, tids = 0, []
    while off < fb_size:
        n = chunk if (fb_size - off) > chunk else (fb_size - off)
        if spi.pending() >= 2:
            spi.wait(tids[0]); tids.pop(0)
        tid = spi.write(mv[off:off + n])
        if tid is not None:
            tids.append(tid)
        off += n
    spi.wait_all()


def fire_bounce(spi, mv, fb_size, bounce, chunk):
    """bounce（內部 CAP_DMA）refill + 送 → 每 chunk 保證走 C 內部單筆路徑，
    Python 級管線（pending>=2 退讓）。純測 C enqueue/wait 機械。"""
    bmv = bounce
    off, tids = 0, []
    while off < fb_size:
        n = chunk if (fb_size - off) > chunk else (fb_size - off)
        bmv[:n] = mv[off:off + n]
        if spi.pending() >= 2:
            spi.wait(tids[0]); tids.pop(0)
        tid = spi.write(bmv[:n])
        if tid is not None:
            tids.append(tid)
        off += n
    spi.wait_all()


def _time_frames(fire_fn, spi, mv, fb_size, chunk, bounce=None, frames=50):
    gc.collect()
    for _ in range(_WARM):
        fire_fn(spi, mv, fb_size, chunk) if bounce is None else fire_fn(spi, mv, fb_size, bounce, chunk)
    gc.collect()
    t0 = time.ticks_us()
    for _ in range(frames):
        fire_fn(spi, mv, fb_size, chunk) if bounce is None else fire_fn(spi, mv, fb_size, bounce, chunk)
    elapsed = time.ticks_diff(time.ticks_us(), t0)
    return elapsed // frames


# ═══════════════ 主流程 ═══════════════

def run_all(frames=50):
    lcd = bus.get_service("lcd")
    if lcd is None:
        print("❌ lcd not on bus — run boot.py first")
        return
    bus_adapter = getattr(lcd, "_bus", None)
    spi = getattr(bus_adapter, "_spi", None) or getattr(lcd, "spi", None)
    dc = getattr(bus_adapter, "_dc", None) or getattr(lcd, "dc", None)
    if spi is None or dc is None:
        print("❌ need spi/dc (direct mode)")
        return

    w = int(bus.shared.get("tft_width", getattr(lcd, "width", 240)))
    h = int(bus.shared.get("tft_height", getattr(lcd, "height", 320)))
    bpp = int(getattr(lcd, "bytes_per_pixel", 2))
    fb_size = w * h * bpp

    print("=" * 60)
    print("TFT minimal DMA probe  ({}x{} {}bpp = {}KB/frame)".format(
        w, h, bpp, fb_size // 1024))
    print("theory @80MHz 1-lane: {}us/frame  (lane_count={})".format(
        _THEORY_US, spi.lane_count() if hasattr(spi, "lane_count") else "?"))
    print("=" * 60)
    _heap_diag("start")
    print("-" * 60)

    results = []

    # ── T0: gc_collect() 成本（關鍵！） ──
    # animate(direct) 每幀結尾 spi.wait_all() → C 內 gc_collect()。
    # 若 MP heap 在 PSRAM 且含大量 frame buffer，full GC sweep 可能 50-150ms。
    # 模擬 prebuilt pool=6 的記憶體足跡再量。
    print("[T0] gc_collect() cost with 6x150KB loaded (simulate prebuilt pool)")
    t_bufs = []
    try:
        for _ in range(6):
            try:
                t_bufs.append(bytearray(fb_size))
            except Exception as e:
                print("    bytearray alloc fail: {}".format(e))
                break
        t0 = time.ticks_us(); gc.collect(); t_gc = time.ticks_diff(time.ticks_us(), t0)
        print("    gc.collect() with {:.0f}KB live: {:>7}us".format(
            len(t_bufs) * fb_size / 1024, t_gc))
        results.append(("T0 gc_collect(900KB)", t_gc, "live-load"))
    finally:
        del t_bufs
        gc.collect()
    print()

    # ── T1: fb_mode="dma" pool=1（期望內部 DMA，直接測 C 內部路徑） ──
    print("[T1] direct  fb_mode=dma pool=1  (heap_caps CAP_DMA 150KB)")
    fb1, k1 = _alloc_one(fb_size, "dma")
    print("    alloc -> {} ({} bytes)".format(k1, len(fb1)))
    _heap_diag("after-alloc")
    try:
        mv1 = fb1[:fb_size] if isinstance(fb1, memoryview) else memoryview(fb1)[:fb_size]
        _ramwr_start(spi, dc, w, h)
        us = _time_frames(fire_direct, spi, mv1, fb_size, 32768, frames=frames)
        print("    RESULT: {}us/frame".format(us))
        results.append(("T1 direct-dma-fb", us, k1))
    except Exception as e:
        print("    ERROR: {}".format(e))
    _free(fb1, k1)
    print()

    # ── T2: fb_mode="ram" pool=1（heap bytearray；heap 在 PSRAM → 走 C copy 路徑） ──
    print("[T2] direct  fb_mode=ram pool=1  (GC heap bytearray)")
    fb2, k2 = _alloc_one(fb_size, "ram")
    print("    alloc -> {} ({} bytes)".format(k2, len(fb2)))
    _heap_diag("after-alloc")
    try:
        mv2 = fb2[:fb_size] if isinstance(fb2, memoryview) else memoryview(fb2)[:fb_size]
        _ramwr_start(spi, dc, w, h)
        us = _time_frames(fire_direct, spi, mv2, fb_size, 32768, frames=frames)
        print("    RESULT: {}us/frame".format(us))
        results.append(("T2 direct-ram-fb", us, k2))
    except Exception as e:
        print("    ERROR: {}".format(e))
    print()

    # ── T4: 8KB 內部 bounce，refill + 直送（純測 C enqueue/wait 機械） ──
    print("[T4] direct  8KB internal bounce (pipelined CPU-copy + C single-path)")
    bounce, kb = _alloc_one(8192, "dma")
    print("    bounce alloc -> {}".format(kb))
    _heap_diag("after-bounce")
    try:
        # 來源用 PSRAM/heap 皆可（refill 是 CPU copy），照 T2 的 fb 當來源
        src = fb2 if 'fb2' in dir() and fb2 is not None else fb1
        mv_src = src[:fb_size] if isinstance(src, memoryview) else memoryview(src)[:fb_size]
        _ramwr_start(spi, dc, w, h)
        us = _time_frames(fire_bounce, spi, mv_src, fb_size, 8192, bounce=bounce, frames=frames)
        print("    RESULT: {}us/frame".format(us))
        results.append(("T4 direct-8k-bounce", us, kb))
    except Exception as e:
        print("    ERROR: {}".format(e))
    _free(bounce, kb)
    _free(fb2, k2)
    print()

    # ── summary ──
    print("=" * 60)
    print("Summary  (theory wire = {}us)".format(_THEORY_US))
    print("-" * 60)
    for name, us, kind in results:
        print("  {:<20} {:>7}us  (fb={})".format(name, us, kind))
    print("\n判讀:")
    print("  T1/T4 ≈ 15-20ms → C 分 chunk 機械正常，120ms 是記憶體設計問題（900KB pool）")
    print("  T1/T4 ≈ 120ms   → C 機械有問題，往 spi_bus.c 的 enqueue_raw/wait 查")
    print("  T2 慢但 T4 快   → C PSRAM copy 路徑序列化太慢，需 double-buffer 或 Python 換 bounce")
    print("done.")
