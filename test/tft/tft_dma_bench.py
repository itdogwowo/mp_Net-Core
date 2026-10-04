# tft_dma_bench.py
# TFT driver 包裝效能驗證 — 直送 bus vs driver 各 API
#
# 目標：確認「直送 spi bus」與「包成 TFT driver」之間有沒有性能落差。
# 排除 player 設計變數，純測 driver 包裝層損耗。
#
# 前置：soft reboot 讓 boot.py 完成硬體初始化（LCD 已在 bus 上）
# 用法：
#   import tft_dma_bench
#   tft_dma_bench.run()              # 預設 4 場景
#   tft_dma_bench.run(scenes="all")  # 同上
#   tft_dma_bench.run(scenes="AC")   # 只跑 A（直送）+ C（show_async）
#   tft_dma_bench.run(frames=200)    # 加大幀數
#
# 場景：
#   A. Direct bus      — spi.write() 直送 + pending>=2 退讓（test_jpeg_full 基準）
#   B. show_frame      — lcd.show_frame()（write_frame，每 chunk wait，預期最慢）
#   C. show_async      — lcd.show_async() + flush（write_data_async，預期 ≈ 基準）
#   D. show            — lcd.show()（write_data_async + flush）
#
# 每個 driver 場景測兩個子模式：
#   - per-frame set_window（真實 player 用法，每幀重設視窗）
#   - once set_window（只設一次，分離視窗設定損耗 vs 傳輸損耗）

import gc, time, random
from lib.sys.sys_bus import bus

# ═══════════════════ params ═══════════════════

_WARMUP    = 30
_RUNS      = 100
_CHUNK     = 32 * 1024


# ═══════════════════ framebuffer alloc ═══════════════════

def _alloc_fb(size, fb_mode="auto"):
    """
    依 fb_mode 分配 framebuffer（選擇權交回用戶，供二分實測）：
      "auto"  — SPIRAM → CAP_DMA → bytearray 三級 fallback（現狀）
      "psram" — 只試 CAP_SPIRAM，失敗 fallback bytearray
      "dma"   — 只試 CAP_DMA（內部 SRAM，DMA 可直讀），失敗 fallback bytearray
      "ram"   — 純 bytearray
    """
    try:
        import heap_caps
        if fb_mode in ("auto", "psram"):
            try:
                b = heap_caps.malloc(size, heap_caps.CAP_SPIRAM)
                if b is not None:
                    return b
            except Exception:
                pass
        if fb_mode in ("auto", "dma"):
            try:
                b = heap_caps.malloc(size, heap_caps.CAP_DMA)
                if b is not None:
                    return b
            except Exception:
                pass
    except Exception:
        pass
    return bytearray(size)


def _fill_noise(fb, seed=12345):
    """固定 seed noise（不每幀重生，純測傳輸）"""
    # MicroPython random 無 Random 類別，用 seed() + getrandbits
    try:
        random.seed(seed)
    except Exception:
        pass
    n = len(fb)
    for i in range(n):
        fb[i] = random.getrandbits(8) & 0xFF


# 6 個高對比純色（RGB565 大端）— 肉眼最容易判斷「有沒有送出去」
_SOLID_COLORS = [
    (0xF8, 0x00),  # RED
    (0x07, 0xE0),  # GREEN
    (0x00, 0x1F),  # BLUE
    (0xFF, 0xE0),  # YELLOW
    (0xFF, 0xFF),  # WHITE
    (0x00, 0x00),  # BLACK
]


def _fill_solid(fb, hi, lo):
    """用純色填滿 fb（RGB565 大端）"""
    n = len(fb)
    for i in range(0, n, 2):
        fb[i] = hi
        fb[i + 1] = lo


def _fill_rainbow(fb, w, h, phase):
    """會動的彩虹水平條紋 — 每幀 phase+1 讓條紋偏移，肉眼可見動畫"""
    # 6 色條紋，每幀偏移一行
    bands = [0xF800, 0x07E0, 0x001F, 0xFFE0, 0xF81F, 0xFFFF]
    row_bytes = w * 2
    for y in range(h):
        c = bands[(y + phase) % len(bands)]
        hi, lo = c >> 8, c & 0xFF
        base = y * row_bytes
        for x in range(0, row_bytes, 2):
            fb[base + x] = hi
            fb[base + x + 1] = lo


