# action/net_actions.py
# 遠端更新鏈路 第一階段 — 發現/保險/網絡/IP/master 指令集
#
# 指令 (sys 群 0x10xx):
#   請求 (Master→Slave, 本模組註冊 handler):
#     0x100D IDENTIFY_REQ   — 逐 address 素描; 帶 reply_addr 告知 master_cid
#     0x100F REBOOT         — 延遲後重啟 (保險)
#     0x1010 WREPL_CTRL     — 查詢/確保開/關 WebREPL (保險)
#     0x1012 NET_START      — 依 iface_type 啟動網絡 (lan/wifi/ap/espnow)
#     0x1014 GET_IP         — 取得多介面 IP 清單
#     0x1016 SET_MASTER     — 顯式設定回應定址 master_cid
#   公告 (對端→控制端, 單向, 不回覆):
#     0x1002 SLAVE_ANNOUNCE — 對端開機/週期性自我介紹 (slave_id+pixel_count+hw)
#                             ★ ESP-NOW 上「唯一」的主動發現途徑 (MAC 無法枚舉)
#   回應 (Slave→Master, 只送出):
#     0x100E IDENTIFY_RSP / 0x1011 WREPL_RSP / 0x1013 NET_START_RSP / 0x1015 IP_RSP
#
# 回應 addr 一律 = bus.master_cid (未設=0xFFFF 廣播)。

import json
import machine
import time
from lib.sys.sys_bus import bus
from lib.sys.proto import Proto, ADDR_BROADCAST
from lib.sys.schema_codec import SchemaCodec
from lib.sys import webrepl_ctl

CMD_SLAVE_ANNOUNCE = 0x1002
CMD_IDENTIFY_REQ = 0x100D
CMD_IDENTIFY_RSP = 0x100E
CMD_REBOOT = 0x100F
CMD_WREPL_CTRL = 0x1010
CMD_WREPL_RSP = 0x1011
CMD_NET_START = 0x1012
CMD_NET_START_RSP = 0x1013
CMD_GET_IP = 0x1014
CMD_IP_RSP = 0x1015
CMD_SET_MASTER = 0x1016
CMD_WEBUI_CTRL = 0x1017
CMD_WEBUI_RSP = 0x1018


def _reply(ctx, rsp_cmd, fields):
    """送出回應幀, addr 回 bus.master_cid (未設=0xFFFF 廣播)。"""
    app = ctx["app"]
    cmd_def = app.store.get(rsp_cmd)
    if not cmd_def:
        return
    try:
        payload = SchemaCodec.encode(cmd_def, fields)
        ctx["send"](Proto.pack(rsp_cmd, payload, addr=bus.master_cid))
    except Exception as e:
        print("❌ [Net] reply {} failed: {}".format(hex(rsp_cmd), e))


def _get_nm():
    """取得 NetworkManager 服務 (不存在回 None)。"""
    return bus.get_service("network_manager")


def _ips_json():
    nm = _get_nm()
    if nm is None:
        return "{}"
    try:
        return json.dumps(nm.get_ips())
    except Exception:
        return "{}"


def on_identify_rsp(ctx, args):
    """0x100E IDENTIFY_RSP —— **收**方（Master 側 / 面板側）。

    這是「自己素描到什麼 slave」的登記點：把回覆的 cid + slave_id + ip
    連同收幀當下的射頻 MAC（ctx["_peer_mac"]）記進 PeerRegistry。
    自己送出的 0x100D 會走到 on_identify_req；收到的回覆走這裡，兩者對稱。
    """
    reg = bus.get_service("peers")
    if reg is None:
        return
    try:
        reg.learn_from_identify_rsp(ctx, args)
    except Exception as e:
        print("[Net] peers learn failed: {}".format(e))


def on_identify_req(ctx, args):
    """0x100D: 逐 address 素描。帶 reply_addr 告知 master_cid, 回應 cid+slave_id+IP。"""
    reply_addr = args.get("reply_addr", 0xFFFF) & 0xFFFF
    if reply_addr != ADDR_BROADCAST:
        bus.master_cid = reply_addr  # 這一輪開機保持住 master address
    _reply(ctx, CMD_IDENTIFY_RSP, {
        "cid": bus.cid,
        "slave_id": bus.slave_id,
        "ip": _ips_json(),
    })


