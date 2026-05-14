import time
import gc
import struct
from lib.proto import Proto
from lib.schema_codec import SchemaCodec
from lib.buffer_hub import AtomicStreamHub

CMD_RAM_BENCH_START = 0x1811
CMD_RAM_BENCH_CHUNK = 0x1812
CMD_RAM_BENCH_STOP = 0x1813
CMD_RAM_BENCH_REPORT = 0x1814

_SESS = {}

def _send_report(ctx, report):
    app = ctx.get("app")
    send_func = ctx.get("send")
    if not app or not send_func:
        return
    cmd_def = app.store.get(CMD_RAM_BENCH_REPORT)
    if not cmd_def:
        return
    payload = SchemaCodec.encode(cmd_def, report)
    pkt = Proto.pack(CMD_RAM_BENCH_REPORT, payload)
    send_func(pkt)

def _ring_write(sess, data):
    ring = sess.get("ring")
    if ring is None:
        return
    rlen = sess.get("ring_len", 0)
    if rlen <= 0:
        return
    pos = sess.get("pos", 0)
    ln = len(data)
    if ln <= 0:
        return
    if ln >= rlen:
        ring[:] = data[-rlen:]
        sess["pos"] = 0
        return
    end = pos + ln
    if end <= rlen:
        ring[pos:end] = data
        pos = end
        if pos >= rlen:
            pos = 0
        sess["pos"] = pos
        return
    first = rlen - pos
    ring[pos:] = data[:first]
    rest = ln - first
    if rest:
        ring[:rest] = data[first:]
    sess["pos"] = rest

def on_ram_bench_start(ctx, args):
    run_id = int(args.get("run_id", 0)) & 0xFFFF
    total_size = int(args.get("total_size", 0)) & 0xFFFFFFFF
    chunk_size = int(args.get("chunk_size", 0)) & 0xFFFF
    mode = int(args.get("mode", 0)) & 0xFF
    ring_kb = int(args.get("ring_kb", 0)) & 0xFFFF
    ring = None
    ring_len = 0
    hub = None
    hub_buf_size = 0
    hub_drops = 0
    if mode == 1:
        ring_len = ring_kb * 1024
        if ring_len <= 0:
            ring_len = chunk_size * 4
        if ring_len > 0:
            try:
                ring = bytearray(ring_len)
            except MemoryError:
                ring = None
                ring_len = 0
    elif mode == 2:
        nbuf = ring_kb
        if nbuf <= 0:
            nbuf = 4
        if nbuf > 32:
            nbuf = 32
        hub_buf_size = chunk_size + 2
        try:
            hub = AtomicStreamHub(hub_buf_size, num_buffers=nbuf)
        except MemoryError:
            hub = None
            hub_buf_size = 0
    _SESS[run_id] = {
        "start_ms": time.ticks_ms(),
        "bytes": 0,
        "chunks": 0,
        "total_size": total_size,
        "chunk_size": chunk_size,
        "mode": mode,
        "ring": ring,
        "ring_len": ring_len,
        "pos": 0,
        "hub": hub,
        "hub_buf_size": hub_buf_size,
        "hub_drops": hub_drops,
        "mem_free_start": gc.mem_free(),
    }

def on_ram_bench_chunk(ctx, args):
    run_id = int(args.get("run_id", 0)) & 0xFFFF
    sess = _SESS.get(run_id)
    if not sess:
        return
    data = args.get("data")
    if data is None:
        return
    ln = len(data)
    if ln <= 0:
        return
    mode = sess.get("mode", 0)
    if mode == 1:
        _ring_write(sess, data)
    elif mode == 2:
        hub = sess.get("hub")
        if hub is None:
            sess["hub_drops"] = (sess.get("hub_drops", 0) + 1) & 0xFFFFFFFF
            return
        if ln + 2 > sess.get("hub_buf_size", 0):
            sess["hub_drops"] = (sess.get("hub_drops", 0) + 1) & 0xFFFFFFFF
            return
        w = hub.get_write_view()
        if w is None:
            hub.get_read_view()
            w = hub.get_write_view()
            if w is None:
                sess["hub_drops"] = (sess.get("hub_drops", 0) + 1) & 0xFFFFFFFF
                return
        struct.pack_into("<H", w, 0, ln)
        w[2:2 + ln] = data
        hub.commit()
    sess["bytes"] = (sess.get("bytes", 0) + ln) & 0xFFFFFFFF
    sess["chunks"] = (sess.get("chunks", 0) + 1) & 0xFFFFFFFF

def on_ram_bench_stop(ctx, args):
    run_id = int(args.get("run_id", 0)) & 0xFFFF
    sess = _SESS.pop(run_id, None)
    if not sess:
        return
    end_ms = time.ticks_ms()
    elapsed_ms = time.ticks_diff(end_ms, sess.get("start_ms", end_ms))
    if elapsed_ms <= 0:
        elapsed_ms = 1
    total_bytes = int(sess.get("bytes", 0)) & 0xFFFFFFFF
    chunks = int(sess.get("chunks", 0)) & 0xFFFFFFFF
    mb_s_x1000 = (total_bytes * 1000 * 1000) // (elapsed_ms * 1048576)
    report = {
        "run_id": run_id,
        "bytes": total_bytes,
        "chunks": chunks,
        "elapsed_ms": int(elapsed_ms) & 0xFFFFFFFF,
        "mb_s_x1000": int(mb_s_x1000) & 0xFFFFFFFF,
    }
    _send_report(ctx, report)

def register(app):
    app.disp.on(CMD_RAM_BENCH_START, on_ram_bench_start)
    app.disp.on(CMD_RAM_BENCH_CHUNK, on_ram_bench_chunk)
    app.disp.on(CMD_RAM_BENCH_STOP, on_ram_bench_stop)
    print("✅ [Action] RAM bench actions registered")
