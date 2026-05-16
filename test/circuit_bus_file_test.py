import struct
import sys
import time
import gc


IS_MICROPYTHON = (getattr(sys, "implementation", None) and sys.implementation.name == "micropython")

if IS_MICROPYTHON:
    import ubinascii as binascii
else:
    import binascii

try:
    import hashlib
except Exception:
    try:
        import uhashlib as hashlib
    except Exception:
        hashlib = None

from lib.proto import Proto, StreamParser
from lib.schema_loader import SchemaStore
from lib.schema_codec import SchemaCodec

_CMD_NAMES = {
    0x2001: "FILE_BEGIN",
    0x2002: "FILE_CHUNK",
    0x2003: "FILE_END",
    0x2004: "FILE_ACK",
    0x2005: "FILE_QUERY",
    0x2006: "FILE_QUERY_RSP",
    0x2007: "FILE_READ",
    0x2009: "FILE_DELETE",
    0x200B: "FILE_SCAN",
    0x3201: "MP4_PLAYER_CTL",
    0x3202: "MP4_SOURCE_SET",
    0x3203: "MP4_STATUS_GET",
    0x3204: "MP4_STATUS_RSP",
}


def _ticks_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.time() * 1000)


def _ticks_diff(a, b):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(a, b)
    return a - b


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000.0)


def _xorshift32(x):
    x &= 0xFFFFFFFF
    x ^= ((x << 13) & 0xFFFFFFFF)
    x ^= (x >> 17)
    x ^= ((x << 5) & 0xFFFFFFFF)
    return x & 0xFFFFFFFF


def _fill_prng(buf, state):
    mv = memoryview(buf)
    ln = len(mv)
    off = 0
    while off + 4 <= ln:
        state = _xorshift32(state)
        struct.pack_into("<I", mv, off, state)
        off += 4
    if off < ln:
        state = _xorshift32(state)
        tail = struct.pack("<I", state)
        mv[off:] = tail[: ln - off]
    return state


def _iter_test_bytes(total_size, chunk_size, seed):
    if chunk_size <= 0:
        chunk_size = 1024
    if chunk_size > 4096:
        chunk_size = 4096
    remain = int(total_size)
    state = int(seed) & 0xFFFFFFFF
    buf = bytearray(chunk_size)
    while remain > 0:
        n = chunk_size if remain >= chunk_size else remain
        if n != len(buf):
            buf = bytearray(n)
        state = _fill_prng(buf, state)
        yield buf
        remain -= n


def _calc_sha256(total_size, chunk_size, seed):
    if hashlib is None:
        raise RuntimeError("hashlib/uhashlib not available")
    h = hashlib.sha256()
    for blk in _iter_test_bytes(total_size, chunk_size, seed):
        h.update(blk)
    return h.digest()


def _uart_open(uart_id, baudrate, tx=None, rx=None, timeout=0, timeout_char=0):
    if not IS_MICROPYTHON:
        raise RuntimeError("UART requires MicroPython")
    import machine
    try:
        return machine.UART(
            int(uart_id),
            baudrate=int(baudrate),
            bits=8,
            parity=None,
            stop=1,
            tx=machine.Pin(tx) if tx is not None else None,
            rx=machine.Pin(rx) if rx is not None else None,
            timeout=int(timeout),
            timeout_char=int(timeout_char),
        )
    except TypeError:
        return machine.UART(
            int(uart_id),
            baudrate=int(baudrate),
            bits=8,
            parity=None,
            stop=1,
            tx=tx,
            rx=rx,
            timeout=int(timeout),
            timeout_char=int(timeout_char),
        )


def _read_uart(uart, into_buf):
    try:
        n = uart.readinto(into_buf)
        if n is None:
            return 0
        return n
    except Exception:
        try:
            raw = uart.read(len(into_buf))
            if not raw:
                return 0
            n = len(raw)
            into_buf[:n] = raw
            return n
        except Exception:
            return 0


def _hex_preview(data, max_len=32):
    if data is None:
        return "<None>"
    d = bytes(data)
    if len(d) <= max_len:
        return binascii.hexlify(d).decode()
    return binascii.hexlify(d[:max_len]).decode() + "..."


