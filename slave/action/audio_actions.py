# action/audio_actions.py
# audio (0x32xx) 命令層：只把 Master 指令寫成「離散命令」進 bus.shared，
# 由 DjTask（tasks/dj_task.py）消費後執行讀檔 / 播放 / 狀態管理。
# 本模組不持有播放狀態（檔案 handle、playlist、供給鏈都在 DjTask 內）。
#
# 例外：0x320A AUDIO_LIST_QUERY 是純查詢 —— 直接讀 bus 上的 "audio_playlist"
# 快取 service（DjTask.on_start 註冊）組 entries 回覆，不繞任務迴圈。

import struct
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus

# 每筆 entry = name(str_u16len) + duration_ms(u32) + compat(u8)；
# 8K payload 上限內留 header/CRC/欄位餘裕，entries 塞到 ~7.5K 就截斷。
_ENTRY_MAX = 7500


def on_audio_set(ctx, args):
    """0x3201: 準備單檔（file_name = playlist 的 name）→ audio_cmd_set"""
    bus.shared["audio_cmd_set"] = {
        "file_name": args.get("file_name", ""),
        "play_mode": int(args.get("play_mode", 0) or 0),
        "volume": int(args.get("volume", 0) or 0),
    }


def on_audio_play(ctx, args):
    """0x3202: 起播（start_ms>0 = 中途加入）→ audio_cmd_play"""
    bus.shared["audio_cmd_play"] = {
        "start_ms": int(args.get("start_ms", 0) or 0),
    }


def on_audio_stop(ctx, args):
    """0x3203: 停止 → audio_cmd_stop"""
    bus.shared["audio_cmd_stop"] = True


def on_audio_pause(ctx, args):
    """0x3204: 暫停/恢復 → audio_cmd_pause"""
    bus.shared["audio_cmd_pause"] = bool(args.get("pause", 0))


def on_audio_seek(ctx, args):
    """0x3205: 跳轉 → audio_cmd_seek"""
    bus.shared["audio_cmd_seek"] = {
        "target_ms": int(args.get("target_ms", 0) or 0),
    }


def on_audio_volume(ctx, args):
    """0x3206: 主音量（0~100）→ audio_cmd_volume（dj_task 混音時折進每軌增益）"""
    bus.shared["audio_cmd_volume"] = max(0, min(100, int(args.get("volume", 0) or 0)))


def on_audio_program_set(ctx, args):
    """0x3209: 獨立多軌節目 → audio_cmd_program。tracks = JSON bytes:
    {"tracks": [{"file","loop","volume","start_ms"}], "limit": 0-100}"""
    bus.shared["audio_cmd_program"] = {"json": args.get("tracks", b"")}


# ── 播放列表管理（M3）──────────────────────────────

def on_audio_list_query(ctx, args):
    """0x320A: 命令通道查播放列表（異系統相容用；自家 Master 走檔案通道下載
    playlist.json）。直接讀 "audio_playlist" 快取 service 回 0x320B。

    entries 每筆 = name(str_u16len) + duration_ms(u32) + compat(u8)；
    塞滿 8K 前截斷：total = 全部筆數、count = 實際帶回筆數，total>count 表示
    截斷 → Master 改用檔案通道拉全量。
    """
    pl = bus.get_service("audio_playlist")
    names = sorted(pl.files.keys()) if pl is not None else []
    entries = bytearray()
    count = 0
    for n in names:
        e = pl.files[n]
        nb = n.encode("utf-8")
        piece = struct.pack("<H", len(nb)) + nb + struct.pack("<IB",
                                                              int(e.get("duration_ms", 0) or 0),
                                                              int(e.get("compat", 0) or 0))
        if len(entries) + len(piece) > _ENTRY_MAX:
            break
        entries += piece
        count += 1
    app = ctx.get("app")
    if app is None or "send" not in ctx:
        return
    try:
        cmd_def = app.store.get(0x320B)
        if not cmd_def:
            return
        payload = SchemaCodec.encode(cmd_def, {
            "total": min(255, len(names)),
            "count": count,
            "entries": bytes(entries),
        })
        ctx["send"](Proto.pack(0x320B, payload))
    except Exception as e:
        print("[Audio] LIST_RSP failed: {}".format(e))


def on_audio_list_rescan(ctx, args):
    """0x320C: 重掃 SD 重建 playlist.json → audio_cmd_rescan（dj_task 消費，
    播放中則延後到回 IDLE；完成回 0x320E AUDIO_LIST_READY）。"""
    bus.shared["audio_cmd_rescan"] = True


def on_audio_list_remove(ctx, args):
    """0x320D: 從索引移除；delete_file=1 連 SD 檔案一起刪 → audio_cmd_remove"""
    bus.shared["audio_cmd_remove"] = {
        "name": args.get("name", ""),
        "delete_file": int(args.get("delete_file", 0) or 0),
    }


def register(app):
    app.disp.on(0x3201, on_audio_set)     # SET
    app.disp.on(0x3202, on_audio_play)    # PLAY
    app.disp.on(0x3203, on_audio_stop)    # STOP
    app.disp.on(0x3204, on_audio_pause)   # PAUSE
    app.disp.on(0x3205, on_audio_seek)    # SEEK
    app.disp.on(0x3206, on_audio_volume)  # VOLUME
    app.disp.on(0x3209, on_audio_program_set)  # PROGRAM_SET（多軌節目）
    app.disp.on(0x320A, on_audio_list_query)    # LIST_QUERY（直接回 0x320B）
    app.disp.on(0x320C, on_audio_list_rescan)   # RESCAN
    app.disp.on(0x320D, on_audio_list_remove)   # REMOVE
    print("✅ [Action] Audio actions registered")
