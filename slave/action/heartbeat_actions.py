# action/heartbeat_actions.py
import ubinascii
import time
import gc
import machine
# 修正匯入路徑，使用你定義好的 Proto 類
from lib.proto import Proto 
from lib.schema_codec import SchemaCodec 
from lib.sys_bus import bus
# 全局變量記錄最後一次心跳時間
_LAST_HB_TICK = 0

# 獲取唯一的 MAC ID
def get_uid():
    return ubinascii.hexlify(machine.unique_id()).decode().upper()

def send_heartbeat(ctx):
    """手動發送心跳包"""
    app = ctx.get("app")
    if not app: return

    cmd_id = 0x1201
    cmd_def = app.store.get(cmd_id)
    if not cmd_def: return

    # 準備數據
    payload_data = {
        "slave_id": bus.slave_id,
        "uptime_ms": time.ticks_ms(),
        "mem_free": gc.mem_free(),
        "ws_connected": 1 if ctx.get("is_ws", False) else 0
    }

    try:
        # 使用類方法 SchemaCodec.encode 代替 encode_payload
        payload = SchemaCodec.encode(cmd_def, payload_data)
        # 使用類方法 Proto.pack 代替 pack_packet
        packet = Proto.pack(cmd_id, payload)
        
        send_func = ctx.get("send")
        if send_func:
            send_func(packet)
            
        print(f"[HB] Sent to PC as {payload_data['slave_id']}") # 加這行調試
    except Exception as e:
        print("[HB] Send Error: {}".format(e))

def on_heartbeat_ack(ctx, args):
    """處理來自 PC 的心跳確認 (0x1202)"""
    # args 已經由 dispatcher 解析完畢
    success = args.get("success", 0)
    if success:
        # 這裡可以更新本地的校時標誌
        pass

def register(app):
    """註冊心跳指令"""
    # 假設 0x1202 是 HEARTBEAT_ACK
    app.disp.on(0x1202, on_heartbeat_ack)
    print("[HB] Heartbeat actions registered")