def _parse_ack_payload(payload):
    if len(payload) < 6:
        return None, None
    fid = payload[0] | (payload[1] << 8)
    aoff = payload[2] | (payload[3] << 8) | (payload[4] << 16) | (payload[5] << 24)
    return fid, aoff


def _parse_query_rsp_payload(payload):
    if len(payload) < 1 + 32 + 4 + 2:
        return None
    exists = payload[0]
    got_sha = bytes(payload[1:33])
    size = payload[33] | (payload[34] << 8) | (payload[35] << 16) | (payload[36] << 24)
    path_len = payload[37] | (payload[38] << 8)
    got_path = ""
    if 39 + path_len <= len(payload):
        try:
            got_path = bytes(payload[39:39 + path_len]).decode("utf-8")
        except Exception:
            got_path = ""
    return exists, got_sha, size, got_path


def _wait_packet(uart, parser, want_cmd, timeout_ms,
                 expected_fid=None, expected_off=None,
                 expected_size=None, expected_sha=None,
                 debug=False, drain_ms=0):
    start = _ticks_ms()
    tmp = bytearray(512)

    if drain_ms and drain_ms > 0:
        ds = _ticks_ms()
        while _ticks_diff(_ticks_ms(), ds) < int(drain_ms):
            n = _read_uart(uart, tmp)
            if n <= 0:
                _sleep_ms(2)
                continue
            parser.feed(memoryview(tmp)[:n])
            for _ in parser.pop():
                pass

    while _ticks_diff(_ticks_ms(), start) < int(timeout_ms):
        n = _read_uart(uart, tmp)
        if n <= 0:
            _sleep_ms(2)
            continue
        parser.feed(memoryview(tmp)[:n])
        for ver, addr, cmd, payload in parser.pop():
            if debug:
                name = _CMD_NAMES.get(cmd, "UNKNOWN")
                print("DEBUG RX cmd=0x{:04X}({}) len={} hex={}".format(
                    int(cmd), name, len(payload), _hex_preview(payload)))
            if cmd != want_cmd:
                continue
            if cmd == 0x2004 and expected_fid is not None:
                fid, aoff = _parse_ack_payload(payload)
                if fid != expected_fid or aoff != expected_off:
                    if debug:
                        print("DEBUG ACK mismatch: fid={} exp={} aoff={} exp={}".format(
                            int(fid), int(expected_fid), int(aoff), int(expected_off)))
                    continue
            elif cmd == 0x2006 and expected_size is not None:
                rsp = _parse_query_rsp_payload(payload)
                if rsp is None or rsp[0] != 1:
                    continue
                if rsp[2] != expected_size:
                    continue
                if expected_sha is not None and rsp[1] != expected_sha:
                    continue
            return ver, addr, cmd, payload
    return None


def _wait_any_cmd(uart, parser, want_cmd, timeout_ms, debug=False, drain_ms=0):
    start = _ticks_ms()
    tmp = bytearray(512)

    if drain_ms and drain_ms > 0:
        ds = _ticks_ms()
        while _ticks_diff(_ticks_ms(), ds) < int(drain_ms):
            n = _read_uart(uart, tmp)
            if n <= 0:
                _sleep_ms(2)
                continue
            parser.feed(memoryview(tmp)[:n])
            for _ in parser.pop():
                pass

    while _ticks_diff(_ticks_ms(), start) < int(timeout_ms):
        n = _read_uart(uart, tmp)
        if n <= 0:
            _sleep_ms(2)
            continue
        parser.feed(memoryview(tmp)[:n])
        for ver, addr, cmd, payload in parser.pop():
            if debug:
                name = _CMD_NAMES.get(cmd, "UNKNOWN")
                print("DEBUG RX cmd=0x{:04X}({}) len={} hex={}".format(
                    int(cmd), name, len(payload), _hex_preview(payload)))
            if cmd == want_cmd:
                return ver, addr, cmd, payload
    return None


