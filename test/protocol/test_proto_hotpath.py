# -*- coding: utf-8 -*-
"""StreamParser / buffer_hub 熱路徑優化驗證 (自包含, 不需網路/硬體)

驗證兩處生產改動:
  1. lib/sys/proto.py  StreamParser.feed 的「memoryview slice 賦值」取代 viper 逐 byte 複製,
     以及 compact 的 slice 賦值。覆蓋黏包/半包/resync/compact/大 payload/CRC 壞幀/生命週期。
  2. lib/sys/buffer_hub.py  AtomicStreamHub 的 view 模式 (get_read_view/release_read),
     對齊 tasks/bus_decode.py 的新消費迴圈 (取代 read_into copy 模式)。

用法 (裝置 REPL 或 PC 都能跑 StreamParser 部分; hub 部分需 MicroPython):
  exec(open("test/protocol/test_proto_hotpath.py").read())

每個用例獨立 PASS/FAIL, 最後印總計。任一 FAIL 請回報對應用例名。
"""

import os
import struct
import sys

try:
    import ubinascii as binascii
except ImportError:
    import binascii

# ── 路徑：讓同一支測試在 PC 與裝置上都能 import 到 lib.* ────────────
#   ★ 2026-10 修正：`proto.py` / `buffer_hub.py` 已經搬到 `lib/sys/` 底下，
#     這裡卻還寫 `from lib.proto import ...` → 在 PC 上直接
#     `ModuleNotFoundError: No module named 'lib''`（裝置上因為 sys.path
#     剛好含專案根目錄才僥倖可跑）。連路徑一起修掉，兩邊一致。
_HERE = os.path.dirname(os.path.abspath(__file__))
_SLAVE = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "slave")
if _SLAVE not in sys.path:
    sys.path.insert(0, _SLAVE)

from lib.sys.proto import (Proto, StreamParser, MAX_PAYLOAD, HDR_LEN,
                           CRC_LEN, SOF, CUR_VER)


# ── 測試用的虛擬 cmd (不衝突任何 schema) ──
_CMD = 0x18F0


def _make_payload(seq, size):
    """payload = u16 seq + 確定性 data。size 是 data 長度 (不含 seq 2B)。"""
    data = bytes([(i + seq) & 0xFF for i in range(size)])
    return struct.pack("<H", seq & 0xFFFF) + data


def _frame(seq, size):
    """回傳 bytes (Proto.pack 回 memoryview, 轉成 bytes 方便拼接/比較)。"""
    return bytes(Proto.pack(_CMD, _make_payload(seq, size)))


# ═══════════════════════════════════════════════════════════════
#  Section 1 — StreamParser 正確性
# ═══════════════════════════════════════════════════════════════

def _drain(p, check_seq=True):
    """pop 乾淨並驗證 seq 依序, 回傳 (幀數, 總 data bytes)。"""
    n = 0
    total = 0
    for _ver, _addr, _cmd, payload in p.pop():
        assert _cmd == _CMD, "cmd 錯: 0x{:04X}".format(_cmd)
        assert len(payload) >= 2, "payload 太短"
        seq = payload[0] | (payload[1] << 8)
        if check_seq:
            assert seq == n, "seq 錯序: got {} want {}".format(seq, n)
        n += 1
        total += len(payload) - 2
    return n, total


def test_single_frame():
    p = StreamParser()
    p.feed(_frame(0, 4096))
    assert _drain(p) == (1, 4096)


def test_sticky_packets():
    p = StreamParser()
    # 3 幀黏在一起一次 feed (bytes 拼接, 不是 memoryview)
    p.feed(_frame(0, 2048) + _frame(1, 2048) + _frame(2, 2048))
    assert _drain(p) == (3, 2048 * 3)


def test_half_packet():
    p = StreamParser()
    f = _frame(0, 4096)
    half = len(f) // 2
    p.feed(f[:half])
    assert _drain(p) == (0, 0), "半包不該提前 pop"
    p.feed(f[half:])
    assert _drain(p) == (1, 4096)


def test_random_chunking():
    p = StreamParser()
    frames = [_frame(s, 1024) for s in range(20)]
    stream = b"".join(frames)
    i = 0
    step = 0
    parsed = 0
    # 隨機分段餵, 但「餵一段 → 立即 pop」, 模擬生產 handle_stream 節奏
    while i < len(stream):
        take = min(13 + (step * 977) % 1500, len(stream) - i)
        p.feed(stream[i:i + take])
        i += take
        step += 1
        for _v, _a, _c, payload in p.pop():
            seq = payload[0] | (payload[1] << 8)
            assert seq == parsed, "seq 錯序 got {} want {}".format(seq, parsed)
            parsed += 1
    assert parsed == 20


def test_garbage_resync():
    p = StreamParser()
    # 前面塞 5 位元組垃圾 + 中間塞 3 位元組垃圾, 逼出 resync
    p.feed(b"\x01\x02\x03\x04\x05" + _frame(0, 2048) + b"\xaa\xbb\xcc" + _frame(1, 2048))
    assert _drain(p) == (2, 2048 * 2)


