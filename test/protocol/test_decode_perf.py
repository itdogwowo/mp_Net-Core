# -*- coding: utf-8 -*-
"""解碼路徑 (StreamParser → ADDR 過濾 → SchemaCodec/viper decode) 性能 + 正確性驗證

驗證:
  1. ADDR 過濾: 廣播(0xFFFF)/本機 cID 放行, 定址他人丟棄。
  2. SchemaCodec.decode (viper) 往返正確性。
  3. 性能: 核心解碼迴圈 (parser + 過濾 + viper) 每幀耗時/吞吐;
     重量變體 (FILE_CHUNK + 2KB bytes_rest); 選用區段走 native
     app.handle_stream 全路徑 (僅 ESP32, 含 dispatch)。

用法:
  PC   (CPython, 驗正確性 + 粗基線; viper 退化為純 Py):
       python test/protocol/test_decode_perf.py
  ESP32 (MicroPython, viper 真實):
       exec(open("test/protocol/test_decode_perf.py").read())   # 或直接執行

注意: Proto.pack 回共享 buffer 的 memoryview, 下次 pack 會覆蓋, 故用 bytes()
      立即具體化每幀 (與專案「立即消費」契約一致)。
"""
import sys, os, time

IS_MP = (sys.implementation.name == 'micropython')


# ★ 2026-10：proto / schema_loader / schema_codec / sys_bus 都已搬到
#   `lib/sys/` 底下，import 路徑要跟著走（見 changelog §32.7）。
def _bootstrap():
    """找出 slave/ (含 lib/ schema/) 加進 sys.path, 回傳 schema 目錄。"""
    cands = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        cands.append(os.path.normpath(os.path.join(here, "..", "..", "slave")))
    except NameError:
        pass
    cands.append(os.path.join(os.getcwd(), "slave"))
    cands.append(os.getcwd())
    cands.append("/")
    for s in cands:
        try:
            if s and os.path.isdir(os.path.join(s, "lib")):
                if s not in sys.path:
                    sys.path.insert(0, s)
                sc = os.path.join(s, "schema")
                if os.path.isdir(sc):
                    return sc
        except OSError:
            continue
    for sc in ("/schema", "schema"):
        try:
            if any(n.endswith(".json") for n in os.listdir(sc)):
                return sc
        except OSError:
            continue
    raise RuntimeError("找不到 slave/ (lib) 或 schema 目錄")


SCHEMA_DIR = "/schema" if IS_MP else _bootstrap()  # 裝置: os.path 不存在, 直接用 /schema

# CPython 相容: 讓 @viper 的 _viper_decode 作為純 Python 跑 (bytearray 可寫)
if not IS_MP:
    import lib.sys.schema_codec as _sc
    _sc.ptr8 = lambda x: x
    _sc.ptr16 = lambda x: x

from lib.sys.sys_bus import bus
from lib.sys.proto import Proto, StreamParser, MAX_PAYLOAD, ADDR_BROADCAST
from lib.sys.schema_loader import SchemaStore
from lib.sys.schema_codec import SchemaCodec


def now_us():
    return time.ticks_us() if IS_MP else int(time.perf_counter() * 1e6)


def us_since(t0):
    t1 = now_us()
    return time.ticks_diff(t1, t0) if IS_MP else (t1 - t0)


# ── 模擬 ConfigManager T0 推動 cID (單一真相: bus.cid) ──
MY_CID_STR = "ABCD"
MY_CID = int(MY_CID_STR, 16) & 0xFFFF
if "System" not in bus.shared:
    bus.shared["System"] = {}
bus.shared["System"]["cID"] = MY_CID_STR
bus.cid = MY_CID

store = SchemaStore(SCHEMA_DIR)
store.finalize()

