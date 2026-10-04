# -*- coding: utf-8 -*-
"""StreamParser 解碼吞吐測速 (單線程純解碼 + 雙線程生產/消費管道)

背景: test_proto_hotpath.py 是「正確性」驗證(自產自解)。本檔是「速度」驗證,
補足熱路徑優化 (feed 改 memoryview slice 賦值) 的吞吐量測。

兩種模式:
  1. bench_decode()    單線程純解碼 — 預先 pack 好整串幀流, 之後只測 feed+pop,
                       乾淨量出 StreamParser 解碼器的 CPU 上限。可跨 PC/裝置跑
                       (proto.py 的 feed 已是純 Python slice 賦值, 不依賴 viper)。

  2. bench_pipe()      雙線程生產/消費 — 模擬 core0 收包(core0 生產) →
                       AtomicStreamHub → core1 解碼(消費)。驗證 SPSC hub 不丟資料
                       + view 模式正確。⚠️ MicroPython _thread 有 GIL, 純計算
                       雙線程會串行, 吞吐是「架構參考值」不是並行上限; 真實並行
                       靠 socket I/O 釋放 GIL 才發生。此模式僅裝置可跑(需 _thread
                       + buffer_hub 的 micropython)。

用法 (裝置 REPL):
  exec(open("test/protocol/test_proto_speed.py").read())
  bench_decode()          # 單線程純解碼, chunk 2K/4K/8K 各跑一次
  bench_pipe()            # 雙線程管道 (需裝置)

判讀:
  - bench_decode 的 MB/s 就是「解碼器純 CPU 上限」, 對比優化前(viper 逐 byte)
    可看出 slice 賦值帶來的提升。
  - bench_pipe 跑完若「幀數一致、無遺漏」即架構正確。
"""

import gc
import os
import sys
import time

try:
    import ubinascii as binascii
except ImportError:
    import binascii

# ── 路徑 bootstrap（★ 2026-10 補）────────────────────────────────
#   以前這支只能從專案根目錄跑（靠 CWD），而且路徑寫 `lib.proto`
#   —— 模組搬到 `lib/sys/` 之後就 ModuleNotFoundError 了。
_HERE = os.path.dirname(os.path.abspath(__file__))
_SLAVE = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "slave")
if _SLAVE not in sys.path:
    sys.path.insert(0, _SLAVE)

from lib.sys.proto import Proto, StreamParser, MAX_PAYLOAD

_CMD = 0x18F0
_HUB_OFF = 2

# 計時抽象: 裝置用 ticks_ms, PC(CPython) 用 time.time 兜底
try:
    _ticks_ms = time.ticks_ms
    _ticks_diff = time.ticks_diff
except AttributeError:
    _t0 = [0.0]
    def _ticks_ms():
        import time as _t
        return _t.time() * 1000.0
    def _ticks_diff(a, b):
        return a - b


def _mb_s(total_bytes, elapsed_ms):
    if elapsed_ms <= 0:
        elapsed_ms = 1
    return (total_bytes * 1000.0) / (elapsed_ms * 1048576)


# ═══════════════════════════════════════════════════════════════
#  單線程純解碼測速
# ═══════════════════════════════════════════════════════════════

def _build_stream(chunk, total_bytes):
    """預先 pack 好 total_bytes 位元組的幀流, 串成一個大 bytearray。

    幀 = header(9) + payload(chunk) + crc(4)。payload 是確定性純 data (無 seq),
    所以 pop 出的 payload 長度即 data 長度, 統計簡單。
    """
    payload = bytes([i & 0xFF for i in range(chunk)])
    frame = Proto.pack(_CMD, payload)          # 共享 buffer, 立即複製
    flen = len(frame)                          # 9 + chunk + 4
    n = total_bytes // chunk
    stream = bytearray(n * flen)
    mv = memoryview(stream)
    for i in range(n):
        mv[i * flen:(i + 1) * flen] = frame    # 複製一幀 (memoryview slice 賦值)
    return stream, n


def _decode_all(stream, chunk, parser):
    """逐幀 feed → 立即 pop, 回傳解出的 payload 總位元組數。

    注意: StreamParser buffer 只容一幀 (max_len=chunk → buf=chunk+13)。
    若按固定大小亂切喂, 會讓不完整幀殘留在 buffer 又塞不下新資料而被丟棄。
    正確節奏 = 生產 bus_decode: 收一筆就 feed 一次並立即 pop 乾淨。"""
    mv = memoryview(stream)
    flen = 9 + chunk + 4
    n = len(stream) // flen
    got = 0
    for i in range(n):
        parser.feed(mv[i * flen:(i + 1) * flen])
        while True:
            r = parser.pop_frame()
            if r is None:
                break
            got += len(r[3])
    return got