def _mp4_wait_status(uart, parser, store, timeout_ms=2000, debug=False):
    pkt = _wait_any_cmd(uart, parser, 0x3204, timeout_ms, debug=debug)
    if pkt is None:
        return None
    _, _, _, payload = pkt
    cmd_def = store.get(0x3204)
    return SchemaCodec.decode(cmd_def, payload, store=store)


def run_mp4_smoke(
    uart_id=1,
    baudrate=115200,
    tx=None,
    rx=None,
    source="output.jpk",
    mode=1,
    start=0,
    range=0xFFFFFFFF,
    seek_frame=0,
    require_rsp=False,
    debug=False,
):
    store = SchemaStore("/schema")
    store.finalize()

    def _try_once(tx2, rx2):
        print("MP4 init UART{} baud={} tx={} rx={}".format(
            int(uart_id), int(baudrate), tx2, rx2))
        uart2 = _uart_open(uart_id, baudrate, tx=tx2, rx=rx2, timeout=0, timeout_char=0)
        try:
            uart2.read()
        except Exception:
            pass

        parser2 = StreamParser(max_len=16384)
        src_def = store.get(0x3202)
        src_payload = SchemaCodec.encode(src_def, {
            "source": str(source),
            "mode": int(mode),
            "start": int(start),
            "range": int(range),
        })
        uart2.write(Proto.pack(0x3202, src_payload))
        st2 = _mp4_wait_status(uart2, parser2, store, debug=debug)
        if st2 is None:
            return None, uart2, parser2
        print("MP4 status after SOURCE_SET:", st2)
        return st2, uart2, parser2

    st, uart, parser = _try_once(tx, rx)
    if st is None and tx is not None and rx is not None and int(tx) != int(rx):
        st, uart, parser = _try_once(rx, tx)
    if st is None and require_rsp:
        raise RuntimeError("No MP4_STATUS_RSP after SOURCE_SET")
    if st is None:
        print("WARN: No MP4_STATUS_RSP after SOURCE_SET")

    ctl_def = store.get(0x3201)

    pause_payload = SchemaCodec.encode(ctl_def, {"action": 2, "value": 1})
    uart.write(Proto.pack(0x3201, pause_payload))
    st = _mp4_wait_status(uart, parser, store, debug=debug)
    if st is None and require_rsp:
        raise RuntimeError("No MP4_STATUS_RSP after PAUSE")
    if st is None:
        print("WARN: No MP4_STATUS_RSP after PAUSE")
    else:
        print("MP4 status after PAUSE:", st)

    resume_payload = SchemaCodec.encode(ctl_def, {"action": 2, "value": 0})
    uart.write(Proto.pack(0x3201, resume_payload))
    st = _mp4_wait_status(uart, parser, store, debug=debug)
    if st is None and require_rsp:
        raise RuntimeError("No MP4_STATUS_RSP after RESUME")
    if st is None:
        print("WARN: No MP4_STATUS_RSP after RESUME")
    else:
        print("MP4 status after RESUME:", st)

    if seek_frame and int(seek_frame) > 0:
        seek_payload = SchemaCodec.encode(ctl_def, {"action": 3, "value": int(seek_frame)})
        uart.write(Proto.pack(0x3201, seek_payload))
        st = _mp4_wait_status(uart, parser, store, debug=debug)
        if st is None and require_rsp:
            raise RuntimeError("No MP4_STATUS_RSP after SEEK")
        if st is None:
            print("WARN: No MP4_STATUS_RSP after SEEK")
        else:
            print("MP4 status after SEEK:", st)

        _sleep_ms(50)
        uart.write(Proto.pack(0x3203, b""))
        st = _mp4_wait_status(uart, parser, store, debug=debug)
        if st is None and require_rsp:
            raise RuntimeError("No MP4_STATUS_RSP after STATUS_GET")
        if st is None:
            print("WARN: No MP4_STATUS_RSP after STATUS_GET")
        else:
            print("MP4 status after STATUS_GET:", st)

    stop_payload = SchemaCodec.encode(ctl_def, {"action": 0, "value": 0})
    uart.write(Proto.pack(0x3201, stop_payload))
    st = _mp4_wait_status(uart, parser, store, debug=debug)
    if st is None and require_rsp:
        raise RuntimeError("No MP4_STATUS_RSP after STOP")
    if st is None:
        print("WARN: No MP4_STATUS_RSP after STOP")
    else:
        print("MP4 status after STOP:", st)

    play_payload = SchemaCodec.encode(ctl_def, {"action": 1, "value": 0})
    uart.write(Proto.pack(0x3201, play_payload))
    st = _mp4_wait_status(uart, parser, store, debug=debug)
    if st is None and require_rsp:
        raise RuntimeError("No MP4_STATUS_RSP after PLAY")
    if st is None:
        print("WARN: No MP4_STATUS_RSP after PLAY")
    else:
        print("MP4 status after PLAY:", st)
    return True


