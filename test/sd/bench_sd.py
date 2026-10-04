# -*- coding: utf-8 -*-
"""SD 卡速度基準測試 (MicroPython / ESP32)

測試三條路徑的讀取速度：
  F1. FAT (os.open/readinto)     — 標準 MicroPython FAT
  F2. SD-raw (fast_io.Storage)   — 繞過 FAT，直接 sector 讀寫
  F3. fs_manager (自動降級鏈)    — 優先 SD-raw，失敗降級 FAT
  F4. DMA vs bytearray buffer   — 測重構核心:readblocks 進 DMA 記憶體 vs 普通 RAM
  F5. StreamReader 預讀管線      — 雙/三緩衝預讀的串流吞吐

用法:
  import bench_sd
  bench_sd.run()           # 完整測試矩陣（含 F1~F5）
  bench_sd.run_quick()     # 快速測試 (只測默認 buffer 的 F1~F3)
  bench_sd.run_dma()       # 只跑 F4: DMA vs bytearray 對比
  bench_sd.run_stream()    # 只跑 F5: StreamReader 預讀管線
"""

import gc, time, os
from lib.sys.sys_bus import bus

# ── 測試參數 ──
TEST_SIZE = 4 * 1024 * 1024   # 4 MB 總讀取量 (可調整 2/4/8 MB)
BUF_SIZES = [512, 2048, 4096, 8192, 16384, 32768, 65536]
FAT_PATH = "/sd/_sd_bench_test.bin"
RAW_NAME = "_sd_bench_raw"


def _fmt_bytes(n):
    if n >= 1048576:
        return "{:.1f} MB".format(n / 1048576)
    if n >= 1024:
        return "{:.0f} KB".format(n / 1024)
    return "{} B".format(n)


def _mb_s(total_bytes, elapsed_ms):
    if elapsed_ms <= 0:
        elapsed_ms = 1
    return (total_bytes * 1000) / (elapsed_ms * 1048576)


def _ensure_test_file_fat(path, size):
    """建立 FAT 測試檔案 (如果不存在或大小不對)"""
    try:
        st = os.stat(path)
        if st[6] == size:
            return True
    except OSError:
        pass
    print("  建立 FAT 測試檔 {} ({})...".format(path, _fmt_bytes(size)))
    try:
        buf = bytearray(32768)
        with open(path, "wb") as f:
            written = 0
            while written < size:
                n = min(len(buf), size - written)
                f.write(buf[:n])
                written += n
        return True
    except Exception as e:
        print("  ⚠️ 建立 FAT 測試檔失敗:", e)
        return False


def _is_incomplete(storage, name):
    """檢查檔案是否標記為 incomplete（CRC == FFFFFFFF，寫入中斷）。"""
    try:
        entry = storage._alloc.find(name)
        if entry is None:
            return False
        crc = entry[2] if len(entry) >= 3 else None
        return crc == "FFFFFFFF"
    except Exception:
        return False


def _ensure_test_file_raw(storage, name, size):
    """建立 SD-raw 測試檔案（已存在且完整才重用，否則刪除重建）"""
    existing = storage.list_files()
    if name in existing:
        info = existing[name]
        # bytes 夠大「且」非 incomplete 才可直接重用
        if info["bytes"] >= size and not _is_incomplete(storage, name):
            return True
        # 既有的壞了或不夠大，先刪
        try:
            storage.remove(name)
        except Exception:
            pass
    print("  建立 raw 測試檔 {} ({})...".format(name, _fmt_bytes(size)))
    try:
        buf = bytearray(32768)
        storage.write_begin(name, size)
        written = 0
        while written < size:
            n = min(len(buf), size - written)
            storage.write(buf[:n])
            written += n
        storage.write_end()
        return True
    except Exception as e:
        print("  ⚠️ 建立 raw 測試檔失敗:", e)
        return False


# ═══════════════════════════════════════════════════════════════
#  F1: FAT 讀取測試
# ═══════════════════════════════════════════════════════════════

def _fat_read_bench(path, buf_size, total_bytes):
    """FAT readinto 基準"""
    buf = bytearray(buf_size)
    gc.collect()
    t0 = time.ticks_ms()
    total = 0
    try:
        with open(path, "rb") as f:
            while total < total_bytes:
                n = f.readinto(buf)
                if n <= 0:
                    break
                total += n
    except Exception as e:
        print("    FAT read err:", e)
        return 0, 0
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    return total, elapsed


