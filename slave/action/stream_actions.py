# action/stream_actions.py
from lib.sys_bus import bus
from lib.proto import Proto
from lib.schema_codec import SchemaCodec
from lib.sys_bus import bus
def on_stream_state_set(ctx, args):
    """0x3009: 準備分塊與文件模式"""
    bus.shared.update({
        "active_file": bus.get_service("data_Phat")+ '/' + args["file_name"],
        "cur_block": args["block_id"],
        "play_mode": args["play_mode"],
        "is_seeking": True,
        "is_ready": False,
        "is_streaming": False # 先設為 False 以免 Ready 途中亂跳
    })
    print(f"📡 [Stream] Set: {args['file_name']}")

def on_stream_play(ctx, args):
    """0x300A: 開始播放 (支持中途加入)"""
    start_frame = args.get("start_frame", 0)
    
    # 如果指定了起始幀，通知 Supply Chain 進行跳轉
    if start_frame > 0:
        bus.shared.update({
            "seek_frame": start_frame,
            "is_seeking": True # 觸發重新加載/跳轉
        })
        # 刷新 Buffer Hub 以清除舊數據
        hub = bus.get_service("pixel_stream")
        if hub: hub.flush()
        print(f"▶️ PLAY from frame {start_frame}")
    else:
        print(f"▶️ PLAY from start")
        
    bus.shared.update({"is_streaming": True})

def handle_supply_chain(hub, s, ctx):
    """由 Core 0 定時調用，負責加載與 READY 回報"""
    if bus.shared.get("is_seeking"):
        try:
            hub.flush()
            if s.get("f_local"): s["f_local"].close()
            s["f_local"] = open(bus.shared["active_file"], "rb")
            
            # 處理跳轉
            seek_frame = bus.shared.get("seek_frame", 0)
            if seek_frame > 0:
                st = bus.get_service("st_LED")
                if st:
                    offset = seek_frame * st.total_bytes
                    s["f_local"].seek(offset)
                    print(f"⏩ Seek to frame {seek_frame} (offset {offset})")
                bus.shared["seek_frame"] = 0 # Reset
            
            # 預填第一幀
            view = hub.get_write_view()
            if view is not None:
                if s["f_local"].readinto(view) > 0:
                    hub.commit()
            
            bus.shared["is_seeking"] = False
            bus.shared["is_ready"] = True
            
            # --- 主動回報 READY 給 PC ---
            cmd_def = ctx["app"].store.get(0x3008)
            payload = SchemaCodec.encode(cmd_def, {"block_id": bus.shared["cur_block"]})
            ctx["send"](Proto.pack(0x3008, payload))
            print(f"✅ READY: {bus.shared['active_file']}")
        except Exception as e:
            print(f"❌ Load Error: {e}")
            bus.shared["is_seeking"] = False

    # 播放規律預讀
    if bus.shared.get("is_streaming") and not bus.shared.get("is_paused"):
        # 利用 Hub 自帶 dirty 位檢查供給
        if not hub.dirty and s.get("f_local"):
            view = hub.get_write_view()
            if view is not None:
                if s["f_local"].readinto(view) == 0:
                    if bus.shared.get("play_mode") == 1: s["f_local"].seek(0)
                    else: bus.shared["is_streaming"] = False
                else:
                    hub.commit()

def register(app):
    # 播放控制
    app.disp.on(0x3009, on_stream_state_set) # SET
    app.disp.on(0x300A, on_stream_play) # PLAY
    app.disp.on(0x3005, lambda c,a: bus.shared.update({"is_paused": bool(a["pause"])})) # PAUSE
    app.disp.on(0x3002, lambda c,a: bus.shared.update({"is_streaming": False, "is_ready": False})) # STOP
    # 0x3003 Direct Mode
    app.disp.on(0x3003, lambda c,a: bus.get_service("pixel_stream").write_from(a["pixel_data"]))