def run_mp4_smoke_quick(uart_id=1, baudrate=115200, tx=None, rx=None, debug=False):
    return run_mp4_smoke(
        uart_id=uart_id,
        baudrate=baudrate,
        tx=tx,
        rx=rx,
        source="output.jpk",
        mode=1,
        start=0,
        range=0xFFFFFFFF,
        seek_frame=0,
        debug=debug,
    )


def _input_line(prompt):
    try:
        return input(prompt)
    except Exception:
        try:
            print(prompt, end="")
        except Exception:
            pass
        return ""


def _parse_int_or_default(s, default):
    if s is None:
        return default
    ss = str(s).strip()
    if not ss:
        return default
    try:
        return int(ss, 0)
    except Exception:
        try:
            return int(ss)
        except Exception:
            return default


def run_mp4_interactive(
    uart_id=1,
    baudrate=115200,
    tx=None,
    rx=None,
    debug=False,
):
    store = SchemaStore("/schema")
    store.finalize()

    def _open_try(tx2, rx2):
        print("MP4 init UART{} baud={} tx={} rx={}".format(
            int(uart_id), int(baudrate), tx2, rx2))
        uart2 = _uart_open(uart_id, baudrate, tx=tx2, rx=rx2, timeout=0, timeout_char=0)
        try:
            uart2.read()
        except Exception:
            pass
        return uart2, StreamParser(max_len=16384)

    uart, parser = _open_try(tx, rx)
    ok = False
    uart.write(Proto.pack(0x3203, b""))
    st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
    if st is not None:
        ok = True
        print("MP4 status:", st)
    elif tx is not None and rx is not None and int(tx) != int(rx):
        uart, parser = _open_try(rx, tx)
        uart.write(Proto.pack(0x3203, b""))
        st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
        if st is not None:
            ok = True
            print("MP4 status:", st)

    if not ok:
        print("警告：收不到 MP4_STATUS_RSP（仍會繼續發送指令）")

    src = "output.jpk"
    mode = 1
    start = 0
    span = 0xFFFFFFFF

    src_def = store.get(0x3202)
    ctl_def = store.get(0x3201)
    last_st = st

    while True:
        print("")
        print("1) 播放  2) 暫停  3) 跳轉  4) 設定來源  5) 狀態  6) 停止  0) 離開")
        cmd = _input_line("> ")
        cmd = "" if cmd is None else str(cmd).strip().lower()
        if cmd in ("0", "q", "quit", "exit"):
            return True
        if cmd in ("1", "play"):
            payload = SchemaCodec.encode(ctl_def, {"action": 1, "value": 0})
            uart.write(Proto.pack(0x3201, payload))
            st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
            if st is not None:
                print("MP4 狀態:", st)
                last_st = st
            continue
        if cmd in ("2", "pause"):
            payload = SchemaCodec.encode(ctl_def, {"action": 2, "value": 1})
            uart.write(Proto.pack(0x3201, payload))
            st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
            if st is not None:
                print("MP4 狀態:", st)
                last_st = st
            continue
        if cmd in ("3", "seek"):
            s = _input_line("跳轉到 frame（絕對）> ")
            seek_frame = _parse_int_or_default(s, 0)
            s = _input_line("同時更新播放範圍？(y/N)> ")
            yes = ("" if s is None else str(s).strip().lower()) in ("y", "yes", "1")
            if yes:
                start = int(seek_frame)
                default_range = -1 if int(span) == 0xFFFFFFFF else int(span)
                s = _input_line("新的範圍 range（幀數，-1=到最後）[{}]> ".format(str(int(default_range))))
                r = _parse_int_or_default(s, int(default_range))
                span = 0xFFFFFFFF if int(r) < 0 else int(r)
                payload = SchemaCodec.encode(src_def, {
                    "source": str(src),
                    "mode": int(mode),
                    "start": int(start),
                    "range": int(span),
                })
                uart.write(Proto.pack(0x3202, payload))
                st = _mp4_wait_status(uart, parser, store, timeout_ms=1200, debug=debug)
                if st is not None:
                    print("MP4 狀態:", st)
                    last_st = st
            else:
                print("實際送出 seek frame =", int(seek_frame))
                payload = SchemaCodec.encode(ctl_def, {"action": 3, "value": int(seek_frame)})
                uart.write(Proto.pack(0x3201, payload))
                st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
                if st is not None:
                    print("MP4 狀態:", st)
                    last_st = st
            continue
        if cmd in ("4", "source", "src"):
            s = _input_line("來源 source [{}]> ".format(src))
            s = "" if s is None else str(s).strip()
            if s:
                src = s
            s = _input_line("模式 mode [{}] (0=auto 1=pack 2=folder)> ".format(int(mode)))
            mode = _parse_int_or_default(s, int(mode))
            s = _input_line("起點 start [{}]> ".format(int(start)))
            start = _parse_int_or_default(s, int(start))
            default_range = -1 if int(span) == 0xFFFFFFFF else int(span)
            s = _input_line("範圍 range（幀數，-1=到最後）[{}]> ".format(str(int(default_range))))
            r = _parse_int_or_default(s, int(default_range))
            span = 0xFFFFFFFF if int(r) < 0 else int(r)
            payload = SchemaCodec.encode(src_def, {
                "source": str(src),
                "mode": int(mode),
                "start": int(start),
                "range": int(span),
            })
            uart.write(Proto.pack(0x3202, payload))
            st = _mp4_wait_status(uart, parser, store, timeout_ms=1200, debug=debug)
            if st is not None:
                print("MP4 狀態:", st)
                last_st = st
            continue
        if cmd in ("5", "status", "get"):
            uart.write(Proto.pack(0x3203, b""))
            st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
            if st is not None:
                print("MP4 狀態:", st)
                last_st = st
            continue
        if cmd in ("6", "stop"):
            payload = SchemaCodec.encode(ctl_def, {"action": 0, "value": 0})
            uart.write(Proto.pack(0x3201, payload))
            st = _mp4_wait_status(uart, parser, store, timeout_ms=800, debug=debug)
            if st is not None:
                print("MP4 狀態:", st)
                last_st = st
            continue


