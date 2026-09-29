import time
import gc
import ubinascii
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus

BCAST_MAC = b'\xff\xff\xff\xff\xff\xff'


def _mac_str_to_bytes(mac_str):
    if isinstance(mac_str, bytes) and len(mac_str) == 6:
        return mac_str
    try:
        s = mac_str.replace(":", "").replace("-", "").replace(" ", "")
        return ubinascii.unhexlify(s)
    except Exception:
        return BCAST_MAC


def on_now_init(ctx, args):
    app = ctx.get("app")
    if not app:
        return

    esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {})
    enable = esp_cfg.get('enable', 0)
    if not enable:
        print("[NOW] ESP_now disabled in config, skip init")
        return

    now = bus.get_service("NowBus")
    if now is None:
        try:
            from lib.sys.now_bus import NowBus
            wifi_cfg = bus.shared.get('Network', {}).get('wifi', {})
            wifi_enable = wifi_cfg.get('enable', 0)
            channel = esp_cfg.get('channel', 1)

            now = NowBus()
            if wifi_enable:
                ok = now.init()
            else:
                ok = now.init(channel=channel)

            if ok:
                bus.register_service("NowBus", now)
                sources = bus.get_service("bus_sources")
                if sources:
                    sources.add(now)
                print("[NOW] ESP-NOW ready, ch={}".format(now._channel()))
            else:
                print("[NOW] init failed")
        except Exception as e:
            print("[NOW] init err: {}".format(e))
    else:
        print("[NOW] already initialized")


def on_now_send_hb(ctx, args):
    app = ctx.get("app")
    if not app:
        return

    now = bus.get_service("NowBus")
    if now is None:
        print("[NOW] not initialized, run NOW_INIT first")
        return

    target_mac = args.get("target_mac", "FF:FF:FF:FF:FF:FF")
    count = max(1, int(args.get("count", 1)))

    hb_def = app.store.get(0x1201)
    if not hb_def:
        print("[NOW] HEARTBEAT schema not found")
        return

    payload_data = {
        "slave_id": bus.slave_id,
        "uptime_ms": time.ticks_ms(),
        "mem_free": gc.mem_free(),
        "ws_connected": 0,
    }

    try:
        payload = SchemaCodec.encode(hb_def, payload_data)
        packet = Proto.pack(0x1201, payload)
    except Exception as e:
        print("[NOW] pack err: {}".format(e))
        return

    mac = _mac_str_to_bytes(target_mac)
    ok = 0
    fail = 0

    for _ in range(count):
        if now.send(mac, packet):
            ok += 1
        else:
            fail += 1

    print("[NOW] send_hb -> {} count={} ok={} fail={}".format(target_mac, count, ok, fail))


def on_now_stats(ctx, args):
    now = bus.get_service("NowBus")
    if now is None:
        print("[NOW] not initialized")
        return

    s = now.stats
    print("[NOW] stats rx={} tx={} ok={} fail={} drop={}".format(
        s["rx"], s["tx"], s["tx_ok"], s["tx_fail"], s["rx_drop"]))


def _now_on(esp_cfg):
    """確保 ESP-NOW 開著；回 (NowBus, 動作字串)。

    ★ 「已經關掉」與「從來沒建過」是兩種不同的狀態，分開處理：
      - 服務不存在      → 建一個新的（同 on_now_init）
      - 服務存在但斷線  → **就地重開**（`init()`），不要建第二個
        原本 `on_now_init` 對這種情況只印 "already initialized" 就返回，
        所以「關掉之後再也開不回來」—— 這是 0x1304 補的洞。

    ★ config 的 `ESP_now.enable` 是**授權**，不是狀態：
      它說「這台裝置允許用 ESP-NOW」。實際開關是執行期動作（0x1304）。
      所以關掉不會改 config —— 重開機還是回到 config 說的那個狀態。
    """
    now = bus.get_service("NowBus")
    if now is not None and now.connected:
        return now, "already"

    channel = esp_cfg.get("channel", 1)
    wifi_enable = bus.shared.get('Network', {}).get('wifi', {}).get('enable', 0)
    if now is None:
        from lib.sys.now_bus import NowBus
        now = NowBus()
        what = "created"
    else:
        what = "reopened"

    ok = now.init() if wifi_enable else now.init(channel=channel)
    if not ok:
        return None, "failed"

    bus.register_service("NowBus", now)   # 已存在時 register_service 回 False，無害
    sources = bus.get_service("bus_sources")
    if sources:
        sources.add(now)
    print("[NOW] ESP-NOW {}, ch={}".format(what, now._channel()))
    return now, what