# 輕量: SLAVE_ANNOUNCE (0x1002) — slave_id(str) + pixel_count(u16) + hw_version(str)
CMD_LIGHT = 0x1002
cmd_light = store.get(CMD_LIGHT)
EXPECTED = {"slave_id": "ESP32_AB", "pixel_count": 336, "hw_version": "v1.0"}
payload_light = SchemaCodec.encode(cmd_light, EXPECTED)

# 重量: FILE_CHUNK (0x2002) — file_id(u16) + offset(u32) + data(bytes_rest, 2KB)
CMD_HEAVY = 0x2002
cmd_heavy = store.get(CMD_HEAVY)
HEAVY_DATA = bytes([i & 0xFF for i in range(2048)])
EXPECTED_HEAVY = {"file_id": 7, "offset": 123, "data": HEAVY_DATA}
payload_heavy = SchemaCodec.encode(cmd_heavy, EXPECTED_HEAVY) if cmd_heavy else b""

# 三種定址幀 (bytes() 立即具體化, 避免共享 buffer 被下次 pack 覆蓋)
frame_me    = bytes(Proto.pack(CMD_LIGHT, payload_light, addr=MY_CID))
frame_bcast = bytes(Proto.pack(CMD_LIGHT, payload_light, addr=ADDR_BROADCAST))
frame_other = bytes(Proto.pack(CMD_LIGHT, payload_light, addr=0x1234))
BATCH = frame_me + frame_bcast + frame_other
FRAMES_IN = 3
FRAMES_DECODED = 2  # me + bcast


# 核心解碼迴圈 (與 app.handle_stream 等價; 直呼 SchemaCodec.decode, 跳過 dispatch 開銷)
def decode_batch(parser, data, cmd_def):
    parser.feed(data)
    my_cid = bus.cid
    n = 0
    while True:
        r = parser.pop_frame()
        if r is None:
            break
        _ver, addr, cmd, pl = r
        if addr != ADDR_BROADCAST and addr != my_cid:
            continue
        SchemaCodec.decode(cmd_def, pl, store)
        n += 1
    return n


# ── Section 1: 正確性 ──
def test_decode_roundtrip():
    args = SchemaCodec.decode(cmd_light, payload_light, store)
    for k, v in EXPECTED.items():
        assert args.get(k) == v, "decode 失敗 %s: %r != %r" % (k, args.get(k), v)


def test_addr_filter():
    p = StreamParser(max_len=MAX_PAYLOAD)
    p.feed(BATCH)
    accepted = []
    while True:
        r = p.pop_frame()
        if r is None:
            break
        _ver, addr, _cmd, _pl = r
        if addr != ADDR_BROADCAST and addr != MY_CID:
            continue
        accepted.append(addr)
    assert len(accepted) == FRAMES_DECODED, "過濾數量錯: %d" % len(accepted)
    assert MY_CID in accepted and ADDR_BROADCAST in accepted
    assert 0x1234 not in accepted, "不該收的收到了"


def test_batch_count():
    p = StreamParser(max_len=MAX_PAYLOAD)
    assert decode_batch(p, BATCH, cmd_light) == FRAMES_DECODED


def test_heavy_roundtrip():
    if not cmd_heavy:
        raise RuntimeError("SKIP: schema 無 FILE_CHUNK 0x2002")
    args = SchemaCodec.decode(cmd_heavy, payload_heavy, store)
    assert args.get("file_id") == 7, "file_id: %r" % args.get("file_id")
    assert args.get("offset") == 123, "offset: %r" % args.get("offset")
    got = args.get("data")
    assert bytes(got) == HEAVY_DATA, "data 不符 (len %d)" % len(got)


# ── Section 2: 性能 ──
def _measure(fn, n):
    for _ in range(50):  # warmup
        fn()
    t0 = now_us()
    for _ in range(n):
        fn()
    dt = us_since(t0)
    return dt