def run_master(
    uart_id=1,
    baudrate=115200,
    tx=None,
    rx=None,
    path="/test_500kb.bin",
    total_size=512000,
    chunk_size=1024,
    seed=0xC0FFEE,
    file_id=None,
    ack_timeout_ms=2000,
    ack_retry=8,
    log_interval_ms=1000,
    debug=False,
):
    print("MASTER init UART{} baud={} tx={} rx={}".format(
        int(uart_id), int(baudrate), tx, rx))
    uart = _uart_open(uart_id, baudrate, tx=tx, rx=rx, timeout=0, timeout_char=0)
    try:
        uart.read()
    except Exception:
        pass

    store = SchemaStore("/schema")
    store.finalize()

    if file_id is None:
        file_id = _ticks_ms() & 0xFFFF

    sha = _calc_sha256(total_size, chunk_size, seed)
    print("TEST file_id={} size={} chunk={} sha={}".format(
        int(file_id), int(total_size), int(chunk_size), binascii.hexlify(sha).decode()
    ))

    begin_def = store.get(0x2001)
    begin_payload = SchemaCodec.encode(begin_def, {
        "file_id": int(file_id),
        "total_size": int(total_size),
        "chunk_size": int(chunk_size),
        "sha256": sha,
        "path": str(path),
    })
    raw_begin = Proto.pack(0x2001, begin_payload)
    uart.write(raw_begin)
    print("SEND FILE_BEGIN {} bytes".format(len(raw_begin)))

    parser = StreamParser(max_len=16384)
    sent = 0
    off = 0
    last_log = _ticks_ms()
    chunk_idx = 0

    for blk in _iter_test_bytes(total_size, chunk_size, seed):
        ok = False
        for retry in range(int(ack_retry)):
            chunk_def = store.get(0x2002)
            chunk_payload = SchemaCodec.encode(chunk_def, {
                "file_id": int(file_id),
                "offset": int(off),
                "data": blk,
            })
            raw_chunk = Proto.pack(0x2002, chunk_payload)
            uart.write(raw_chunk)

            if debug and retry == 0:
                print("SEND chunk#{} off={} len={}".format(
                    int(chunk_idx), int(off), len(raw_chunk)))

            pkt = _wait_packet(
                uart, parser, 0x2004, ack_timeout_ms,
                expected_fid=int(file_id), expected_off=int(off),
                debug=debug,
            )
            if pkt is not None:
                ok = True
                break
            if debug:
                print("DEBUG retry={} no ACK for off={}".format(int(retry), int(off)))
            _sleep_ms(10)
        if not ok:
            raise RuntimeError("ACK timeout at offset {} (chunk #{})".format(
                int(off), int(chunk_idx)))

        off += len(blk)
        sent += len(blk)
        chunk_idx += 1
        now = _ticks_ms()
        if _ticks_diff(now, last_log) >= int(log_interval_ms):
            last_log = now
            print("PROGRESS {}/{}".format(int(sent), int(total_size)))
            gc.collect()

    end_def = store.get(0x2003)
    end_payload = SchemaCodec.encode(end_def, {"file_id": int(file_id)})
    uart.write(Proto.pack(0x2003, end_payload))
    print("SEND FILE_END")

    pkt = _wait_packet(
        uart, parser, 0x2006, 60000,
        expected_size=int(total_size), expected_sha=sha,
        debug=debug,
    )
    if pkt is None:
        raise RuntimeError("No final FILE_QUERY_RSP (0x2006) matched")

    _, _, _, payload = pkt
    rsp = _parse_query_rsp_payload(payload)
    if rsp is None:
        raise RuntimeError("Failed to parse FILE_QUERY_RSP")
    _, got_sha, size, got_path = rsp

    print("DONE size={} sha={} path={}".format(
        int(size), binascii.hexlify(got_sha).decode(), got_path
    ))
    return True


