# action/pixel_actions.py
# 本地燈效 (Local Mode) 遠端控制 + 配對支援
#
# 指令 (pixel 群 0x31xx, 對應 slave/schema/pixel.json):
#   請求 (Master→Slave, 本模組註冊 handler):
#     0x3101 MODE_LIST_QUERY    — 查詢本地燈效模式 id 清單
#     0x3105 MODE_SET           — 播放指定本地模式 (一個一個播, 供配對識別)
#     0x3106 MODE_STOP          — 停止本地模式 (熄燈)
#     0x3107 MODE_DETAIL_QUERY  — 查詢單一模式名稱等細節
#   回應 (Slave→Master, 只送出):
#     0x3102 MODE_LIST_RSP / 0x3108 MODE_DETAIL_RSP
#
# 播放端 = PixelTask（pixel_task.py）等多個消費方：本模組只把指令經 gmode 寫進
# 共用狀態 bus.shared（mode_id / mode_seq / mode_start_at），消費方跟狀態執行；
# 不需 PC 串流 data.bin。mode id 是全系統共用參數（MP3/audio 等同樣消費）。

import time
import struct
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus

# ── 內部模式識別碼：協議的 (mode_type, mode_id) 分開讀取，進系統後合併成
#    單一 16-bit id = (mode_type << 8) | mode_id —— modes/*.json 的 id 即此值。
def _combine(mode_type, mode_id):
    return ((int(mode_type) & 0xFF) << 8) | (int(mode_id) & 0xFF)


def _send(ctx, rsp_cmd, fields):
    app = ctx["app"]
    try:
        cmd_def = app.store.get(rsp_cmd)
        payload = SchemaCodec.encode(cmd_def, fields)
        if "send" in ctx:
            ctx["send"](Proto.pack(rsp_cmd, payload))
    except Exception as e:
        print("[Pixel] reply {} failed: {}".format(hex(rsp_cmd), e))


def on_mode_list_query(ctx, args):
    """0x3101: 回報模式清單（gmode 合併池：pixel + audio）。

    mode_type: 0=全部、1=LED、2=SERVO、3=AUDIO（16-bit id 高 byte 過濾）。
    entries = 依 id 排序的 u16 串（每筆 2 bytes, little-endian, 對齊
    SchemaCodec 的 <H 習慣）= 內部 16-bit 模式識別碼 (mode_type<<8 | mode_id)。
    """
    mode_type = int(args.get("mode_type", 0) or 0)
    gmode = bus.get_service("gmode")
    if gmode is not None:
        pool = gmode.mode_pool()
        ids = gmode.filter_ids(pool, mode_type)
    else:
        # 無 gmode（舊行為）：只有 pixel 池、不過濾
        pool = bus.shared.get("pixel_maps", {})
        ids = sorted(int(i) for i in pool.keys())
    entries = b"".join(struct.pack("<H", i) for i in ids)
    _send(ctx, 0x3102, {
        "mode_type": mode_type,
        "count": min(255, len(ids)),
        "entries": entries,
    })
    print("[Pixel] MODE_LIST type={} count={}".format(mode_type, len(ids)))


def on_mode_set(ctx, args):
    """0x3105: 播放指定模式（gmode 貫通：燈效 + 綁定音效同步起播）。

    先強制退出串流（stream_active=False / is_streaming=False），避免 data.bin
    供給鏈與本地燈效搶 pixel_stream hub。
    (mode_type, mode_id) 分開讀取 → 合併成單一 16-bit id 進 gmode。
    start_delay_ms：pixel 與 audio 都用同一個延遲起播（同步）。
    """
    mode_type = args.get("mode_type", 0)
    mode_id = args.get("mode_id", 0)
    start_delay_ms = args.get("start_delay_ms", 0) or 0
    # 🔧 亮度: 有輸入用輸入, 沒輸入/0 預設 255 (全亮)。套用到渲染核心 (APA102 亮度頭)。
    brightness = args.get("brightness") or 255
    st = bus.get_service("st_pixel")
    if st is not None and hasattr(st, "set_brightness"):
        st.set_brightness(brightness)
    # 停用串流供給鏈 (stream_active) 與渲染旗標, 避免與本地 show 衝突
    bus.shared.update({
        "stream_active": False,
        "is_streaming": False,
        "is_paused": False,
        "is_ready": False,
    })
    gmode = bus.get_service("gmode")
    if gmode is not None:
        gmode.set_mode(_combine(mode_type, mode_id), start_delay_ms=start_delay_ms)
    else:
        # gmode 由 app.py 建立(單一事實來源),理論上一定存在;缺失代表啟動異常。
        # 不自行寫 bus.shared["mode_id"] —— 那會漏掉 audio 扇出(燈效動但音效不同步)。
        print("[Pixel] gmode 缺失 — MODE_SET 未執行")
    print("[Pixel] MODE_SET type={} id={} bri={} delay={}ms".format(
        mode_type, mode_id, brightness, start_delay_ms))


def on_mode_stop(ctx, args):
    """0x3106: 停止模式（gmode 貫通：燈滅 + 音停）。"""
    action = int(args.get("action", 0) or 0)
    bus.shared.update({
        "stream_active": False,
        "is_streaming": False,
        "is_paused": False,
        "is_ready": False,
    })
    gmode = bus.get_service("gmode")
    if gmode is not None:
        gmode.stop_mode(action)
    else:
        # gmode 一定存在(app.py 建立);缺失代表啟動異常,不自行寫 mode_id。
        print("[Pixel] gmode 缺失 — MODE_STOP 未執行")
    print("[Pixel] MODE_STOP action={}".format(action))
    print("[Pixel] MODE_STOP action={}".format(action))


def on_mode_detail_query(ctx, args):
    """0x3107: 回報單一模式細節 (名稱; total_ms 目前無資料=0)。"""
    mode_type = args.get("mode_type", 0)
    mode_id = args.get("mode_id", 0)
    gmode = bus.get_service("gmode")
    if gmode is not None:
        m = gmode.resolve(_combine(mode_type, mode_id))
    else:
        modes = bus.shared.get("pixel_maps", {})
        m = modes.get(_combine(mode_type, mode_id))
    name = m.get("name", "") if m else ""
    _send(ctx, 0x3108, {
        "mode_type": mode_type,
        "mode_id": mode_id,
        "total_ms": 0,
        "name": name,
    })


def register(app):
    app.disp.on(0x3101, on_mode_list_query)
    app.disp.on(0x3105, on_mode_set)
    app.disp.on(0x3106, on_mode_stop)
    app.disp.on(0x3107, on_mode_detail_query)
    print("[Pixel] Local-mode actions registered")