def on_slave_announce(ctx, args):
    """0x1002 SLAVE_ANNOUNCE —— **收**方（控制端）：對端開機／週期性公告。

    ★ 這條指令補的是 ESP-NOW 上「無法掃描」的那個洞：
      射頻位址是 MAC，MAC **不是可枚舉的數值空間**，所以 0x100D 那種
      「逐 address 掃」在 ESP-NOW 上無從發起 —— 主動發現只能靠對端送上門。
      對端廣播 0x1002 → 本板在**收到的那一刻**才知道它的 MAC（ctx["_peer_mac"]）
      → PeerRegistry 登記 → 之後才能定向（unicast）查詢它。

    與被動學習（bus_decode._drain 的 learn_from_frame）的差別：
      - 被動學習：任何幀都會登記「來源 MAC」，但不帶身份
      - 這裡：公告**自帶 slave_id / pixel_count / hw_version** → 一次補齊
      兩者寫的是同一筆記錄（ESP32 上 slave_id == MAC hex → 同一個 key），
      所以是互補而非重複。

    不做的事（刻意）：
      - 不自動「綁定」——綁定是使用者的明確動作（UI 的綁定鈕 → 0x1016）。
        自動把每個聽到的節點都設成目標，等於誰都能接管這台面板。
      - 不回應 —— 公告是**單向**的，回它會讓 N 個對端同時回話（風暴）。
        要對方回話請用 0x100D（那才是「請求」）。
    """
    reg = bus.get_service("peers")
    if reg is None:
        return
    try:
        reg.learn_from_announce(ctx, args)
    except Exception as e:
        print("[Net] announce learn failed: {}".format(e))
        return
    print("[Net] 📣 SLAVE_ANNOUNCE {} pixel_count={} hw={}".format(
        args.get("slave_id") or "-", args.get("pixel_count", 0),
        args.get("hw_version", "") or "-"))


def on_reboot(ctx, args):
    """0x100F: 延遲後重啟 (保險)。"""
    delay_ms = args.get("delay_ms", 0) or 0
    if delay_ms > 0:
        time.sleep_ms(delay_ms)
    print("🔁 [Net] REBOOT requested, resetting...")
    machine.reset()


def on_wrepl_ctrl(ctx, args):
    """0x1010: 查詢(0)/確保開(1)/關(2) WebREPL。"""
    action = args.get("action", 0)
    if action == 1:
        webrepl_ctl.ensure()
    elif action == 2:
        webrepl_ctl.stop()
    enabled, info = webrepl_ctl.status()
    _reply(ctx, CMD_WREPL_RSP, {"enabled": enabled, "info": info})


def on_net_start(ctx, args):
    """0x1012: 依 iface_type 啟動網絡 (0=lan 1=wifi 2=ap 3=espnow)。"""
    iface_type = args.get("iface_type", 0)
    nm = _get_nm()
    ok = 0
    iface = ""
    ip = ""

    if iface_type == 0:  # lan
        if nm is not None:
            try:
                if nm.enable_lan():
                    ok = 1
                    iface = "lan"
            except Exception as e:
                print("❌ [Net] LAN start failed: {}".format(e))
    elif iface_type == 1:  # wifi STA
        if nm is not None:
            try:
                nm.enable_wifi()
                ok = 1
                iface = "wifi"
            except Exception as e:
                print("❌ [Net] WiFi start failed: {}".format(e))
    elif iface_type == 2:  # AP
        if nm is not None:
            try:
                if nm.enable_ap():
                    ok = 1
                    iface = "ap"
            except Exception as e:
                print("❌ [Net] AP start failed: {}".format(e))
    elif iface_type == 3:  # ESP-NOW
        # ★ 走 0x1301 的**同一個實作**（`now_actions._now_on`），不另寫一套 init。
        #   原本這裡自己建 NowBus + init(channel=ch)，缺了兩件事：
        #     ① 沒有「服務存在但已斷線」的處理 → 對已 deinit 的實例再 init
        #     ② 沒有 network.py 的 ESP_ERR_ESPNOW_EXIST 防護
        #   同一件事三套實作（0x1301 / 0x1012 / 0x1304），其中兩套是壞的
        #   —— 2026-09 整併成一套。
        #   延後 import：避免 network/action 模組層的循環依賴。
        try:
            from action.now_actions import _now_on
            esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {}) or {}
            now, what = _now_on(esp_cfg)
            if now is not None:
                ok = 1
                iface = "espnow"
            else:
                print("❌ [Net] ESP-NOW init {}".format(what))
        except Exception as e:
            print("❌ [Net] ESP-NOW start failed: {}".format(e))

    if ok and iface != "espnow":
        try:
            ips = nm.get_ips()
            ip = ips.get(iface, "") if isinstance(ips, dict) else ""
        except Exception:
            ip = ""
    _reply(ctx, CMD_NET_START_RSP, {"ok": ok, "iface": iface, "ip": ip})


