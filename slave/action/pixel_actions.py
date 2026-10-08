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
    # 亮度 0-255：255 = 不改（沿用全域 u8 約定，見 waiting_to_trash_actions._NO_CHANGE）；
    # 0 是合法值（= 最暗/關，APA102 亮度頭 >>3 後為 0）。
    # ⚠️ 不可寫 `args.get("brightness") or 255` —— 那會把「要求全暗」的 0 換成「最亮」。
    _bri = args.get("brightness")
    if _bri is None:
        _bri = 255
    brightness = max(0, min(255, int(_bri)))
    if brightness != 255:
        st_bri = bus.get_service("st_pixel")
        if st_bri is not None and hasattr(st_bri, "set_brightness"):
            st_bri.set_brightness(brightness)
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


def _is_local_provider():
    """本板是不是「執行端」（自己的模式池來自 /pixel/modes/*.json）？

    ★ 2026-10 改判準（**原本看 `pixel_maps` 有沒有，現在不能那樣看**）：
      `ConfigManager.load_modes()` 現在也會把 **DB 的清單灌進同一個快取**
      （為了讓不跑 PixelTask 的裝置答得出 `0x3101`），
      所以「快取有沒有東西」已經**不再等於**「本板是不是執行端」
      —— 拿它當判準會讓遙控器誤判成執行端，反而把收到的遠端清單丟掉。

      改用 `mode.source`：那正是「**這份清單哪裡來的**」的權威記錄
      （`set_local_modes` → "local"；`set_remote_list` → "remote"）。

    ★ 執行端不該被遠端清單覆蓋 —— 它的模式池來自 /pixel/modes/*.json（事實來源），
      若被遠端寫入蓋掉，UI 會顯示錯的清單，直到下次開機 PixelTask 再覆蓋。
    """
    try:
        from lib.sys.ConfigManager import cfg_manager
        return cfg_manager.kv_get("mode.source", None) == "local"
    except Exception:
        # 取不到 ConfigManager 時退回舊判準（至少不會讓功能整個失效）
        return bus.shared.get("pixel_maps") is not None


def on_mode_list_rsp(ctx, args):
    """0x3102 MODE_LIST_RSP —— **收**方（控制端）：對方回報的模式清單。

    entries = u16 串（每筆 2 bytes LE）= 內部 16-bit 模式識別碼。
    寫入模式表 + source=remote（見 doc/03_notes/19_remote_control_plan.md §6）。
    ⚠️ 不在這裡自動逐一發 0x3107 —— 「逐一取得細節」由呼叫端（UI / 同步流程）
       決定，避免在收幀 handler 裡一次爆出 N 個發射。
    """
    if _is_local_provider():
        print("[Pixel] 本板有本地模式池 → 忽略遠端清單")
        return
    raw = bytes(args.get("entries", b"") or b"")     # bytes_rest 解出來是 memoryview
    ids = []
    for i in range(0, len(raw) - 1, 2):
        ids.append(raw[i] | (raw[i + 1] << 8))
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.set_remote_list(ids)
    except Exception as e:
        print("[Pixel] 模式清單寫入失敗: {}".format(e))
    print("[Pixel] MODE_LIST_RSP type={} count={} → 已寫入模式表(remote)".format(
        args.get("mode_type", 0), len(ids)))


def on_mode_detail_rsp(ctx, args):
    """0x3108 MODE_DETAIL_RSP —— **收**方（控制端）：逐一取得的模式細節。

    只寫那一筆（@mode.detail.<id>）—— btree 逐 key 的價值所在。
    """
    if _is_local_provider():
        return
    mid = _combine(args.get("mode_type", 0), args.get("mode_id", 0))
    name = args.get("name", "") or ""
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.set_remote_detail(mid, name)
    except Exception as e:
        print("[Pixel] 模式細節寫入失敗: {}".format(e))
    print("[Pixel] MODE_DETAIL_RSP 0x{:04X} name={!r} → 已寫入模式表".format(mid, name))


def register(app):
    app.disp.on(0x3101, on_mode_list_query)
    app.disp.on(0x3102, on_mode_list_rsp)      # 控制端收清單
    app.disp.on(0x3105, on_mode_set)
    app.disp.on(0x3106, on_mode_stop)
    app.disp.on(0x3107, on_mode_detail_query)
    app.disp.on(0x3108, on_mode_detail_rsp)    # 控制端收細節
    print("[Pixel] Local-mode actions registered")
