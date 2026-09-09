# /action/sys_actions.py
import machine, time
import gc
import os
import json
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus
from lib.sys.ConfigManager import cfg_manager

# 定義常量 (直接使用數值)
CMD_DISCOVER = 0x1001
CMD_ANNOUNCE = 0x1002
CMD_SYS_INFO_GET = 0x1003
CMD_SYS_CTRL = 0x1004
CMD_SYS_TASK_QUERY = 0x1005
CMD_SYS_TASK_RSP = 0x1006
CMD_SYS_TASK_SET = 0x1007
CMD_WIFI_CTRL = 0x1008
CMD_WEB_CTRL = 0x1009

# --- 處理函數 (嚴格遵循 ctx, args 兩個參數) ---

def on_connect_request(bus_manager, url):
    """
    處理連線請求
    bus_manager: 傳入 ctrl_bus 實例
    url: 完整的 ws://... 網址
    """
    try:
        last_url = getattr(bus_manager, "_last_url", None)
        # 🔧 防抖動 + 健康檢查 (原設計只防抖動):
        #   master 每 10s 敲門, 已連到同一個 URL 就維持現況, 避免連線抖動。
        #   但「已連線」不代表還活著 —— half-open (對面靜默消失, 無 FIN/RST) 時
        #   connected 卡在 True, 若照舊直接 return True 會把敲門吞掉 → 永久離線。
        #   因此加一道健康門檻: 近 ws_stale_ms (預設 30s, 對齊 master「30s 無回應
        #   標離線」的健康檢查) 內有收到資料才算活著; 超過就視為死連線, 放行重連。
        if bus_manager.connected and last_url == url:
            idle = 0
            try:
                idle = bus_manager.idle_ms() if hasattr(bus_manager, "idle_ms") else 0
            except Exception:
                idle = 0
            try:
                stale = int(bus.shared.get("System", {}).get("ws_stale_ms", 30000) or 30000)
            except Exception:
                stale = 30000
            if idle <= stale:
                return True
            print("🔄 [Network] WS 逾時無流量 ({}ms > {}ms) → 斷線重連".format(idle, stale))
            bus_manager.disconnect()
            time.sleep_ms(50)

        # 1. 解析 URL
        parts = url.replace("ws://", "").split("/", 1)
        hp = parts[0].split(":")
        h = hp[0]
        p = int(hp[1]) if len(hp) > 1 else 80
        
        # 修正: 確保 path 正確解析
        if len(parts) > 1:
            path = "/" + parts[1]
        else:
            path = "/"

        # 2. 防止 DISCOVER 重複觸發造成反覆重連
        if bus_manager.connected:
            peer = getattr(bus_manager, "_peer", None)
            if peer == (h, p, path):
                # 已連到同一個 master → 不重連
                return True
            else:
                print(f"🔄 [Network] Active connection detected, resetting for: {h}:{p}{path}")
                bus_manager.disconnect()
                time.sleep_ms(50)

        # 3. 執行新連接
        # 注意: bus_manager.connect 內部的 settimeout(5) 會阻塞 Core0 少許時間
        # 但對於控制信道切換這是必要的。
        # 這裡呼叫 NetBus.connect(host, port, path)
        res = bus_manager.connect(h, p, path=path)

        if res:
             bus_manager._last_url = url
             bus_manager._peer = (h, p, path)
             bus_sys = bus.shared["System"]
             # 針對性無損更新 
             if bus_sys.get("master_IP") != h: 
                 bus_sys["master_IP"] = h 
                 print(f"💾 Updating Master IP: {h}") 
                 cfg_manager.save_from_bus(update_key="System.master_IP") 
                 
             if bus_sys.get("master_port") != p: 
                 bus_sys["master_port"] = p 
                 print(f"💾 Updating Master Port: {p}") 
                 cfg_manager.save_from_bus(update_key="System.master_port")

        return res
        
    except Exception as e:
        print(f"❌ [sys_actions] Connect Error: {e}")
        return False

def on_discover(ctx, args):
    """
    在 Discovery 觸發時被調用
    ctx 應包含 app 及 ctrl_bus
    """
    ws_base = args.get("ws_url", "")
    if not ws_base: return
    
    slave_id = "".join(f"{b:02X}" for b in machine.unique_id())
    full_url = f"{ws_base.rstrip('/')}/{slave_id}"
    
    # 呼叫上面的重連函數
    # 從 ctx 中獲取 ctrl_bus 實例
    ctrl_bus = ctx.get("ctrl_bus")
    if ctrl_bus:
        on_connect_request(ctrl_bus, full_url)