def on_get_ip(ctx, args):
    """0x1014: 回多介面 IP 清單。"""
    _reply(ctx, CMD_IP_RSP, {"ip": _ips_json()})


def on_set_master(ctx, args):
    """0x1016: 顯式設定回應定址 master_cid（＝**方向確認**）。

    payload 是**對方的 cid**，語意是「你的 master 是我」。
    所以本板收到它 → 記住對方位址，且**本板是被控方**（role = "slave"）。
    反之，本板主動發它 → 是告訴對方「你的 master 是我」。

    ★ 這裡是**唯一會持久化方向的地方**：
      - 本指令是明確動作（人按了綁定，或對方明確告知）→ 值得寫 flash
      - `0x100D IDENTIFY_REQ` 的 `reply_addr` 是隱含版本（每次敲門都來）
        → 只寫記憶體，不落盤（見 on_identify_req）
    持久化失敗不影響本次設定（記憶體已生效），只印訊息。
    """
    mc = args.get("master_cid", 0xFFFF) & 0xFFFF
    bus.master_cid = mc
    # 有人明確告知方向 → 本板是被控方；MAC 之後由 peers 表 by_cid() 查
    if mc != ADDR_BROADCAST and not getattr(bus, "role", None):
        bus.role = "slave"
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.save_node()
    except Exception as e:
        print("[Net] SET_MASTER 持久化失敗（記憶體仍生效）: {}".format(e))


def on_webui_ctrl(ctx, args):
    """0x1017: 查詢(0)/開(1)/關(2) Web UI。開關靠 task_manager.set_affinity("web_ui", ...),
    與既有 WEB_CTRL(0x1009) 同一機制; 統一納入 net_actions 管理 (帶回應)。"""
    action = args.get("action", 0)
    tm = bus.get_service("task_manager")
    if tm is None:
        _reply(ctx, CMD_WEBUI_RSP, {"enabled": 0, "info": "no task_manager (worker_engine mode)"})
        return
    if action == 1:
        tm.set_affinity("web_ui", (1, 0))
    elif action == 2:
        tm.set_affinity("web_ui", (0, 0))
    affinity = tm.config.get("web_ui", (0, 0))
    enabled = 1 if affinity[0] == 1 else 0
    _reply(ctx, CMD_WEBUI_RSP, {"enabled": enabled, "info": "web_ui affinity={}".format(affinity)})


def register(app):
    app.disp.on(CMD_SLAVE_ANNOUNCE, on_slave_announce)
    app.disp.on(CMD_IDENTIFY_REQ, on_identify_req)
    app.disp.on(CMD_IDENTIFY_RSP, on_identify_rsp)
    app.disp.on(CMD_REBOOT, on_reboot)
    app.disp.on(CMD_WREPL_CTRL, on_wrepl_ctrl)
    app.disp.on(CMD_NET_START, on_net_start)
    app.disp.on(CMD_GET_IP, on_get_ip)
    app.disp.on(CMD_SET_MASTER, on_set_master)
    app.disp.on(CMD_WEBUI_CTRL, on_webui_ctrl)
    print("✅ [Action] Net actions registered")