def on_now_ctrl(ctx, args):
    """0x1304 NOW_CTRL —— 查詢(0) / 確保開(1) / **關**(2)。

    與 `0x1010 WREPL_CTRL`、`0x1017 WEBUI_CTRL` 同型（`action: u8`），
    維持「晶片開關指令長得一樣」的系列一致性。

    ★ 為什麼「關」需要一個指令：`0x1301 NOW_INIT` 只有開、沒有關，
      所以面板的 ESP-NOW 開關原本是**單向**的（關不掉）。ESP-NOW 獨佔
      2.4GHz 射頻，要改用 Wi-Fi 連線時關掉它是實際需求，不是潔癖。

    ★ 關的順序（**不能顛倒**）：
        ① 先從 bus_sources 移除 → ② 才 deinit
      反過來的話，bus_decode 的下一輪 `_drain()` 會拿到一個已經 dead 的
      bus（`rx_hub` 還在、`_esp` 已是 None），在射頻層炸開。
      移除後 bus_decode 就完全看不到它了。

    ★ 關掉之後的狀態刻意是「服務還在、`connected=False`」：
      - `NowTask.loop()` 靠 `connected` 停止 poll ✔
      - `NowBus.send()` 靠 `connected` 直接回 False（不會炸）✔
      - UI 的 `_tx()` 拿到 False → 顯示「送不出去」而不是靜默 ✔
      這比「把服務整個拔掉」好：所有既有呼叫端本來就是容錯寫法，
      拔掉服務反而會讓 `get_service("NowBus")` 變 None 的分支到處跑。
    """
    action = args.get("action", 0)
    esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {}) or {}
    now = bus.get_service("NowBus")

    if action == 2:
        if now is None or not now.connected:
            print("[NOW] already off")
        else:
            sources = bus.get_service("bus_sources")
            if sources:
                sources.remove(now)          # ① 先摘掉來源
            now.deinit()                     # ② 才關射頻
            print("[NOW] ESP-NOW off")

    elif action == 1:
        if not esp_cfg.get('enable', 0):
            # config 說不要 → 不偷偷開。要開就改 config，語意單一。
            print("[NOW] ESP_now disabled in config → 不開 (改 Network.ESP_now.enable)")
        else:
            _now, what = _now_on(esp_cfg)
            if what == "failed":
                print("[NOW] init failed")

    elif action != 0:
        print("[NOW] 未知 action={} (0=查詢 1=開 2=關)".format(action))

    # ── 回報（查詢與動作走同一條路，呼叫端只需讀 enabled）───────────
    now = bus.get_service("NowBus")
    enabled = 1 if (now is not None and now.connected) else 0
    ch = now._channel() if enabled else "-"
    print("[NOW] ctrl action={} → enabled={} ch={} cfg_enable={}".format(
        action, enabled, ch, esp_cfg.get('enable', 0)))


def register(app):
    app.disp.on(0x1301, on_now_init)
    app.disp.on(0x1302, on_now_send_hb)
    app.disp.on(0x1303, on_now_stats)
    app.disp.on(0x1304, on_now_ctrl)
    print("[NOW] ESP-NOW actions registered")

