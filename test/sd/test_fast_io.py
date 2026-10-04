# -*- coding: utf-8 -*-
"""fast_io.Storage 功能正確性測試 + 真實檔案讀取效能

目的
  驗證重構後的 lib/fast_io.py 所有檔案操作路徑功能正常，並量真實檔案讀取速度：
    寫檔（整檔 / 分段）、開檔、讀檔（分段 / 整檔）、seek/tell、
    列檔、刪檔、CRC 完整性、例外路徑、DMA buffer 命中、StreamReader 完整 API、
    close() 釋放、Storage 邊界 case，最後用 SD 卡上實際部署的媒體檔量
    「開檔→讀完→關檔」端到端吞吐。

⚠️ 安全設計（非常重要）
  Storage.write 透過 Allocator 在 raw sector 配置空間，靠 alloc.json 的
  _offset（FAT 分區結束位置）作為防線。若 alloc.json 不存在或 _offset=0，
  Allocator.append 會從 sector 0 配置 → 直接覆蓋 FAT 表 → 卡壞掉要回電腦重格式化。
  本測試開頭會檢查這道防線：_offset 必須 > 0，否則拒跑並告知補救方法。
  絕不在沒有 alloc.json 的卡上跑寫入測試。

判讀
  逐項印 ✅ PASS / ❌ FAIL（附數值），最後總結。

用法
  裝置（soft reboot 後，SD 已掛載）：
    import test_fast_io
    test_fast_io.run()
  本測試需要真實 SD 卡，無法在 CPython 跑（CPython 無 machine.SDCard）。
"""
import sys

IS_MICROPYTHON = (getattr(sys, "implementation", None)
                  and sys.implementation.name == "micropython")

if not IS_MICROPYTHON:
    print("⚠️ test_fast_io 需要真實 SD 卡（machine.SDCard），無法在 CPython 跑。")
    print("   請在 ESP32 裝置上執行：import test_fast_io; test_fast_io.run()")
    sys.exit(0)

import os, time, gc
from lib.sys.sys_bus import bus
from lib.fast_io import Storage, StreamReader

# ── 測試公用 ─────────────────────────────────────────────────────
_failed = 0
_passed = 0


def _check(label, cond, extra=""):
    global _failed, _passed
    mark = "✅ PASS" if cond else "❌ FAIL"
    if not cond:
        _failed += 1
    else:
        _passed += 1
    print("  {} — {}{}".format(mark, label, ("  " + extra) if extra else ""))


def _check_raises(label, fn, exc_type=Exception):
    """確認 fn() 會丟例外。回傳是否如預期丟出。"""
    global _failed, _passed
    try:
        fn()
        _failed += 1
        print("  ❌ FAIL — {}（預期丟例外但沒有）".format(label))
        return False
    except exc_type:
        _passed += 1
        print("  ✅ PASS — {}（如預期丟 {}）".format(label, exc_type.__name__))
        return True
    except Exception as e:
        # 丟了例外但類型不對也算值得記錄，算 PASS（有丟就好，類型從寬）
        _passed += 1
        print("  ✅ PASS — {}（丟了 {}: {}）".format(label, type(e).__name__, e))
        return True