def run_master_quick(uart_id=1, baudrate=115200, tx=None, rx=None, debug=False):
    return run_master(
        uart_id=uart_id,
        baudrate=baudrate,
        tx=tx,
        rx=rx,
        path="/test_500kb.bin",
        total_size=4096,
        chunk_size=1024,
        seed=0xC0FFEE,
        debug=debug,
    )


def main():
    argv = getattr(sys, "argv", None)
    mode = "mp4i"
    tx = 18
    rx = 8
    if argv and len(argv) >= 2:
        mode = str(argv[1] or "").strip().lower()
    if argv and len(argv) >= 4:
        try:
            tx = int(argv[2])
            rx = int(argv[3])
        except Exception:
            tx = 8
            rx = 18
    if mode in ("file", "uart_file"):
        return run_master_quick(uart_id=1, baudrate=115200, tx=tx, rx=rx, debug=True)
    if mode in ("mp4i", "i", "interactive"):
        return run_mp4_interactive(uart_id=1, baudrate=115200, tx=tx, rx=rx, debug=True)
    if mode in ("all", "full"):
        run_master_quick(uart_id=1, baudrate=115200, tx=tx, rx=rx, debug=True)
        _sleep_ms(200)
        return run_mp4_smoke_quick(uart_id=1, baudrate=115200, tx=tx, rx=rx, debug=True)
    return run_mp4_smoke_quick(uart_id=1, baudrate=115200, tx=tx, rx=rx, debug=True)


if __name__ == "__main__":
    main()