# ═══════════════════════════════════════════════════════════════
#  F2: SD-raw (fast_io.Storage) 讀取測試
# ═══════════════════════════════════════════════════════════════

def _raw_read_bench(storage, name, buf_size, total_bytes):
    """fast_io Storage read_into 基準"""
    buf = bytearray(buf_size)
    gc.collect()
    try:
        size = storage.read_begin(name)
    except Exception as e:
        print("    raw read_begin err:", e)
        # read_begin 失敗時確保 _r_open 不卡住（雙保險，根源已修）
        if storage._r_open:
            storage.read_end()
        return 0, 0
    t0 = time.ticks_ms()
    total = 0
    while total < total_bytes and total < size:
        n = storage.read_into(buf)
        if n <= 0:
            break
        total += n
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    storage.read_end()
    return total, elapsed


# ═══════════════════════════════════════════════════════════════
#  F3: fs_manager 統一讀取測試
# ═══════════════════════════════════════════════════════════════

def _fs_read_bench(fs, path, total_bytes):
    """fs_manager.read() 整個讀入 benchmark"""
    gc.collect()
    t0 = time.ticks_ms()
    try:
        data = fs.read(path)
    except Exception as e:
        print("    fs read err:", e)
        return 0, 0
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    total = len(data) if data else 0
    return total, elapsed


# ═══════════════════════════════════════════════════════════════
#  格式化輸出
# ═══════════════════════════════════════════════════════════════

def _print_header(title):
    print("\n" + "=" * 62)
    print("  {}  (total: {})".format(title, _fmt_bytes(TEST_SIZE)))
    print("=" * 62)
    print("  {:>12s}  {:>10s}  {:>10s}  {:>12s}".format(
        "buf_size", "bytes_read", "elapsed_ms", "MB/s"))
    print("  " + "-" * 58)


def _print_row(buf_size, total, elapsed, mb_s):
    bar = ""
    if mb_s >= 8:
        bar = "  ████████"
    elif mb_s >= 6:
        bar = "  ██████"
    elif mb_s >= 4:
        bar = "  ████"
    elif mb_s >= 2:
        bar = "  ██"
    print("  {:>8s}  {:>10s}  {:>8d} ms  {:>8.2f} MB/s{}".format(
        _fmt_bytes(buf_size), _fmt_bytes(total), elapsed, mb_s, bar))


# ═══════════════════════════════════════════════════════════════
#  主測試函數
# ═══════════════════════════════════════════════════════════════