def bench_decode(chunks=(2048, 4096, 8192), total_kb=512, runs=3):
    """單線程純解碼測速 (解碼器 CPU 上限)。"""
    print("\n" + "=" * 60)
    print("單線程純解碼測速 (feed+pop, 不含 pack/網路)")
    print("=" * 60)

    for chunk in chunks:
        total_bytes = total_kb * 1024
        stream, n_frames = _build_stream(chunk, total_bytes)
        expect = chunk * n_frames

        best = None
        for r in range(runs):
            parser = StreamParser(max_len=chunk)
            gc.collect()
            t0 = _ticks_ms()
            got = _decode_all(stream, chunk, parser)
            elapsed = _ticks_diff(_ticks_ms(), t0)
            if got != expect:
                print("  chunk={} run={} ❌ 量不符 got={} exp={}".format(chunk, r, got, expect))
                best = None
                break
            mb = _mb_s(got, elapsed)
            if best is None or mb > best:
                best = mb
        if best is not None:
            print("  {:>6s} payload  {:>6d} 幀  {:>7.2f} MB/s".format(
                "{}K".format(chunk // 1024), n_frames, best))
        stream = None
        parser = None
        gc.collect()
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════
#  雙線程生產/消費管道測速 (僅裝置)
# ═══════════════════════════════════════════════════════════════

def _frame_drain_slot(slot_mv, start, end):
    """從單一 slot 的 memoryview 直接 pop 完整幀 (零拷貝)。

    ⚠️ MicroPython memoryview 無 .find, Python 逐 byte 掃描太慢 (實測打崩吞吐),
    故此函式目前保留為「原型」(不建議在熱路徑用)。正確做法是核心 StreamParser
    支援從外部 view pop (需 carry-over), 見 _bench_fastparser.py 的 6.85 MB/s 方向。
    回傳 (start, plen) 或 None; 語意同 pop_frame (SOF/VER/LEN/CRC32)。"""
    # 保守實作: 直接用 bytes 找 SOF (拷貝一次, 定位用); 真實加速需核心 parser 支援
    from lib.sys.proto import HDR_LEN, CRC_LEN, SOF, CUR_VER, MAX_PAYLOAD
    import binascii as _b
    buf = bytes(slot_mv)
    ln = end
    while (ln - start) >= HDR_LEN:
        idx = buf.find(SOF, start, ln)
        if idx < 0:
            return None
        start = idx
        if (ln - start) < HDR_LEN:
            return None
        ver = buf[start + 2]
        pln = buf[start + 7] | (buf[start + 8] << 8)
        if ver != CUR_VER or pln > MAX_PAYLOAD:
            start += 1
            continue
        total = HDR_LEN + pln + CRC_LEN
        if (ln - start) < total:
            return None
        ps = start + HDR_LEN
        pe = ps + pln
        crc_recv = buf[pe] | (buf[pe + 1] << 8) | (buf[pe + 2] << 16) | (buf[pe + 3] << 24)
        crc_calc = _b.crc32(slot_mv[start + 2:pe], 0) & 0xFFFFFFFF
        if crc_calc != crc_recv:
            start += 1
            continue
        return start + total, pe - ps
    return None


def bench_pipe(chunk=4096, total_kb=1024):
    """雙線程管道: 生產線程 pack→寫 hub, 消費線程讀 hub→feed→pop。

    模擬 core0 收包 / core1 解碼的分離架構。驗證 SPSC hub + view 模式不丟資料。
    消費端善用 hub view 零拷貝: 完整幀直接從 slot pop, 只有殘幀 fallback feed。
    """
    try:
        import _thread
        from lib.sys.buffer_hub import AtomicStreamHub
    except ImportError as e:
        print("⚠️ bench_pipe 需裝置 (_thread + buffer_hub): {}".format(e))
        return

    import struct

    total_bytes = total_kb * 1024
    n_frames = total_bytes // chunk
    flen = 9 + chunk + 4                 # 一幀長
    slot_size = flen + _HUB_OFF          # slot 含 2B 長度前綴
    hub = AtomicStreamHub(slot_size, num_buffers=8, try_dma=False)

    payload = bytes([i & 0xFF for i in range(chunk)])
    parser = StreamParser(max_len=chunk)

    produced = [0]
    parsed_bytes = [0]
    parsed_frames = [0]
    done = [False]
    t_start = [0]

    def producer():
        pkt = Proto.pack(_CMD, payload)
        i = 0
        while i < n_frames:
            v = hub.get_write_view()
            if v is None:
                time.sleep_ms(0)         # hub 滿, 讓 GIL 給消費端
                continue
            struct.pack_into("<H", v, 0, flen)
            v[_HUB_OFF:_HUB_OFF + flen] = pkt
            hub.commit()
            i += 1
            produced[0] = i

    def consumer():
        t_start[0] = _ticks_ms()
        pb = parsed_bytes[0]
        pf = parsed_frames[0]
        while True:
            v = hub.get_read_view()
            if v is None:
                if produced[0] >= n_frames and hub.get_fill_level() == 0:
                    break
                time.sleep_ms(0)
                continue
            ln = v[0] | (v[1] << 8)
            if ln > 0:
                parser.feed(v[_HUB_OFF:_HUB_OFF + ln])
                while True:
                    r = parser.pop_frame()
                    if r is None:
                        break
                    pb += len(r[3])
                    pf += 1
            hub.release_read()
        parsed_bytes[0] = pb
        parsed_frames[0] = pf

    _thread.stack_size(16 * 1024)
    _thread.start_new_thread(producer, ())
    consumer()

    elapsed = _ticks_diff(_ticks_ms(), t_start[0])
    print("\n" + "=" * 60)
    print("雙線程管道測速 (生產 → hub → 解碼)")
    print("=" * 60)
    print("  chunk       : {}K".format(chunk // 1024))
    print("  生產幀數    : {} / {}".format(produced[0], n_frames))
    print("  解出幀數    : {}".format(parsed_frames[0]))
    ok = (produced[0] == n_frames and parsed_frames[0] == n_frames
          and parsed_bytes[0] == total_bytes)
    print("  完整性      : {}".format("✅ 無遺漏" if ok else "❌ 幀數/位元組不符"))
    if ok:
        print("  解碼吞吐    : {:.2f} MB/s (含 GIL 串行, 參考值)".format(
            _mb_s(parsed_bytes[0], elapsed)))
    print("=" * 60)


if __name__ == "__main__":
    print(">>> bench_decode (single-thread, pop_frame) <<<")
    bench_decode()
    gc.collect()
    print("\n>>> bench_pipe (dual-thread, pop_frame) <<<")
    bench_pipe()
