from lib.sys_bus import bus
from lib.proto import Proto
from lib.schema_codec import SchemaCodec
from lib.dp_manager_service import ensure_dp_manager_service, configure_from_dp_config, load_dp_config
from lib.dp_buffer_service import ensure_dp_buffer_service, configure_for_layout
from lib.jpeg_hooks import get_hook_runner


def _send_status(ctx, playing, frame, total, fps, err):
    app = ctx["app"]
    cmd_def = app.store.get(0x3106)
    payload = SchemaCodec.encode(cmd_def, {
        "playing": int(playing),
        "frame": int(frame),
        "total": int(total),
        "fps": int(fps or 0),
        "err": str(err or ""),
    })
    pkt = Proto.pack(0x3106, payload)
    if "send" in ctx:
        ctx["send"](pkt)


def _get_dp():
    return bus.get_service("dp_manager")


def _get_buf():
    return bus.get_service("dp_buffer")


def _flush_pipeline(svc):
    jpeg_in = svc.get("jpeg_in") if svc else None
    if jpeg_in:
        jpeg_in.flush()
    buf = _get_buf()
    if buf:
        buf["pending"] = None
        jpeg_out = buf.get("jpeg_out")
        if jpeg_out:
            jpeg_out.flush()
        out_hub = buf.get("out_hub")
        if out_hub:
            out_hub.flush()


def on_jpeg_player_ctl(ctx, args):
    action = int(args.get("action", 0) or 0)
    seek_frame = int(args.get("seek_frame", 0) or 0)

    svc = ensure_dp_manager_service(bus)
    buf = ensure_dp_buffer_service(bus)

    if action == 0:
        svc["enable"] = False
        buf["enable"] = False
        svc["sch_i"] = 0
        _flush_pipeline(svc)
        bus.shared["jpeg_player"] = {"playing": False, "paused": False}
        print("⏹ [JPEG] Stopped")
        _send_status(ctx, 0, 0, len(svc.get("schedule") or []), 0, "")

    elif action == 1:
        schedule = svc.get("schedule") or []
        total = len(schedule)
        if seek_frame > 0 and total > 0:
            svc["sch_i"] = int(seek_frame) % total
        _flush_pipeline(svc)
        svc["enable"] = True
        buf["enable"] = True
        bus.shared["jpeg_player"] = {"playing": True, "paused": False}
        print(f"▶️ [JPEG] Play from frame {svc.get('sch_i', 0)}")
        _send_status(ctx, 1, int(svc.get("sch_i", 0)), total, 0, "")

    elif action == 2:
        svc["enable"] = False
        bus.shared["jpeg_player"] = {"playing": False, "paused": True}
        print("⏸ [JPEG] Paused")
        _send_status(ctx, 0, int(svc.get("sch_i", 0)), len(svc.get("schedule") or []), 0, "")


def on_jpeg_config_load(ctx, args):
    config_path = str(args.get("config_path", "") or "")
    if not config_path:
        config_path = "/dp_config.json"

    try:
        dp = load_dp_config(config_path)
        svc = configure_from_dp_config(bus, dp, dp_config_path=config_path)
        pixel_format = (svc.get("jpeg") or {}).get("pixel_format", "RGB565_BE")
        frame_bufs = int(bus.shared.get("pipeline_frame_buffers", 3) or 3)
        configure_for_layout(bus, svc.get("layout") or [], pixel_format=pixel_format, num_buffers=frame_bufs)
        total = len(svc.get("schedule") or [])
        print(f"📂 [JPEG] Config loaded: {config_path} frames={total}")
        _send_status(ctx, 0, 0, total, 0, "")
    except Exception as e:
        err = str(e)
        print(f"❌ [JPEG] Config load failed: {err}")
        _send_status(ctx, 0, 0, 0, 0, err)


def on_jpeg_player_params(ctx, args):
    svc = ensure_dp_manager_service(bus)
    loop = int(args.get("loop", 255) or 255)
    blend_mode = int(args.get("blend_mode", 255) or 255)

    if loop != 255:
        loop_flag = bool(loop)
        bus.shared["jpeg_loop"] = loop_flag
        print(f"⚙ [JPEG] loop={loop_flag}")

    if blend_mode != 255:
        mode_str = "blit" if blend_mode else "interleave"
        bus.shared["jpeg_blend_mode"] = mode_str
        print(f"⚙ [JPEG] blend_mode={mode_str}")
        cfg_path = str(svc.get("dp_config_path") or "/dp_config.json")
        try:
            dp = load_dp_config(cfg_path)
            dp.setdefault("player", {})["blend_mode"] = mode_str
            configure_from_dp_config(bus, dp, dp_config_path=cfg_path)
            frame_bufs = int(bus.shared.get("pipeline_frame_buffers", 3) or 3)
            configure_for_layout(bus, svc.get("layout") or [],
                                 pixel_format=(svc.get("jpeg") or {}).get("pixel_format", "RGB565_BE"),
                                 num_buffers=frame_bufs)
            print(f"⚙ [JPEG] blend_mode applied, schedule rebuilt")
        except Exception as e:
            print(f"❌ [JPEG] blend_mode apply failed: {e}")

    total = len(svc.get("schedule") or [])
    _send_status(ctx, 0, int(svc.get("sch_i", 0)), total, 0, "")


def on_jpeg_hook_ctl(ctx, args):
    enable = bool(args.get("enable", 0) or 0)
    hook_type = int(args.get("hook_type", 0) or 0)
    config_json = str(args.get("config_json", "") or "")

    buf = ensure_dp_buffer_service(bus)

    if enable:
        runner = get_hook_runner()
        if config_json:
            runner.set_config(config_json)
        buf["hook"] = runner
        buf["hook_enable"] = True
        print(f"🖌 [JPEG] Hook enabled type={hook_type}")
    else:
        buf["hook"] = None
        buf["hook_enable"] = False
        print("🖌 [JPEG] Hook disabled")

    total = len((_get_dp() or {}).get("schedule") or [])
    _send_status(ctx, 0, 0, total, 0, "")


def on_jpeg_status_get(ctx, args):
    svc = _get_dp()
    if svc is None:
        _send_status(ctx, 0, 0, 0, 0, "dp_manager not ready")
        return

    player = bus.shared.get("jpeg_player") or {}
    playing = 1 if player.get("playing") else 0
    frame = int(svc.get("sch_i", 0) or 0)
    total = len(svc.get("schedule") or [])
    err = str(svc.get("last_err") or "")
    buf = _get_buf()
    fps = 0
    if buf is not None:
        fps = int(buf.get("fps_current", 0) or 0)
    _send_status(ctx, playing, frame, total, fps, err)


def register(app):
    app.disp.on(0x3101, on_jpeg_player_ctl)
    app.disp.on(0x3102, on_jpeg_config_load)
    app.disp.on(0x3103, on_jpeg_player_params)
    app.disp.on(0x3104, on_jpeg_hook_ctl)
    app.disp.on(0x3105, on_jpeg_status_get)
    print("✅ [Action] JPEG actions registered")