# ═══════════════════ heap 診斷 ═══════════════════

def _heap_diag():
    try:
        import heap_caps
        for name, caps in [("DMA    ", heap_caps.CAP_DMA),
                           ("SPIRAM ", heap_caps.CAP_SPIRAM)]:
            try:
                free = heap_caps.get_free_size(caps)
                total = heap_caps.get_total_size(caps)
                print("  {}: free={}KB / total={}KB".format(
                    name, free // 1024, total // 1024))
            except Exception:
                print("  {}: (query unavailable)".format(name))
    except Exception as e:
        print("  heap_caps unavailable: {}".format(e))


# ═══════════════════ Scene A: direct bus (baseline) ═══════════════════

def _ramwr_start(spi, dc, w, h):
    """設 CASET/PASET/RAMWR（只第一幀）— 照 test_jpeg_full"""
    dc.value(0); spi.write(bytearray([0x2A]))
    dc.value(1); spi.write(bytes([0, 0, (w - 1) >> 8, (w - 1) & 0xFF]))
    dc.value(0); spi.write(bytearray([0x2B]))
    dc.value(1); spi.write(bytes([0, 0, (h - 1) >> 8, (h - 1) & 0xFF]))
    spi.wait_all()
    dc.value(0); spi.write(bytearray([0x2C])); spi.wait_all()
    dc.value(1)


def _ramwr_fast(spi, dc):
    """只送 RAMWR（窗口已設）"""
    dc.value(0); t = spi.write(bytearray([0x2C]))
    spi.wait(t)
    dc.value(1)


def _dma_fire_direct(spi, mv, fb_size):
    """直送 bus：32KB 分段，pending>=2 才退讓（test_jpeg_full 手法）
    結尾 wait_all 確保這幀全部送完，避免下一幀 RAMWR 命令與本幀 data 在 queue 交錯。"""
    off = 0
    tids = []
    while off < fb_size:
        n = min(_CHUNK, fb_size - off)
        if spi.pending() >= 2:
            spi.wait(tids[0]); tids.pop(0)
        tid = spi.write(mv[off:off + n])
        if tid is not None:
            tids.append(tid)
        off += n
    # ⚠ 必須等本幀全部完成，下一幀的 _ramwr_fast 才能 dc.value(0) 寫命令
    spi.wait_all()


def scene_direct_bus(lcd, spi, dc, fb, w, h, fb_size):
    """場景 A：spi.write() 直送 + 每幀 _ramwr_fast（窗口起頭設一次）"""
    mv = memoryview(fb)[:fb_size] if not isinstance(fb, memoryview) else fb[:fb_size]
    _ramwr_start(spi, dc, w, h)

    # warmup
    for _ in range(_WARMUP):
        _ramwr_fast(spi, dc)
        _dma_fire_direct(spi, mv, fb_size)

    gc.collect()
    t0 = time.ticks_us()
    for _ in range(_RUNS):
        _ramwr_fast(spi, dc)
        _dma_fire_direct(spi, mv, fb_size)
    elapsed = time.ticks_diff(time.ticks_us(), t0)
    return elapsed // _RUNS


# ═══════════════════ Scene B/C/D: driver APIs ═══════════════════

def scene_driver(lcd, bus_adapter, fb, w, h, fb_size, mode, set_window_mode):
    """
    driver 場景通用跑法。
      mode: "show_frame" | "show_async" | "show" | "present"
      set_window_mode: "per_frame" | "once"（present 模式忽略，固定 begin_display 一次）
    """
    mv = memoryview(fb)[:fb_size] if not isinstance(fb, memoryview) else fb[:fb_size]

    # present 走新 pipeline API（begin_display 一次 + 每幀 present+wait）
    if mode == "present":
        lcd.begin_display()
        def _do_once():
            tids = lcd.present(mv)
            lcd.present_wait()
    else:
        win_set = (set_window_mode == "once")
        if win_set:
            lcd.set_window(0, 0, w - 1, h - 1)
        def _do_once():
            if not win_set:
                lcd.set_window(0, 0, w - 1, h - 1)
            if mode == "show_frame":
                lcd.show_frame(mv)
            elif mode == "show_async":
                lcd.show_async(mv)
                bus_adapter.flush()
            elif mode == "show":
                lcd.show(mv)

    # warmup
    for _ in range(_WARMUP):
        _do_once()
    if hasattr(bus_adapter, "flush"):
        bus_adapter.flush()

    gc.collect()
    t0 = time.ticks_us()
    for _ in range(_RUNS):
        _do_once()
    if hasattr(bus_adapter, "flush"):
        bus_adapter.flush()
    elapsed = time.ticks_diff(time.ticks_us(), t0)
    return elapsed // _RUNS


# ═══════════════════ reporter ═══════════════════

def _fmt(us):
    fps = 1e6 / us if us else 0
    return "{:>6}us  {:>5.0f}fps".format(us, fps)


def _delta(us_x, us_base):
    if not us_base:
        return ""
    pct = (us_x - us_base) * 100.0 / us_base
    sign = "+" if pct >= 0 else ""
    return "  ({}{:.1f}% vs baseline)".format(sign, pct)


# ═══════════════════ main ═══════════════════

def animate(mode="safe", frames=60, interval_ms=50, set_window_mode="per_frame",
            prebuilt=False, pool=6, fb_mode="auto"):
    """
    動畫驗證 — 肉眼確認資料真的送到螢幕。

    prebuilt:
      False（預設）— 邊生成邊 display：每幀 _fill_rainbow 後立刻送（生成時間混進去）
      True         — 預先建好 pool 幀再 display：循環播放預建幀，只測純傳輸

    mode:
      "safe"                                — tft_test_tool 手法（分 4K chunk + 每 chunk wait）
      "show_async" / "show" / "show_frame"  — driver 各 API
      "direct"                              — 直送 bus + queue 疊滿
      "present"                             — 新 pipeline API（begin_display + present）
    set_window_mode: "per_frame" | "once"
    interval_ms: 每幀間隔
    pool: prebuilt=True 時預建幀數
    fb_mode: "auto" | "psram" | "dma" | "ram"（fb 分配策略）
    """
    lcd = bus.get_service("lcd")
    if lcd is None:
        print("❌ lcd not on bus — run boot.py first")
        return

    bus_adapter = getattr(lcd, "_bus", None)
    spi = getattr(bus_adapter, "_spi", None) or getattr(lcd, "spi", None)
    dc = getattr(bus_adapter, "_dc", None) or getattr(lcd, "dc", None)

    w = int(bus.shared.get("tft_width", getattr(lcd, "width", 240)))
    h = int(bus.shared.get("tft_height", getattr(lcd, "height", 320)))
    bpp = int(getattr(lcd, "bytes_per_pixel", 2))
    fb_size = w * h * bpp

    is_direct = (mode == "direct")
    is_safe = (mode == "safe")
    win_set = (set_window_mode == "once") and not is_direct and not is_safe

    # ── 預先建好 pool 幀（prebuilt 模式）──
    prebuilt_fbs = []
    if prebuilt:
        print("prebuilt: building {} frames (fb_mode={})...".format(pool, fb_mode))
        for p in range(pool):
            fb_p = _alloc_fb(fb_size, fb_mode)
            _fill_rainbow(fb_p, w, h, p)
            prebuilt_fbs.append(fb_p)
        print("  {} frames ready ({}KB total)\n".format(
            pool, pool * fb_size // 1024))
    else:
        fb = _alloc_fb(fb_size, fb_mode)

    print("animate: mode={} prebuilt={} sw={} frames={} interval={}ms fb_mode={} ({}x{})".format(
        mode, prebuilt, set_window_mode, frames, interval_ms, fb_mode, w, h))
    print("看螢幕：正常應看到彩虹條紋滾動\n")

    if is_direct:
        if spi is None or dc is None:
            print("❌ direct 模式需要 spi/dc")
            return
        _ramwr_start(spi, dc, w, h)
    elif mode == "present":
        lcd.begin_display()
    elif win_set:
        lcd.set_window(0, 0, w - 1, h - 1)

    safe_chunk = bytearray(8192) if is_safe else None

    def _send_safe(mv):
        """完全照 tft_test_tool._write_solid：分 4K chunk + 每 chunk wait + flush"""
        lcd.set_window(0, 0, w - 1, h - 1)
        total = w * h
        written = 0
        cmv = memoryview(safe_chunk)
        while written < total:
            n = min(total - written, 4096)
            safe_chunk[:n * 2] = mv[written * 2:written * 2 + n * 2]
            hn = bus_adapter.write_data_async(cmv[:n * 2])
            if hn is not None:
                bus_adapter.wait(hn)
            written += n
        bus_adapter.flush()

    def _send_one(mv):
        if is_safe:
            _send_safe(mv)
        elif is_direct:
            _ramwr_fast(spi, dc)
            _dma_fire_direct(spi, mv, fb_size)
        elif mode == "present":
            # 新 pipeline API：begin_display 已設視窗，每幀 present + wait
            lcd.present(mv)
            lcd.present_wait()
        else:
            if not win_set:
                lcd.set_window(0, 0, w - 1, h - 1)
            if mode == "show_async":
                lcd.show_async(mv)
                bus_adapter.flush()
            elif mode == "show":
                lcd.show(mv)
            elif mode == "show_frame":
                lcd.show_frame(mv)

    try:
        send_times = []
        for i in range(frames):
            if prebuilt:
                # 循環播放預建幀（純傳輸，無生成時間）
                src = prebuilt_fbs[i % pool]
                mv = src[:fb_size] if isinstance(src, memoryview) else memoryview(src)[:fb_size]
            else:
                # 邊生成邊送（生成時間混進去）
                _fill_rainbow(fb, w, h, i % 6)
                mv = fb[:fb_size] if isinstance(fb, memoryview) else memoryview(fb)[:fb_size]

            t0 = time.ticks_us()
            _send_one(mv)
            send_us = time.ticks_diff(time.ticks_us(), t0)
            send_times.append(send_us)

            if i % 10 == 0:
                print("  frame {}/{}  last={}us".format(i, frames, send_us))
            time.sleep_ms(interval_ms)

        # 統計（跳過前 5 幀 warmup）
        warm = send_times[5:] if len(send_times) > 5 else send_times
        if warm:
            avg = sum(warm) // len(warm)
            mn = min(warm)
            mx = max(warm)
            fps_avg = 1e6 / avg if avg else 0
            print("\n  send stats (excl warmup): avg={}us min={}us max={}us | {:.0f}fps".format(
                avg, mn, mx, fps_avg))
    finally:
        # ⚠ 修復：只對 heap_caps 成功（memoryview）的 buffer 呼叫 heap_caps.free，
        # fallback bytearray 是 GC heap 物件，free 會腐蝕 heap → 硬當機。
        for fb_p in prebuilt_fbs:
            if isinstance(fb_p, memoryview):
                try:
                    import heap_caps
                    heap_caps.free(fb_p)
                except Exception:
                    pass
        if not prebuilt and isinstance(fb, memoryview):
            try:
                import heap_caps
                heap_caps.free(fb)
            except Exception:
                pass
        if safe_chunk is not None:
            del safe_chunk
    print("done.")


def run(frames=None, scenes="all", fb_mode="auto"):
    """
    frames: 每場景計時幀數（預設 _RUNS=100）
    scenes: "all" | 字串組合 "A"/"B"/"C"/"D"/"E"（如 "AC" 只跑 A+C）
    fb_mode: "auto" | "psram" | "dma" | "ram"（fb 分配策略）
    """
    global _RUNS
    if frames:
        _RUNS = int(frames)

    lcd = bus.get_service("lcd")
    if lcd is None:
        print("❌ lcd not on bus — run boot.py first")
        return

    bus_adapter = getattr(lcd, "_bus", None)
    spi = getattr(bus_adapter, "_spi", None) or getattr(lcd, "spi", None)
    dc = getattr(bus_adapter, "_dc", None) or getattr(lcd, "dc", None)

    if spi is None or dc is None:
        print("❌ cannot access raw spi/dc — driver path only")
        spi = dc = None

    w = int(bus.shared.get("tft_width", getattr(lcd, "width", 240)))
    h = int(bus.shared.get("tft_height", getattr(lcd, "height", 320)))
    bpp = int(getattr(lcd, "bytes_per_pixel", 2))
    fb_size = w * h * bpp

    want = set(scenes.upper()) if scenes != "all" else set("ABCDE")

    print("=" * 64)
    print("TFT Driver DMA Benchmark")
    print("=" * 64)
    print("panel: {}x{} {}bpp  fb={}KB  frames={}  fb_mode={}".format(
        w, h, bpp, fb_size // 1024, _RUNS, fb_mode))
    print("driver: {}  spi_has_pending: {}".format(
        bus.shared.get("tft_driver", "?"),
        hasattr(spi, "pending") if spi else False))
    _heap_diag()
    print("-" * 64)

    fb = _alloc_fb(fb_size, fb_mode)
    _fill_noise(fb)
    fb_kind = "PSRAM" if (isinstance(fb, memoryview) and not isinstance(fb, bytearray)) else "bytearray"
    print("fb allocated: {} bytes ({})\n".format(len(fb), fb_kind))

    base_us = None
    results = []

    # ── A: direct bus baseline ──
    if "A" in want and spi is not None and dc is not None:
        print("[A] Direct bus (baseline)")
        try:
            us = scene_direct_bus(lcd, spi, dc, fb, w, h, fb_size)
            base_us = us
            print("    {}".format(_fmt(us)))
            results.append(("A-direct", us))
        except Exception as e:
            print("    ERROR: {}".format(e))
        print()

    # ── B: show_frame (write_frame, per-chunk wait) ──
    if "B" in want:
        print("[B] driver show_frame  (write_frame: per-chunk wait)")
        for sw_mode in ("per_frame", "once"):
            try:
                us = scene_driver(lcd, bus_adapter, fb, w, h, fb_size,
                                  "show_frame", sw_mode)
                print("    {:<11} {}{}".format(
                    sw_mode + ":", _fmt(us), _delta(us, base_us)))
                results.append(("B-show_frame-" + sw_mode, us))
            except Exception as e:
                print("    {:<11} ERROR: {}".format(sw_mode + ":", e))
        print()

    # ── C: show_async (write_data_async, non-block) ──
    if "C" in want:
        print("[C] driver show_async  (write_data_async: non-block)")
        for sw_mode in ("per_frame", "once"):
            try:
                us = scene_driver(lcd, bus_adapter, fb, w, h, fb_size,
                                  "show_async", sw_mode)
                print("    {:<11} {}{}".format(
                    sw_mode + ":", _fmt(us), _delta(us, base_us)))
                results.append(("C-show_async-" + sw_mode, us))
            except Exception as e:
                print("    {:<11} ERROR: {}".format(sw_mode + ":", e))
        print()

    # ── D: show (write_data_async + flush) ──
    if "D" in want:
        print("[D] driver show  (write_data_async + flush)")
        for sw_mode in ("per_frame", "once"):
            try:
                us = scene_driver(lcd, bus_adapter, fb, w, h, fb_size,
                                  "show", sw_mode)
                print("    {:<11} {}{}".format(
                    sw_mode + ":", _fmt(us), _delta(us, base_us)))
                results.append(("D-show-" + sw_mode, us))
            except Exception as e:
                print("    {:<11} ERROR: {}".format(sw_mode + ":", e))
        print()

    # ── E: present (begin_display + present + present_wait, 新 pipeline API) ──
    if "E" in want:
        print("[E] driver present  (begin_display + DMA chunk queue)")
        try:
            us = scene_driver(lcd, bus_adapter, fb, w, h, fb_size, "present", "once")
            print("    {:<11} {}{}".format(
                "pipeline:", _fmt(us), _delta(us, base_us)))
            results.append(("E-present", us))
        except Exception as e:
            print("    {:<11} ERROR: {}".format("pipeline:", e))
        print()

    # ── summary ──
    print("=" * 64)
    print("Summary (sorted by us/frame)")
    print("-" * 64)
    for name, us in sorted(results, key=lambda x: x[1]):
        marker = "  ★ baseline" if name == "A-direct" else ""
        print("  {:<28} {}{}".format(name, _fmt(us), marker))

    # 釋放 fb（⚠ 只對 heap_caps 成功者 free；bytearray fallback 留給 GC）
    if isinstance(fb, memoryview):
        try:
            import heap_caps
            heap_caps.free(fb)
        except Exception:
            pass
    gc.collect()
    print("\ndone.")
