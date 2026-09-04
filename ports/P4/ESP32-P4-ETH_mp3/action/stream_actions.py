# action/stream_actions.py
# stream (0x30xx) 命令層：只把 Master 指令寫成「離散命令」進 bus.shared，
# 由 StreamTask（tasks/stream_task.py）消費後執行讀檔 / 補幀 / 狀態管理。
#
# 本模組不再持有播放狀態（檔案 handle、供給鏈、播放次序都已移入 StreamTask）；
# 只保留對外回報用的 _STREAM_STATE 與 providers（status_actions 會讀）。

from lib.sys.sys_bus import bus

# ── 原始儲存數字（StreamTask 更新，slave 只存不換算；換算由 PC 端自己做）───
_STREAM_STATE = {
    "fps": 0,           # 0x3001 STREAM_INFO 收到的原始 fps（不做任何換算）
    "frame_count": 0,   # 供給鏈已 commit 的原始幀計數
    "mode": 0,          # 0x3009 的原始 play_mode
    "streaming": False, # 是否串流播放（原始旗標）
    "pos_frame": 0,     # 檔內絕對幀號（fp.tell()//frame_bytes，剛 commit 的那一幀）
                        # 進度回報專用: seek/暫停/循環後仍準確 (played_frames 會歸零不準)
}


def get_frame_count():
    return _STREAM_STATE["frame_count"]


def get_mode():
    return _STREAM_STATE["mode"]


def is_streaming():
    return bool(_STREAM_STATE["streaming"])


def on_stream_state_set(ctx, args):
    """0x3009: 準備檔案與播放模式 → stream_cmd_set"""
    bus.shared["stream_cmd_set"] = {
        "file_name": args["file_name"],
        "block_id": args["block_id"],
        "play_mode": args["play_mode"],
    }


def on_stream_play(ctx, args):
    """0x300A: 開始播放（可帶 start_frame 中途加入）。

    🔧 正常起播 (start_frame==0)：在 dispatch 裡「同步」把 is_streaming 設 True，
    讓 RenderTask 下一輪 loop 就起播，不再等 StreamTask 消費 stream_cmd_play 那
    一跳（那跳每台設備 1~10ms 隨機，是起播不同步的 slave 端來源之一）。
    StreamTask 仍會消費 stream_cmd_play 把狀態機推上 _PLAYING（冪等）。
    中途加入 (start_frame>0) 必須走 StreamTask 的 seek，不能提前設 streaming，
    否則 RenderTask 會在 seek 完成前渲染舊幀。
    """
    start_frame = int(args.get("start_frame", 0) or 0)
    if start_frame > 0:
        bus.shared["stream_cmd_play"] = {"start_frame": start_frame}
    else:
        bus.shared.update({"is_streaming": True, "is_paused": False})
        bus.shared["stream_cmd_play"] = {"start_frame": 0}


def on_stream_pause(ctx, args):
    """0x3005: 暫停/恢復 → stream_cmd_pause"""
    bus.shared["stream_cmd_pause"] = bool(args.get("pause", 0))


def on_stream_stop(ctx, args):
    """0x3002: 停止 → stream_cmd_stop"""
    bus.shared["stream_cmd_stop"] = True


def on_stream_seek(ctx, args):
    """0x3004: 跳轉 → stream_cmd_seek"""
    bus.shared["stream_cmd_seek"] = {
        "target_block": args.get("target_block", 0),
        "target_frame": args.get("target_frame", 0),
    }


def on_stream_info(ctx, args):
    """0x3001 STREAM_INFO: 主控廣播串流 fps。

    本機只儲存原始數字（_STREAM_STATE["fps"] + bus.shared["stream_fps_override"]），
    不做任何換算；RenderTask 偵測到變化時才換算一次節拍。
    """
    fps = args.get("fps", 0) or 0
    if fps > 0:
        _STREAM_STATE["fps"] = fps
        bus.shared["stream_fps_override"] = fps
        print("📡 [Stream] raw fps stored -> {}".format(fps))


def _direct_mode(ctx, args):
    """0x3003 Direct Mode: 直接寫入整幀 pixel_data（純網絡逐幀串流）。

    與檔案串流 (StreamTask) 不同，Direct Mode 不走 stream_cmd_* 命令，但
    RenderTask 只在 bus.shared["is_streaming"]=True 時才取幀渲染，所以這裡要把
    streaming 旗標自己架起來，否則幀寫進 hub 也不會上硬體。

    🔧 幀長與 hub slot (st_pixel.total_bytes) 可能不一致：config System.num_pixels
    是靜態值，實際 slot = 各 controller Q*4 加總（PCA9685 未接時會少 64B）。
    用 view 模式「多除小補」(與 StreamTask._fill_slot 同源)，否則 write_from
    整塊切片會因長度不符拋錯、整幀丟失。
    """
    hub = bus.get_service("pixel_stream")
    if hub is None:
        return
    view = hub.get_write_view()
    if view is not None:
        data = args.get("pixel_data", b"")
        k = min(len(data), len(view))
        view[:k] = data[:k]
        if len(view) > k:
            view[k:] = bytes(len(view) - k)   # 尾段補中性值（燈=0 熄滅）
        hub.commit()
    # 只設「渲染旗標」讓 RenderTask 出幀。⚠️ 不要碰 bus.shared["stream_active"]：
    # 那是「檔案串流供給鏈」的專屬旗標 (StreamTask 獨家管理)，PixelTask 看到它為
    # True 會直接讓位不計算本地燈效。direct mode 若把它設 True，會殘留並令
    # 本地燈效/配對模式 (0x3105) 全部停擺。
    bus.shared.update({
        "is_ready": True,
        "is_streaming": True,
        "is_paused": False,
    })
    # 對外狀態回報也要一致（0x1102 的 stream_active 讀這裡），否則 PC 端看不到 direct 在播
    _STREAM_STATE["streaming"] = True


def register(app):
    app.disp.on(0x3001, on_stream_info)    # INFO: 主控主動同步幀率（只存原始 fps）
    app.disp.on(0x3009, on_stream_state_set)  # SET
    app.disp.on(0x300A, on_stream_play)    # PLAY
    app.disp.on(0x3005, on_stream_pause)   # PAUSE
    app.disp.on(0x3002, on_stream_stop)    # STOP
    app.disp.on(0x3004, on_stream_seek)    # SEEK
    app.disp.on(0x3003, _direct_mode)      # Direct Mode

    # 原始數字 provider：主機（PC）經 0x1101 STATUS_GET 主動獲取，
    # 主機端自己加遮罩挑要的欄位、自己做換算。
    bus.register_provider("stream_fps", lambda: _STREAM_STATE["fps"])
    bus.register_provider("stream_frame_count", get_frame_count)
    bus.register_provider("stream_mode", get_mode)
    bus.register_provider("stream_active", is_streaming)
    # 檔內絕對幀號 (播放進度): seek/暫停/循環後仍準確
    bus.register_provider("stream_pos_frame", lambda: _STREAM_STATE.get("pos_frame", 0))