def on_sys_info_get(ctx, args):
    """處理系統信息查詢 (0x1003)"""
    gc.collect()
    stat = os.statvfs('/')
    print(f"ℹ️ [Sys] Info Request - RAM Free: {gc.mem_free()//1024}KB, FS Free: {(stat[0]*stat[3])//1024}KB")

def on_sys_ctrl(ctx, args):
    """處理系統控制指令 (0x1004): Wi-Fi 開關 + CPU 任務控制"""
    wifi_enable = args.get("wifi_enable", 0xFF)
    core_control = args.get("core_control", 0xFF)

    if wifi_enable != 0xFF:
        nm = bus.get_service("network_manager")
        if nm:
            if wifi_enable == 0:
                nm.disable_wifi()
            elif wifi_enable == 1:
                nm.enable_wifi()

    if core_control != 0xFF:
        tm = bus.get_service("task_manager")
        if tm:
            if core_control == 0:
                bus.shared["_saved_affinities"] = dict(tm.config)
                for name in list(tm.config.keys()):
                    tm.set_affinity(name, (0, 0))
                print("⏸️ [SysCtrl] 所有任務已暫停 (affinity → (0,0))")
            elif core_control == 1:
                saved = bus.shared.pop("_saved_affinities", None)
                if saved:
                    for name, affinity in saved.items():
                        tm.set_affinity(name, affinity)
                    print("▶️ [SysCtrl] 任務 affinity 已恢復")

def on_wifi_ctrl(ctx, args):
    wifi_enable = args.get("wifi_enable", 0xFF)
    if wifi_enable not in (0, 1):
        return
    nm = bus.get_service("network_manager")
    if not nm:
        return
    if wifi_enable == 0:
        nm.disable_wifi()
    else:
        nm.enable_wifi()

def on_web_ctrl(ctx, args):
    web_enable = args.get("web_enable", 0xFF)
    if web_enable not in (0, 1):
        return
    tm = bus.get_service("task_manager")
    if not tm:
        return
    if web_enable == 0:
        tm.set_affinity("web_ui", (0, 0))
    else:
        tm.set_affinity("web_ui", (1, 0))

def on_sys_task_query(ctx, args):
    """查詢所有任務的 affinity 與執行核心 (0x1005)"""
    tm = bus.get_service("task_manager")
    if not tm: return
    status = tm.get_status()
    app = ctx["app"]
    cmd_def = app.store.get(CMD_SYS_TASK_RSP)
    payload = SchemaCodec.encode(cmd_def, {"tasks_json": json.dumps(status)})
    if "send" in ctx:
        ctx["send"](Proto.pack(CMD_SYS_TASK_RSP, payload))

def on_sys_task_set(ctx, args):
    """設定單一任務的雙核 affinity (0x1007)"""
    task_name = args.get("task_name", "")
    c0 = args.get("affinity_c0", 0)
    c1 = args.get("affinity_c1", 0)
    if not task_name: return
    tm = bus.get_service("task_manager")
    if not tm: return
    tm.set_affinity(task_name, (c0, c1))

def on_time_sync(ctx, args):
    """0x100A TIME_SYNC: 回報本地收到主機時間戳的時刻 (延遲量測用)。

    PC 端以 0x100A 送出 master_time_ms, 本機收到後立刻回 0x100B
    TIME_SYNC_RSP {received_at_ms: 本地 ticks_ms()}; PC 用自己發送前後
    的時間差計算 RTT (單向延遲 ≈ RTT/2), 供中途加入時推算最準確的幀號。
    """
    app = ctx["app"]
    try:
        received_at = time.ticks_ms()
        cmd_def = app.store.get(0x100B)
        payload = SchemaCodec.encode(cmd_def, {"received_at_ms": received_at})
        if "send" in ctx:
            ctx["send"](Proto.pack(0x100B, payload))
    except Exception as e:
        print(f"❌ [TimeSync] Error: {e}")

def register(app):
    """註冊系統指令到分發器"""
    app.disp.on(CMD_DISCOVER, on_discover)
    app.disp.on(CMD_SYS_INFO_GET, on_sys_info_get)
    app.disp.on(CMD_SYS_CTRL, on_sys_ctrl)
    app.disp.on(CMD_WIFI_CTRL, on_wifi_ctrl)
    app.disp.on(CMD_WEB_CTRL, on_web_ctrl)
    app.disp.on(CMD_SYS_TASK_QUERY, on_sys_task_query)
    app.disp.on(CMD_SYS_TASK_SET, on_sys_task_set)
    app.disp.on(0x100A, on_time_sync)  # TIME_SYNC → 0x100B 延遲回報
    print("✅ [Action] Sys actions registered")
