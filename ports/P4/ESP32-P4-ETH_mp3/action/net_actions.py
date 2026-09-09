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
        try:
            from lib.sys.now_bus import NowBus
            esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {})
            ch = esp_cfg.get('channel', 1)
            now = bus.get_service("NowBus")
            if now is None:
                now = NowBus(label="NOW-Bus")
            if now.init(channel=ch):
                bus.register_service("NowBus", now)
                ok = 1
                iface = "espnow"
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
    """0x1016: 顯式設定回應定址 master_cid。"""
    mc = args.get("master_cid", 0xFFFF) & 0xFFFF
    bus.master_cid = mc


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
    app.disp.on(CMD_IDENTIFY_REQ, on_identify_req)
    app.disp.on(CMD_REBOOT, on_reboot)
    app.disp.on(CMD_WREPL_CTRL, on_wrepl_ctrl)
    app.disp.on(CMD_NET_START, on_net_start)
    app.disp.on(CMD_GET_IP, on_get_ip)
    app.disp.on(CMD_SET_MASTER, on_set_master)
    app.disp.on(CMD_WEBUI_CTRL, on_webui_ctrl)
    print("✅ [Action] Net actions registered")