def run(quick=False):
    """完整基準測試"""
    print("\n" + "╔" + "=" * 60 + "╗")
    print("║  SD Card Speed Benchmark — MicroPython FAT vs SD-raw  ║")
    print("╚" + "=" * 60 + "╝")

    # ── 檢查 SD 卡 ──
    sd = bus.get_service("sd_raw")
    if sd is None:
        print("\n❌ 無法存取 SD 卡 (sd_raw 未註冊到 bus)")
        err = bus.shared.get("sd_error")
        if err:
            print("    原因: {}".format(err))
            print("    開機 log 中 [sd_drv] ❌ 的訊息與 traceback 有完整錯誤。")
        else:
            print("    開機時未記錄到錯誤 — 確認 config.json 的 SDcard.enable=1，")
            print("    且 boot.py 有呼叫 init_sd(bus)。")
        # 提供可於 REPL 直接執行的重現指令 (依目前設定)
        _cfg = bus.shared.get("SDcard") or {}
        _g = _cfg.get("GPIO", {}) or {}
        _c = _cfg.get("config", {}) or {}
        if _g.get("sck") is not None:
            print("    於 REPL 重現完整錯誤:")
            print("      import machine")
            print("      machine.SDCard(slot={}, width={}, sck={}, cmd={}, data={}, freq={})".format(
                _c.get("slot", 0), _c.get("width", 4), _g["sck"], _g["cmd"],
                _g.get("data"), _c.get("freq", 20000000)))
        return

    sector_size = sd.info()[1]
    total_sectors = sd.info()[0]
    cap_mb = (total_sectors * sector_size) // 1048576
    print("SD 卡: {} sectors × {}B = ~{} MB".format(total_sectors, sector_size, cap_mb))

    # ── 測試前檢查分配檔案 ──
    try:
        os.stat("/sd/alloc.json")
        print("alloc.json: 已存在")
    except OSError:
        print("alloc.json: 尚未建立 (將初始化)")

    # ── 初始化 Storage ──
    try:
        from lib.fast_io import Storage
        raw_storage = Storage()
        print("fast_io Storage: ✅ 就緒 (DMA buf: {} bytes)".format(raw_storage._buf_size))
    except Exception as e:
        print("fast_io Storage: ❌", e)
        raw_storage = None

    # ── 初始化 fs_manager ──
    try:
        from lib.fs_manager import fs
        print("fs_manager: ✅ 就緒")
    except Exception as e:
        print("fs_manager: ❌", e)
        fs = None

    # ── 準備測試檔案 ──
    print("\n── 準備測試檔案 ──")
    buf_sizes_test = BUF_SIZES if not quick else [16384, 32768]

    if not _ensure_test_file_fat(FAT_PATH, TEST_SIZE):
        print("❌ 無法建立 FAT 測試檔，跳過 FAT 測試")
    else:
        gc.collect()
        _print_header("F1: FAT (os.open/readinto)")
        for bs in buf_sizes_test:
            try:
                total, elapsed = _fat_read_bench(FAT_PATH, bs, TEST_SIZE)
            except MemoryError:
                total, elapsed = 0, 0
                print("  {:>8s}  (OOM)".format(_fmt_bytes(bs)))
                gc.collect()
                continue
            if total > 0:
                _print_row(bs, total, elapsed, _mb_s(total, elapsed))
            else:
                print("  {:>8s}  (read err)".format(_fmt_bytes(bs)))
            gc.collect()

    if raw_storage:
        gc.collect()
        if _ensure_test_file_raw(raw_storage, RAW_NAME, TEST_SIZE):
            _print_header("F2: SD-raw (fast_io.Storage / read_into)")
            for bs in buf_sizes_test:
                try:
                    total, elapsed = _raw_read_bench(raw_storage, RAW_NAME, bs, TEST_SIZE)
                except MemoryError:
                    total, elapsed = 0, 0
                    print("  {:>8s}  (OOM)".format(_fmt_bytes(bs)))
                    gc.collect()
                    continue
                if total > 0:
                    _print_row(bs, total, elapsed, _mb_s(total, elapsed))
                else:
                    print("  {:>8s}  (read err)".format(_fmt_bytes(bs)))
                gc.collect()

            # F2.5: read_all 測試
            gc.collect()
            try:
                t0 = time.ticks_ms()
                data = raw_storage.read_all(RAW_NAME)
                elapsed = time.ticks_diff(time.ticks_ms(), t0)
                if data:
                    print("\n  SD-raw read_all(): {} in {} ms → {:.2f} MB/s".format(
                        _fmt_bytes(len(data)), elapsed, _mb_s(len(data), elapsed)))
            except Exception as e:
                print("  SD-raw read_all err:", e)

    # F3: fs_manager 統一讀取 (raw 路徑)
    if fs and raw_storage:
        gc.collect()
        _print_header("F3: fs_manager.read() — SD-raw 路徑")
        try:
            # fs_manager 需要 /sd 前綴
            total, elapsed = _fs_read_bench(fs, "/sd/" + RAW_NAME, TEST_SIZE)
            if total > 0:
                _print_row(0, total, elapsed, _mb_s(total, elapsed))
        except Exception as e:
            print("  fs_manager raw err:", e)

    # F3b: fs_manager 讀 FAT 路徑
    if fs:
        gc.collect()
        _print_header("F3b: fs_manager.read() — FAT 路徑")
        try:
            total, elapsed = _fs_read_bench(fs, FAT_PATH, TEST_SIZE)
            if total > 0:
                _print_row(0, total, elapsed, _mb_s(total, elapsed))
        except Exception as e:
            print("  fs_manager fat err:", e)

    # ── 寫入速度快速測試 ──
    if raw_storage:
        gc.collect()
        print("\n── 寫入速度快速測試 (SD-raw, 1 MB) ──")
        wbuf = bytearray(32768)
        wsize = 1 * 1024 * 1024
        try:
            raw_storage.write_begin("_bench_tmp", wsize)
            t0 = time.ticks_ms()
            written = 0
            while written < wsize:
                n = min(len(wbuf), wsize - written)
                raw_storage.write(wbuf[:n])
                written += n
            raw_storage.write_end()
            elapsed = time.ticks_diff(time.ticks_ms(), t0)
            print("  寫入 {}: {} ms → {:.2f} MB/s".format(
                _fmt_bytes(wsize), elapsed, _mb_s(wsize, elapsed)))
            raw_storage.remove("_bench_tmp")
        except Exception as e:
            print("  寫入測試失敗:", e)

    # ── F4: DMA vs bytearray buffer（只在完整模式跑）──
    if not quick:
        try:
            run_dma()
        except Exception as e:
            print("  F4 測試失敗:", e)

    # ── F5: StreamReader 預讀管線（只在完整模式跑）──
    if not quick:
        try:
            run_stream()
        except Exception as e:
            print("  F5 測試失敗:", e)

    # ── 記憶體狀態 ──
    gc.collect()
    print("\n── 記憶體 ──")
    print("  GC free: {} bytes".format(gc.mem_free()))
    print("  GC alloc: {} bytes".format(gc.mem_alloc()))

    print("\n" + "=" * 62)
    print("  測試完成。")

    # 清理
    try:
        os.remove(FAT_PATH)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  F4: DMA buffer vs bytearray buffer（測重構核心效果）