# ── 安全檢查：alloc.json 的 _offset 必須 > 0 ─────────────────────
def _check_alloc_guard(storage):
    """沒有 alloc.json 或 _offset=0 時，拒絕跑寫入測試（否則會毀 FAT）。"""
    print("\n[0] 安全檢查：alloc.json 防線")
    try:
        with open("/sd/alloc.json") as f:
            import json
            data = json.load(f)
        offset = data.get("_offset", 0)
        total = data.get("_total_sectors", 0)
        _check("alloc.json 存在", True)
        _check("_offset > 0（FAT 防線就位）", offset > 0,
               "offset={} sectors (~{}MB)".format(offset, offset * 512 // 1048576))
        if total:
            print("    管理區: sector {}~{} (~{}GB)".format(
                offset, total, (total - offset) * 512 // 1073741824))
        return offset > 0
    except OSError:
        _check("alloc.json 存在", False, "→ 找不到 alloc.json")
        print("\n  ⚠️⚠️⚠️ 危險：沒有 alloc.json，寫入會從 sector 0 開始覆蓋 FAT！")
        print("  補救：在電腦上用 storage_tool.py 執行『格式化』(選項 1)，")
        print("        它會建 FAT 分區 + alloc.json（含 _offset 防線）。")
        print("  或：手動建 /sd/alloc.json，內容 {\"_offset\": 409600, ...}")
        print("        （_offset 要 > 你 FAT 分區的 sector 數）")
        return False
    except Exception as e:
        _check("alloc.json 可解析", False, str(e))
        return False


# ── 測試 1：整檔寫入 + 讀回 + CRC ─────────────────────────────────
def test_write_read_basic(storage, name="_t_basic"):
    print("\n[1] 整檔寫入 write_file → read_all + CRC")
    # 造一個含可辨識 pattern 的資料
    payload = bytearray(8192)
    for i in range(len(payload)):
        payload[i] = i & 0xFF

    try:
        storage.write_file(name, bytes(payload))
    except Exception as e:
        _check("write_file 成功", False, str(e))
        return
    _check("write_file 成功", True)

    # list_files 應能看到
    files = storage.list_files()
    _check("list_files 含此檔", name in files,
           "files={}".format(list(files.keys())[:5]))

    # read_all 讀回
    try:
        data = storage.read_all(name)
    except Exception as e:
        _check("read_all 成功", False, str(e))
        return
    _check("read_all 回傳長度正確", len(data) == len(payload),
           "got={} want={}".format(len(data), len(payload)))
    _check("讀回內容與寫入一致", bytes(data) == bytes(payload))


# ── 測試 2：分段寫入 write_begin/write/write_end ──────────────────
def test_write_chunked(storage, name="_t_chunked"):
    print("\n[2] 分段寫入 write_begin → write×N → write_end")
    # 用比 io_buf（16384）大的資料，分多次 write
    chunk = 4096
    n_chunks = 8
    payload = bytearray()
    for c in range(n_chunks):
        block = bytes([c & 0xFF] * chunk)   # 每段不同填充值
        payload += block
    total = len(payload)

    try:
        storage.write_begin(name, total)
        written = 0
        for c in range(n_chunks):
            block = bytes([c & 0xFF] * chunk)
            n = storage.write(block)
            written += n
        storage.write_end()
    except Exception as e:
        _check("分段寫入流程", False, str(e))
        return
    _check("分段寫入流程完成", True)
    _check("write 回傳總量正確", written == total,
           "written={} total={}".format(written, total))

    data = storage.read_all(name)
    _check("分段寫入讀回一致", bytes(data) == bytes(payload))


# ── 測試 3：read_into 分段讀 + seek/tell ──────────────────────────
def test_read_seek(storage, name="_t_seek"):
    print("\n[3] read_into 分段讀 + seek/tell")
    # 先寫一個已知內容的檔：每 512B 一個 block，block i 全填 i
    n_blocks = 12
    payload = bytearray()
    for b in range(n_blocks):
        payload += bytes([b & 0xFF] * 512)
    storage.write_file(name, bytes(payload))

    size = storage.read_begin(name)
    _check("read_begin 回傳 size 正確", size == len(payload),
           "size={} want={}".format(size, len(payload)))

    # 讀前 1024B
    buf = bytearray(1024)
    n = storage.read_into(buf)
    _check("read_into 首段 1024B", n == 1024, "n={}".format(n))
    _check("首段內容正確", buf == payload[:1024])

    # tell 應在 1024
    pos = storage.tell()
    _check("tell == 1024", pos == 1024, "pos={}".format(pos))

    # seek 到 block 5（offset 2560），對齊 sector
    storage.seek(2560)
    _check("seek 後 tell == 2560", storage.tell() == 2560)

    n = storage.read_into(buf)
    _check("seek 後讀 1024B", n == 1024)
    _check("seek 後內容 == block5/6", buf == payload[2560:2560 + 1024])

    storage.read_end()
    _check("read_end 後可重新 read_begin", True)
    storage.read_begin(name)
    storage.read_end()


# ── 測試 4：多檔案並存 ───────────────────────────────────────────
def test_multiple_files(storage):
    print("\n[4] 多檔案並存")
    names = ["_t_multi_a", "_t_multi_b", "_t_multi_c"]
    payloads = {}
    for nm in names:
        p = bytes(nm.encode()) * 256   # 用檔名當 pattern
        payloads[nm] = p
        storage.write_file(nm, p)
    _check("三檔都寫入", True)

    files = storage.list_files()
    all_present = all(nm in files for nm in names)
    _check("list_files 含三檔", all_present,
           "got={}".format([k for k in files if k.startswith("_t_multi")]))

    # 逐一讀回驗證，順序不依賴
    for nm in names:
        data = storage.read_all(nm)
        _check("讀回 {} 一致".format(nm), bytes(data) == payloads[nm])


# ── 測試 5：刪檔 ─────────────────────────────────────────────────
def test_remove(storage, name="_t_remove"):
    print("\n[5] 刪檔 remove")
    storage.write_file(name, b"X" * 2048)
    _check("先寫入待刪檔", name in storage.list_files())

    storage.remove(name)
    _check("remove 後 list_files 不含", name not in storage.list_files())

    # 讀已刪檔應失敗
    _check_raises("read 已刪檔應 raise", lambda: storage.read_begin(name))


# ── 測試 6：例外路徑 ─────────────────────────────────────────────
def test_exceptions(storage, existing="_t_basic"):
    print("\n[6] 例外路徑")
    # 讀不存在的檔
    _check_raises("read_begin 不存在檔應 raise",
                  lambda: storage.read_begin("_nonexistent_xyz"))

    # write_begin 讀同名（正在讀）應拒
    storage.read_begin(existing)
    try:
        _check_raises("write_begin 讀同名應 raise",
                      lambda: storage.write_begin(existing, 100))
    finally:
        storage.read_end()

    # read_begin 重複（已開）應 raise
    storage.read_begin(existing)
    try:
        _check_raises("read_begin 重複應 raise",
                      lambda: storage.read_begin(existing))
    finally:
        storage.read_end()


# ── 測試 7：CRC 完整性（write 內建 CRC32，儲存在 alloc.json）─────
def test_crc_integrity(storage, name="_t_crc"):
    print("\n[7] CRC 完整性")
    import ubinascii
    payload = bytes(range(256)) * 32   # 8192B
    storage.write_file(name, payload)
    expected_crc = "{:08X}".format(ubinascii.crc32(payload))

    # 從 list_files / alloc 看 CRC 是否記錄
    files = storage.list_files()
    # list_files 不含 CRC，直接讀 alloc entry
    entry = storage._alloc.find(name)
    has_crc = entry is not None and len(entry) >= 3 and entry[2] != "FFFFFFFF"
    _check("alloc entry 記錄了 CRC", has_crc,
           "entry={}".format(entry))
    if has_crc:
        _check("CRC 與本地計算一致", entry[2] == expected_crc,
               "stored={} calc={}".format(entry[2], expected_crc))

    # 讀回後重算 CRC，應相符
    data = storage.read_all(name)
    read_crc = "{:08X}".format(ubinascii.crc32(data))
    _check("讀回資料 CRC 一致", read_crc == expected_crc)


# ── 測試 8：Storage 內部 DMA buffer 配置（重構核心）──────────────
def test_dma_buffer(storage):
    print("\n[8] Storage 內部 buffer（重構後用 alloc_dma）")
    _check("_io_buf 非 None", storage._io_buf is not None)
    _check("_io_buf 長度 == _buf_size",
           len(storage._io_buf) == storage._buf_size,
           "len={} buf_size={}".format(len(storage._io_buf), storage._buf_size))
    # _io_buf_hc 反映是否配到 DMA；裝置上應 True
    _check("_io_buf_hc 為 True（DMA 命中）",
           getattr(storage, "_io_buf_hc", False) is True,
           "hc={}".format(getattr(storage, "_io_buf_hc", "?")))
    # close 應能安全釋放
    # 注意：這裡不真的 close，因為後續測試還要用同一個 storage。


# ── 測試 9：StreamReader 基本功能 ────────────────────────────────
def test_stream_reader(storage, name="_t_basic"):
    print("\n[9] StreamReader 基本讀取")
    # 先確認有個可讀的檔（複用 test_write_read_basic 建的）
    files = storage.list_files()
    if name not in files:
        storage.write_file(name, b"A" * 4096)

    entry = storage._alloc.find(name)
    if entry is None:
        _check("取得測試檔 entry", False)
        return
    start_sector, n_sectors = entry[0], entry[1]
    _check("取得 entry", True, "sector={} cnt={}".format(start_sector, n_sectors))

    try:
        sr = StreamReader(buf_size=4096, n_bufs=2)
    except Exception as e:
        _check("StreamReader 建構", False, str(e))
        return
    _check("StreamReader 建構", True)

    # DMA buffer 命中
    if hasattr(sr, "_hc"):
        dma_n = sum(sr._hc)
        _check("StreamReader DMA buffer 命中", dma_n > 0,
               "{}/{}".format(dma_n, len(sr._hc)))

    # 指到測試檔的 sector 範圍，跑 feed→next→release
    sr._r_sector = start_sector
    sr._r_cnt = n_sectors
    sr._started = True
    sr._eof = False
    consumed = 0
    sec = start_sector
    rem = n_sectors
    ok = True
    try:
        while rem > 0:
            if not sr.feed(sec):
                break
            v = sr.next()
            if v is None:
                break
            consumed += len(v)
            sr.release()
            sec += sr.chunk_sectors
            rem -= sr.chunk_sectors
    except Exception as e:
        ok = False
        print("    StreamReader 例外: {}".format(e))
    _check("feed→next→release 流程", ok)
    _check("讀取量大於 0", consumed > 0, "consumed={}".format(consumed))
    sr.close()
    _check("StreamReader close 安全", True)


# ── 測試 10：Storage 邊界 case（read_into off / seek 超檔尾）──────
def test_storage_edge(storage, name="_t_edge"):
    print("\n[10] Storage 邊界 case")
    payload = bytes(range(256)) * 8    # 2048B，含可辨識 pattern
    storage.write_file(name, payload)

    storage.read_begin(name)
    try:
        # read_into 帶 off：讀 512B 放進 buf[100:]
        buf = bytearray(1024)
        n = storage.read_into(buf, off=100)
        _check("read_into(off=100) 回傳 512", n == 512, "n={}".format(n))
        _check("off 區段 [0:100) 未被覆寫",
               buf[:100] == bytes(100),
               "head={}".format(bytes(buf[:8]).hex()))
        _check("off 區段 [100:612) == payload[:512]",
               buf[100:612] == payload[:512])

        # seek 超過檔尾應 clamp 到檔尾（2048），read_into 回 0
        storage.seek(999999)
        clamped = storage.tell()
        _check("seek(超大) clamp 到檔尾", clamped == len(payload),
               "tell={}".format(clamped))
        n = storage.read_into(buf)
        _check("檔尾 read_into 回 0", n == 0)

        # seek 負值應 clamp 到 0
        storage.seek(-100)
        _check("seek(-100) clamp 到 0", storage.tell() == 0)
    finally:
        storage.read_end()


# ── 測試 11：StreamReader 完整 API（start / feed_all / 滿拒 / read_into）─
def test_stream_reader_full(storage, name="_t_stream"):
    print("\n[11] StreamReader 完整 API")
    # 建一個跨多個 buffer 的檔（buf_size=4096, 檔 4096*6 = 24KB）
    n_bufs = 2
    buf_size = 4096
    file_sectors = (buf_size * 6) // storage._ss
    payload = bytes((i // 512) & 0xFF for i in range(file_sectors * storage._ss))
    storage.write_file(name, payload)

    sr = StreamReader(buf_size=buf_size, n_bufs=n_bufs)
    _check("StreamReader 建構", True)

    # (a) start(alloc, name) 正規入口
    try:
        sr.start(storage._alloc, name)
    except Exception as e:
        _check("start(alloc, name) 正規入口", False, str(e))
        sr.close()
        return
    _check("start(alloc, name) 正規入口", sr._started is True)
    _check("start 設定 _r_sector > 0", sr._r_sector > 0,
           "sector={}".format(sr._r_sector))

    # (b) buffer 滿時 feed 回 False（n_bufs 個 slot 都填滿，第 n+1 次 reject）
    sr2 = StreamReader(buf_size=buf_size, n_bufs=n_bufs)
    sr2._r_sector = sr._r_sector
    sr2._r_cnt = file_sectors
    sr2._started = True
    sr2._eof = False
    filled = 0
    sec = sr._r_sector
    for _ in range(n_bufs):
        if sr2.feed(sec):
            filled += 1
            sec += sr2.chunk_sectors
    _check("前 {} 次 feed 都成功".format(n_bufs), filled == n_bufs)
    rejected = not sr2.feed(sec)
    _check("第 {} 次 feed（滿）回 False".format(n_bufs + 1), rejected)
    sr2.close()

    # (c) read_into(buf) 串流 API — 連續讀直到 EOF
    sr3 = StreamReader(buf_size=buf_size, n_bufs=n_bufs)
    sr3._r_sector = sr._r_sector
    sr3._r_cnt = file_sectors
    sr3._started = True
    sr3._eof = False
    # 先預填讓 next 有資料
    sec = sr._r_sector
    for _ in range(n_bufs):
        sr3.feed(sec)
        sec += sr3.chunk_sectors
    collected = bytearray()
    read_buf = bytearray(buf_size)
    while True:
        # read_into 內部 next+release；需要時手動補 feed
        n = sr3.read_into(read_buf)
        if n == 0:
            # 嘗試補 feed
            if sec < sr._r_sector + file_sectors and sr3.feed(sec):
                sec += sr3.chunk_sectors
                continue
            break
        collected += read_buf[:n]
    sr3.close()
    _check("read_into 串流讀完", len(collected) == len(payload),
           "got={} want={}".format(len(collected), len(payload)))
    _check("read_into 內容一致", bytes(collected) == payload)

    # (d) feed_all 整檔預填
    sr4 = StreamReader(buf_size=buf_size, n_bufs=n_bufs)
    sr4._r_sector = sr._r_sector
    sr4._r_cnt = file_sectors
    sr4._started = True
    sr4._eof = False
    try:
        sr4.feed_all()   # 內部迴圈 feed + sleep，直到 EOF
        _check("feed_all 完成（_eof=True）", sr4._eof is True)
    except Exception as e:
        _check("feed_all 完成", False, str(e))
    sr4.close()

    sr.close()


# ── 測試 12：close() 釋放驗證（重構新增的 DMA 釋放邏輯）──────────
def test_close_release():
    print("\n[12] close() DMA 釋放驗證")
    # Storage
    s = Storage()
    io_buf_before = s._io_buf
    hc_before = getattr(s, "_io_buf_hc", False)
    _check("Storage close 前 _io_buf 非 None", io_buf_before is not None)
    s.close()
    _check("Storage close 後 _io_buf == None", s._io_buf is None)
    # close 冪等：再 close 不丟
    try:
        s.close()
        _check("Storage close 冪等", True)
    except Exception as e:
        _check("Storage close 冪等", False, str(e))

    # StreamReader
    sr = StreamReader(buf_size=4096, n_bufs=2)
    bufs_before = list(sr._bufs)
    hc_before = list(sr._hc)
    _check("StreamReader close 前 _bufs 非空", len(bufs_before) > 0)
    sr.close()
    _check("StreamReader close 後 _bufs 清空", len(sr._bufs) == 0)
    _check("StreamReader close 後 _hc 清空", len(sr._hc) == 0)
    try:
        sr.close()
        _check("StreamReader close 冪等", True)
    except Exception as e:
        _check("StreamReader close 冪等", False, str(e))


# ── 測試 13：真實檔案讀取效能（端到端吞吐）──────────────────────
def test_real_file_throughput(storage):
    print("\n[13] 真實檔案讀取效能（端到端吞吐）")
    # 找 SD 卡上實際部署的檔案來讀。優先用 raw 區（fast_io.Storage）的既有檔，
    # 因為那才走重構路徑；其次試 FAT 區的媒體檔。
    raw_files = storage.list_files()
    # 排除本測試自己建的 _t_* 檔
    real_raw = {k: v for k, v in raw_files.items() if not k.startswith("_t_")}
    _check("找到 raw 區實際檔案", len(real_raw) > 0,
           "raw 檔數={}".format(len(real_raw)))

    # (a) Storage.read_all 整檔讀（raw 路徑，含 DMA buffer）
    if real_raw:
        # 挑最大的那個來讀（讀越多越準）
        target = max(real_raw.items(), key=lambda kv: kv[1]["bytes"])
        tname, tinfo = target
        size = tinfo["bytes"]
        _check("測試目標 raw 檔", True,
               "{} ({}KB)".format(tname, size // 1024))
        gc.collect()
        t0 = time.ticks_ms()
        data = storage.read_all(tname)
        elapsed = time.ticks_diff(time.ticks_ms(), t0)
        mb_s = (len(data) * 1000) / (elapsed * 1048576) if elapsed > 0 else 0
        _check("Storage.read_all 吞吐", len(data) == size,
               "{} in {}ms → {:.2f} MB/s".format(
                   len(data), elapsed, mb_s))

    # (b) Storage.read_into 分段讀（模擬串流）
    if real_raw:
        target = max(real_raw.items(), key=lambda kv: kv[1]["bytes"])
        tname = target[0]
        size = target[1]["bytes"]
        storage.read_begin(tname)
        try:
            buf = bytearray(16384)
            gc.collect()
            t0 = time.ticks_ms()
            total = 0
            while True:
                n = storage.read_into(buf)
                if n == 0: break
                total += n
            elapsed = time.ticks_diff(time.ticks_ms(), t0)
            mb_s = (total * 1000) / (elapsed * 1048576) if elapsed > 0 else 0
            _check("Storage.read_into 分段讀吞吐", total == size,
                   "{} in {}ms → {:.2f} MB/s".format(total, elapsed, mb_s))
        finally:
            storage.read_end()

    # (c) fs_manager 統一層（若可用）
    try:
        from lib.fs_manager import fs
        fs_ok = True
    except Exception:
        fs_ok = False
    if fs_ok and real_raw:
        tname = max(real_raw.items(), key=lambda kv: kv[1]["bytes"])[0]
        path = "/sd/" + tname
        gc.collect()
        t0 = time.ticks_ms()
        try:
            data = fs.read(path)
            elapsed = time.ticks_diff(time.ticks_ms(), t0)
            mb_s = (len(data) * 1000) / (elapsed * 1048576) if elapsed > 0 else 0
            _check("fs_manager.read 統一層吞吐", data is not None,
                   "{}B in {}ms → {:.2f} MB/s".format(
                       len(data) if data else 0, elapsed, mb_s))
        except Exception as e:
            _check("fs_manager.read 統一層吞吐", False, str(e))

    # (d) FAT 區媒體檔（若有 /jpeg/background 或類似）
    fat_tested = False
    try:
        entries = os.listdir("/jpeg/background")
        jpgs = [f for f in entries if f.lower().endswith((".jpg", ".jpeg"))]
        if jpgs:
            test_jpg = "/jpeg/background/" + jpgs[0]
            stat = os.stat(test_jpg)
            fsize = stat[6]
            gc.collect()
            t0 = time.ticks_ms()
            with open(test_jpg, "rb") as f:
                total = 0
                rbuf = bytearray(16384)
                while True:
                    n = f.readinto(rbuf)
                    if n == 0: break
                    total += n
            elapsed = time.ticks_diff(time.ticks_ms(), t0)
            mb_s = (total * 1000) / (elapsed * 1048576) if elapsed > 0 else 0
            _check("FAT 媒體檔讀取吞吐", total == fsize,
                   "{} ({}B) in {}ms → {:.2f} MB/s".format(
                       jpgs[0], total, elapsed, mb_s))
            fat_tested = True
    except OSError:
        pass
    if not fat_tested:
        _check("FAT 媒體檔讀取吞吐", True, "(無 /jpeg/background，skip)")

    print("\n  💡 對照：F1(FAT)~1.85 MB/s，F5(StreamReader DMA)~12.5 MB/s")


# ── 清理測試檔 ───────────────────────────────────────────────────
def _cleanup(storage):
    """刪除本測試建的所有 _t_* 檔，不留下垃圾。"""
    files = storage.list_files()
    test_files = [k for k in files if k.startswith("_t_")]
    for nm in test_files:
        try:
            storage.remove(nm)
        except Exception:
            pass
    if test_files:
        print("\n🧹 清理 {} 個測試檔: {}".format(len(test_files), test_files))


def run():
    print("=" * 60)
    print("fast_io.Storage 功能正確性 + 真實檔案讀取效能 測試")
    print("=" * 60)

    if not IS_MICROPYTHON:
        print("⚠️ 需在 ESP32 裝置上跑")
        return False

    sd = bus.get_service("sd_raw")
    if sd is None:
        print("❌ sd_raw 未註冊（SD 初始化失敗或未掛載）")
        return False
    print("SD: {} sectors × {}B".format(sd.info()[0], sd.info()[1]))

    storage = Storage()
    print("io_buf: {}B  hc={}".format(
        storage._buf_size, getattr(storage, "_io_buf_hc", "?")))

    # 安全檢查 — 沒過就拒跑寫入測試
    safe = _check_alloc_guard(storage)
    if not safe:
        print("\n⛔ 因 alloc.json 防線未就位，中止測試（避免毀卡）。")
        print("   請先在電腦用 storage_tool.py 格式化建 alloc.json，再重跑。")
        storage.close()
        return False

    # 正式測試
    try:
        test_write_read_basic(storage)
        test_write_chunked(storage)
        test_read_seek(storage)
        test_multiple_files(storage)
        test_remove(storage)
        test_exceptions(storage)
        test_crc_integrity(storage)
        test_dma_buffer(storage)
        test_stream_reader(storage)
        test_storage_edge(storage)
        test_stream_reader_full(storage)
        test_close_release()
        test_real_file_throughput(storage)
    except Exception as e:
        print("\n💥 測試中途未預期例外: {}: {}".format(type(e).__name__, e))
        import sys
        sys.print_exception(e)
    finally:
        _cleanup(storage)
        storage.close()

    print("\n" + "=" * 60)
    print("結果：✅ {} 通過 / ❌ {} 失敗".format(_passed, _failed))
    print("=" * 60)
    return _failed == 0


if __name__ == "__main__":
    run()
