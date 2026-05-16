import os

from lib.sys_bus import bus
from lib.proto import Proto
from lib.schema_codec import SchemaCodec


MODE_AUTO = 0
MODE_PACK = 1
MODE_FOLDER = 2


def _get_player_state():
    st = bus.shared.get("mp4_player")
    if not isinstance(st, dict):
        st = {}
        bus.shared["mp4_player"] = st
    return st


def _send_status(ctx, *, playing, paused, mode, frame, total, source, err):
    app = ctx["app"]
    cmd_def = app.store.get(0x3204)
    payload = SchemaCodec.encode(cmd_def, {
        "playing": int(playing),
        "paused": int(paused),
        "mode": int(mode),
        "frame": int(frame),
        "total": int(total),
        "source": str(source or ""),
        "err": str(err or ""),
    })
    pkt = Proto.pack(0x3204, payload)
    if "send" in ctx and ctx["send"]:
        ctx["send"](pkt)


def _status_snapshot():
    st = _get_player_state()
    playing = 1 if st.get("playing") else 0
    paused = 1 if st.get("paused") else 0
    mode = int(st.get("mode", 0) or 0)
    frame = int(st.get("frame", 0) or 0)
    total = int(st.get("total", 0) or 0)
    source = str(st.get("source", "") or "")
    err = str(st.get("err", "") or "")
    return playing, paused, mode, frame, total, source, err


def on_mp4_player_ctl(ctx, args):
    action = int(args.get("action", 0) or 0)
    value = int(args.get("value", 0) or 0)

    st = _get_player_state()
    st["err"] = ""

    if action == 0:
        bus.shared["mp4_paused"] = False
        bus.shared["mp4_playing"] = False
        bus.shared["mp4_seek"] = 0
        bus.shared["mp4_flush"] = 1
        st["playing"] = False
        st["paused"] = False
        _send_status(ctx, playing=0, paused=0, mode=st.get("mode", 0), frame=st.get("frame", 0), total=st.get("total", 0), source=st.get("source", ""), err="")
        return

    if action == 1:
        bus.shared["mp4_playing"] = True
        bus.shared["mp4_paused"] = False
        st["playing"] = True
        st["paused"] = False
        _send_status(ctx, playing=1, paused=0, mode=st.get("mode", 0), frame=st.get("frame", 0), total=st.get("total", 0), source=st.get("source", ""), err="")
        return

    if action == 2:
        pause = 1 if value else 0
        bus.shared["mp4_paused"] = bool(pause)
        if pause:
            bus.shared["mp4_flush"] = 1
        st["paused"] = bool(pause)
        _send_status(ctx, playing=1 if st.get("playing") else 0, paused=pause, mode=st.get("mode", 0), frame=st.get("frame", 0), total=st.get("total", 0), source=st.get("source", ""), err="")
        return

    if action == 3:
        bus.shared["mp4_seek"] = value
        st["err"] = ""
        _send_status(ctx, playing=1 if st.get("playing") else 0, paused=1 if st.get("paused") else 0, mode=st.get("mode", 0), frame=st.get("frame", 0), total=st.get("total", 0), source=st.get("source", ""), err="")
        return


def on_mp4_source_set(ctx, args):
    source = str(args.get("source", "") or "").strip()
    mode = int(args.get("mode", 0) or 0)
    start = int(args.get("start", args.get("range_start", 0)) or 0)
    raw_range = args.get("range", None)
    if raw_range is None:
        end = int(args.get("range_end", 0xFFFFFFFF) or 0)
        span = 0xFFFFFFFF
    else:
        span = int(raw_range or 0)
        if span == 0xFFFFFFFF or span <= 0:
            end = 0xFFFFFFFF
        else:
            end = start + span - 1
    if not source:
        _send_status(ctx, playing=0, paused=0, mode=0, frame=0, total=0, source="", err="empty source")
        return

    st = _get_player_state()
    st["err"] = ""

    if mode not in (MODE_AUTO, MODE_PACK, MODE_FOLDER):
        mode = MODE_AUTO

    if mode == MODE_FOLDER:
        if source.endswith(".jpk"):
            mode = MODE_PACK

    if mode == MODE_AUTO:
        mode = MODE_PACK if source.endswith(".jpk") else MODE_FOLDER

    req = {
        "mode": int(mode),
        "source": source,
        "start": int(start),
        "range": int(span),
        "range_start": int(start),
        "range_end": int(end),
    }
    bus.shared["mp4_source_req"] = req

    st["mode"] = int(mode)
    st["source"] = source
    st["playing"] = True
    st["paused"] = False
    bus.shared["mp4_playing"] = True
    bus.shared["mp4_paused"] = False
    bus.shared["mp4_flush"] = 1

    playing, paused, mode2, frame, total, src2, err = _status_snapshot()
    _send_status(ctx, playing=playing, paused=paused, mode=mode2, frame=frame, total=total, source=src2, err=err)


def on_mp4_status_get(ctx, args):
    playing, paused, mode, frame, total, source, err = _status_snapshot()
    _send_status(ctx, playing=playing, paused=paused, mode=mode, frame=frame, total=total, source=source, err=err)


def register(app):
    app.disp.on(0x3201, on_mp4_player_ctl)
    app.disp.on(0x3202, on_mp4_source_set)
    app.disp.on(0x3203, on_mp4_status_get)
    print("✅ [Action] MP4 actions registered")