# ═══════════════════════════════════════════════════════════════
# 重構把 Storage 內部 I/O buffer 換成 alloc_dma 配的 CAP_DMA 記憶體。
# 此測試直接打 sd_raw.readblocks，比較「DMA buffer」與「普通 bytearray」
# 在同一個 sector 讀取路徑上的吞吐差異——這才看得出重構的實際效果。

def _readblocks_bench(sd, start_sector, n_sectors, buf, rounds):
    """連續 readblocks rounds 次，回傳 (總位元組, 耗時 ms)。"""
    gc.collect()
    t0 = time.ticks_ms()
    total = 0
    sec = start_sector
    for _ in range(rounds):
        sd.readblocks(sec, buf)
        total += len(buf)
        sec += n_sectors
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    return total, elapsed


def _bench_dma_vs_bytearray(sd, storage, name, buf_size):
    """對單一 buf_size 測 DMA buffer vs bytearray buffer。"""
    sector_size = sd.info()[1]
    spc = buf_size // sector_size  # 每 buffer 含多少 sector
    if spc <= 0:
        return None
    try:
        total_size = storage.read_begin(name)
    except Exception as e:
        print("    read_begin err:", e)
        if storage._r_open:
            storage.read_end()
        return None
    # read_end 不重設 _r_sector，故可於 read_end 後安全取用起始 sector
    start_sector = storage._r_sector
    storage.read_end()

    rounds = max(1, (total_size // buf_size) // 2)  # 跑約一半檔案長度
    rounds = min(rounds, 256)  # 上限避免太久

    # 配兩種 buffer
    from lib.sys.buffer_hub import alloc_dma, free_dma
    dma_buf, is_dma = alloc_dma(buf_size)
    ba_buf = bytearray(buf_size)

    results = {"dma": None, "bytearray": None, "is_dma": is_dma}

    # 先跑 DMA buffer
    if is_dma:
        total, elapsed = _readblocks_bench(sd, start_sector, spc, dma_buf, rounds)
        results["dma"] = (total, elapsed) if total > 0 else None
        free_dma(dma_buf, True)
    else:
        # heap_caps 不可用（例如 PC 測試），DMA 路徑無從測起
        pass

    # 再跑 bytearray buffer
    total, elapsed = _readblocks_bench(sd, start_sector, spc, ba_buf, rounds)
    results["bytearray"] = (total, elapsed) if total > 0 else None

    return results


def run_dma(buf_sizes=None, total_hint=2 * 1024 * 1024):
    """F4: DMA buffer vs bytearray buffer 對比測試。

    直接打 sd_raw.readblocks，繞過 Storage 上層的 copy，純量「資料進 buffer」
    的吞吐。DMA buffer 配在內部 SRAM（CAP_DMA），bytearray 可能落在 PSRAM，
    差異主要來自週邊 DMA 能否直送該記憶體區域。
    """
    print("\n" + "╔" + "=" * 60 + "╗")
    print("║  F4: DMA buffer vs bytearray buffer (readblocks)        ║")
    print("╚" + "=" * 60 + "╝")

    sd = bus.get_service("sd_raw")
    if sd is None:
        print("❌ 無法存取 SD 卡")
        return

    if buf_sizes is None:
        buf_sizes = [512, 4096, 16384, 32768]

    try:
        from lib.fast_io import Storage
        from lib.sys.buffer_hub import alloc_dma
        storage = Storage()
    except Exception as e:
        print("❌ Storage/alloc_dma 初始化失敗:", e)
        return

    # 建一個夠大的測試檔（複用既有）
    if not _ensure_test_file_raw(storage, RAW_NAME, total_hint):
        print("❌ 無法建立 raw 測試檔")
        return

    # 確認 DMA 是否可用
    _probe, probe_dma = alloc_dma(512)
    print("heap_caps DMA: {}".format("✅ 可用" if probe_dma else "❌ 不可用（只測 bytearray）"))
    del _probe

    print("\n  {:>10s}  {:>18s}  {:>18s}  {:>10s}".format(
        "buf_size", "DMA MB/s", "bytearray MB/s", "倍率"))
    print("  " + "-" * 62)

    for bs in buf_sizes:
        r = _bench_dma_vs_bytearray(sd, storage, RAW_NAME, bs)
        if r is None:
            print("  {:>8s}  (測試失敗)".format(_fmt_bytes(bs)))
            continue
        dma_mb = _mb_s(*r["dma"]) if r["dma"] else 0
        ba_mb = _mb_s(*r["bytearray"]) if r["bytearray"] else 0
        ratio = (dma_mb / ba_mb) if ba_mb > 0 and r["dma"] else 0
        dma_str = "{:.2f}".format(dma_mb) if r["dma"] else "  n/a"
        ba_str = "{:.2f}".format(ba_mb) if r["bytearray"] else "  n/a"
        ratio_str = "{:.2f}x".format(ratio) if ratio > 0 else "-"
        print("  {:>8s}  {:>18s}  {:>18s}  {:>10s}".format(
            _fmt_bytes(bs), dma_str, ba_str, ratio_str))
        gc.collect()

    print("\n  解讀: 倍率 > 1.0 表示 DMA buffer 較快；" 
          "< 1.0 表示反而較慢（可能 buffer 過大或 PSRAM 直送已最佳化）。")


# ═══════════════════════════════════════════════════════════════
#  F5: StreamReader 預讀管線（雙/三緩衝串流吞吐）
# ═══════════════════════════════════════════════════════════════
# 模擬串流解碼場景：一端 feed（讀 SD 進 ring slot），另一端 next/release
# （消費 slot）。量的是「含預讀重疊」的端到端吞吐，而非單純 readblocks。

def _stream_sequential(sr, total_bytes):
    """單執行緒順序 feed→next→release，量純吞吐（無重疊）。"""
    buf_size = sr.chunk_bytes
    gc.collect()
    t0 = time.ticks_ms()
    consumed = 0
    sec = sr._r_sector
    rem_sectors = sr._r_cnt
    while consumed < total_bytes and rem_sectors > 0:
        if not sr.feed(sec):
            break
        v = sr.next()
        if v is None:
            break
        consumed += len(v)
        sr.release()
        sec += sr.chunk_sectors
        rem_sectors -= sr.chunk_sectors
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    return consumed, elapsed


def run_stream(buf_size=16384, n_bufs=2, total_hint=2 * 1024 * 1024):
    """F5: StreamReader 預讀管線吞吐。

    測兩件事：
      (a) 順序模式：喬執行緒 feed→next→release，無重疊（下限吞吐）
      (b) 預填模式：先 feed 滿所有 buffer，再一次次 next/release（測 buffer 命中）
    """
    print("\n" + "╔" + "=" * 60 + "╗")
    print("║  F5: StreamReader 預讀管線 (buf={}B × {})              ║".format(
        buf_size, n_bufs))
    print("╚" + "=" * 60 + "╝")

    sd = bus.get_service("sd_raw")
    if sd is None:
        print("❌ 無法存取 SD 卡")
        return

    try:
        from lib.fast_io import StreamReader
        from lib.fs_manager import fs
    except Exception as e:
        print("❌ 模組載入失敗:", e)
        return

    # 用 alloc 找檔案起始 sector；沒有就建一個
    try:
        alloc = fs.get_service("alloc") if hasattr(fs, "get_service") else None
    except Exception:
        alloc = None

    # 退化做法：直接用 Storage 建檔拿 sector，再讓 StreamReader 從該 sector 開始讀
    try:
        from lib.fast_io import Storage
        storage = Storage()
        if not _ensure_test_file_raw(storage, RAW_NAME, total_hint):
            print("❌ 無法建立測試檔")
            return
        # 取檔案起始 sector 與 sector 數
        files = storage.list_files()
        if RAW_NAME not in files:
            print("❌ 測試檔建立後仍找不到")
            return
        entry = files[RAW_NAME]
        start_sector = entry.get("sector", 0) if isinstance(entry, dict) else entry[0]
        n_sectors = entry.get("count", 0) if isinstance(entry, dict) else entry[1]
    except Exception as e:
        print("❌ 取檔案 sector 失敗:", e)
        return

    if n_sectors <= 0:
        print("❌ 測試檔 sector 數為 0")
        return

    try:
        sr = StreamReader(buf_size=buf_size, n_bufs=n_bufs)
    except Exception as e:
        print("❌ StreamReader 建立失敗:", e)
        return

    sr._r_sector = start_sector
    sr._r_cnt = n_sectors
    sr._started = True
    sr._eof = False

    print("\n  DMA buffers: {}".format(
        "{}/{}".format(sum(sr._hc), len(sr._hc)) if hasattr(sr, "_hc") else "n/a"))

    # (a) 順序模式
    total, elapsed = _stream_sequential(sr, total_hint)
    if total > 0:
        print("  (a) 順序 feed→next→release: {} in {} ms → {:.2f} MB/s".format(
            _fmt_bytes(total), elapsed, _mb_s(total, elapsed)))
    else:
        print("  (a) 順序模式測試失敗")

    # (b) 預填模式：先餵滿所有 slot，再消費
    gc.collect()
    try:
        sr2 = StreamReader(buf_size=buf_size, n_bufs=n_bufs)
        sr2._r_sector = start_sector
        sr2._r_cnt = n_sectors
        sr2._started = True
        sr2._eof = False
        t0 = time.ticks_ms()
        # 預填
        filled = 0
        sec = start_sector
        rem = n_sectors
        while filled < n_bufs and rem > 0:
            if not sr2.feed(sec):
                break
            filled += 1
            sec += sr2.chunk_sectors
            rem -= sr2.chunk_sectors
        # 消費 + 持續補
        consumed = 0
        while consumed < total_hint and rem > 0:
            v = sr2.next()
            if v is None:
                break
            consumed += len(v)
            sr2.release()
            if rem > 0:
                sr2.feed(sec)
                sec += sr2.chunk_sectors
                rem -= sr2.chunk_sectors
        elapsed = time.ticks_diff(time.ticks_ms(), t0)
        if consumed > 0:
            print("  (b) 預填+持續補:           {} in {} ms → {:.2f} MB/s".format(
                _fmt_bytes(consumed), elapsed, _mb_s(consumed, elapsed)))
        sr2.close()
    except Exception as e:
        print("  (b) 預填模式測試失敗:", e)

    sr.close()
    print("\n  解讀: (b) 應 ≥ (a)，差距代表預讀重疊的收益。"
          "若 DMA buffer 命中，吞吐應接近 F4 的 DMA 數字。")


def run_quick():
    """快速測試: 只測最相關的 buffer 大小"""
    global TEST_SIZE
    TEST_SIZE = 2 * 1024 * 1024
    run(quick=True)


# 可直接 import 後呼叫 run() 或作為獨立腳本執行
if __name__ == "__main__":
    run()