def perf_core_light():
    p = StreamParser(max_len=MAX_PAYLOAD)
    N = 2000
    dt = _measure(lambda: decode_batch(p, BATCH, cmd_light), N)
    total_in = N * FRAMES_IN
    total_dec = N * FRAMES_DECODED
    total_bytes = N * len(BATCH)
    print("  [輕量] 核心迴圈 (parser + ADDR 過濾 + viper), SLAVE_ANNOUNCE %dB payload:" % len(payload_light))
    print("    env       : %s" % ("MicroPython (viper 真實)" if IS_MP else "CPython (viper 退化純 Py, 僅參考)"))
    print("    iters=%d batch=%d幀/%dB  總耗時=%dus (%.2fms)" % (N, FRAMES_IN, len(BATCH), dt, dt / 1000.0))
    print("    每幀(輸入)=%.2fus  每幀(解碼)=%.2fus" % (dt / total_in, dt / total_dec))
    print("    吞吐=%.0f解碼幀/s | %.2fMB/s" % (
        total_dec / (dt / 1e6) if dt else 0, (total_bytes / (dt / 1e6)) / 1e6 if dt else 0))


def perf_core_heavy():
    if not cmd_heavy:
        print("  [重量] [SKIP] schema 無 FILE_CHUNK")
        return
    p = StreamParser(max_len=MAX_PAYLOAD)
    heavy_frame = bytes(Proto.pack(CMD_HEAVY, payload_heavy, addr=MY_CID))
    N = 500
    dt = _measure(lambda: decode_batch(p, heavy_frame, cmd_heavy), N)
    total_bytes = N * len(heavy_frame)
    print("  [重量] 核心迴圈, FILE_CHUNK %dB payload (2KB bytes_rest):" % len(payload_heavy))
    print("    iters=%d 幀=%dB  總耗時=%dus (%.2fms)" % (N, len(heavy_frame), dt, dt / 1000.0))
    print("    每幀=%.2fus  吞吐=%.2fMB/s" % (dt / N, (total_bytes / (dt / 1e6)) / 1e6 if dt else 0))


def perf_native_handle_stream():
    print("  [native] app.handle_stream 全路徑 (含 dispatch, no-op handler):")
    try:
        from app import App
    except Exception as e:
        print("    [SKIP] 需 ESP32 + 完整 action 環境 (PC: %s)" % e)
        return
    app = App()
    app.disp.on(CMD_LIGHT, lambda ctx, args: None)
    app.disp.debug_level = 0
    p2 = app.create_parser()
    N = 2000
    for _ in range(50):
        app.handle_stream(p2, BATCH, "PerfTest", None, None)
    t0 = now_us()
    for _ in range(N):
        app.handle_stream(p2, BATCH, "PerfTest", None, None)
    dt = us_since(t0)
    total_in = N * FRAMES_IN
    print("    iters=%d  總耗時=%dus (%.2fms)  每幀(輸入)=%.2fus  吞吐=%.0f幀/s" % (
        N, dt, dt / 1000.0, dt / total_in, total_in / (dt / 1e6) if dt else 0))
    print("    (與核心迴圈差 = dispatch + native loop 開銷)")


_TESTS = [
    ("decode 往返正確", test_decode_roundtrip),
    ("ADDR 過濾", test_addr_filter),
    ("batch 解碼數量", test_batch_count),
    ("重量 decode 往返", test_heavy_roundtrip),
]


def run_all():
    print("\n" + "=" * 60)
    print("解碼路徑 (ADDR 過濾 + viper decode) 驗證")
    print("=" * 60)
    passed = failed = 0
    for name, fn in _TESTS:
        try:
            fn()
            print("  [PASS] %s" % name)
            passed += 1
        except Exception as e:
            msg = str(e)
            if msg.startswith("SKIP:"):
                print("  [SKIP] %s  (%s)" % (name, msg[6:]))
            else:
                print("  [FAIL] %s  → %s" % (name, e))
                failed += 1
    print("-" * 60)
    print("正確性: %d 通過, %d 失敗" % (passed, failed))
    print("-" * 60)
    perf_core_light()
    perf_core_heavy()
    perf_native_handle_stream()
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    run_all()
