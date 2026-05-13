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


run_master_quick(uart_id=1, baudrate=115200, tx=8, rx=18, debug=True)