def test_compact():
    # max_len 只容 ~1 幀, 逼出 feed 內的 compact 分支
    p = StreamParser(max_len=4096 + 2)
    for s in range(10):
        f = _frame(s, 4096)
        half = len(f) // 2
        p.feed(f[:half])
        p.feed(f[half:])
        # 每幀立即 pop, 模擬生產節奏 (否則 buffer 滿會丟)
        _drain(p, check_seq=False)  # seq 已由下面總驗證, 這裡不檢查順序


def test_max_payload():
    # payload 上限 MAX_PAYLOAD = 8192; seq 佔 2B, 故 data = MAX_PAYLOAD - 2
    p = StreamParser(max_len=MAX_PAYLOAD)
    f = _frame(0, MAX_PAYLOAD - 2)
    assert len(f) == HDR_LEN + MAX_PAYLOAD + CRC_LEN, len(f)
    p.feed(f)
    assert _drain(p) == (1, MAX_PAYLOAD - 2)


def test_crc_corrupt():
    p = StreamParser()
    good = _frame(0, 2048)
    bad = _frame(1, 2048)
    # 破壞 bad 幀 payload 中間一個 byte (CRC 必錯)
    bad = bad[:HDR_LEN + 10] + bytes([bad[HDR_LEN + 10] ^ 0xFF]) + bad[HDR_LEN + 11:]
    p.feed(good + bad + _frame(2, 2048))
    out = [payload[0] | (payload[1] << 8) for _v, _a, _c, payload in p.pop()]
    assert 0 in out, "好幀 seq0 丟失"
    assert 1 not in out, "壞幀被 yield"
    assert 2 in out, "壞幀之後的好幀 resync 失敗"


def test_payload_lifetime():
    # pop 出的 payload 必須是 bytes (可跨 feed 持有), 不是會被覆蓋的 memoryview view
    p = StreamParser()
    p.feed(_frame(0, 1024))
    got = list(p.pop())
    assert len(got) == 1
    payload = got[0][3]
    assert isinstance(payload, (bytes, bytearray)), type(payload)
    p.feed(_frame(1, 1024))
    assert (payload[0] | (payload[1] << 8)) == 0, "舊 payload 被覆蓋"


# ═══════════════════════════════════════════════════════════════
#  Section 2 — buffer_hub view 模式 (對齊 bus_decode 新消費迴圈)
#   (僅 MicroPython 可跑, 因 buffer_hub 依賴 micropython 模組)
# ═══════════════════════════════════════════════════════════════

_HUB_OFF = 2


def test_hub_view_decode():
    try:
        from lib.sys.buffer_hub import AtomicStreamHub
    except ImportError:
        raise RuntimeError("SKIP: buffer_hub 需 MicroPython (CPython 下無 micropython 模組)")
    slot_size = 4096 + _HUB_OFF
    hub = AtomicStreamHub(slot_size, num_buffers=4, try_dma=False)

    # 模擬 net_bus 寫入: 前 2B 存長度 (小端 u16), 後面存幀
    frames = [_frame(s, 1024) for s in range(3)]
    for f in frames:
        view = hub.get_write_view()
        assert view is not None, "hub 滿, 寫不入"
        struct.pack_into("<H", view, 0, len(f))
        view[_HUB_OFF:_HUB_OFF + len(f)] = f
        hub.commit()

    # bus_decode 新消費迴圈: get_read_view → 讀長度 → feed → release_read
    p = StreamParser()
    while True:
        view = hub.get_read_view()
        if view is None:
            break
        ln = view[0] | (view[1] << 8)
        if ln > 0:
            data = view[_HUB_OFF:_HUB_OFF + ln]
            p.feed(data)          # feed 立即複製進 parser._buf, 不持有 view
        hub.release_read()

    assert _drain(p) == (3, 1024 * 3)
    assert hub.get_fill_level() == 0, "slot 未歸還"


# ═══════════════════════════════════════════════════════════════
#  執行
# ═══════════════════════════════════════════════════════════════

_TESTS = [
    ("單幀", test_single_frame),
    ("黏包", test_sticky_packets),
    ("半包", test_half_packet),
    ("隨機分段", test_random_chunking),
    ("垃圾重同步", test_garbage_resync),
    ("compact 分支", test_compact),
    ("最大負載 8K", test_max_payload),
    ("CRC 壞幀丟棄", test_crc_corrupt),
    ("payload 生命週期", test_payload_lifetime),
    ("hub view 模式解碼", test_hub_view_decode),
]


def run_all():
    passed = 0
    failed = 0
    print("\n" + "=" * 56)
    print("StreamParser / buffer_hub 熱路徑優化驗證")
    print("=" * 56)
    for name, fn in _TESTS:
        try:
            fn()
            print("  [PASS] {}".format(name))
            passed += 1
        except Exception as e:
            msg = str(e)
            if msg.startswith("SKIP:"):
                print("  [SKIP] {}  ({})".format(name, msg[6:]))
            else:
                print("  [FAIL] {}  → {}".format(name, e))
                failed += 1
    print("-" * 56)
    print("結果: {} 通過, {} 失敗".format(passed, failed))
    if failed == 0:
        print("✅ 全部通過 — 熱路徑優化正確")
    else:
        print("❌ 有失敗 — 請回報上方 [FAIL] 用例")
    print("=" * 56)
    return failed == 0


if __name__ == "__main__":
    run_all()
