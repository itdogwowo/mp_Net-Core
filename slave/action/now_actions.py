import time
import gc

try:
    import ubinascii
except ImportError:                     # CPython（離線測試）：同一個 API 叫 binascii
    import binascii as ubinascii

from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus

BCAST_MAC = b'\xff\xff\xff\xff\xff\xff'


def _mac_str_to_bytes(mac_str):
    """MAC 字串/bytes → 6 bytes。**解析失敗會印警告**（不再靜默回廣播）。

    ★ 2026-10：舊版失敗時直接回 `BCAST_MAC` 且不吭聲 —— 打錯一個字元就從
      「單播給某台」變成「廣播給所有人」，而且看起來一切正常。
      現在失敗會印出原文，讓它變成看得見的錯誤（仍然回廣播，維持可運作）。
    """
    if isinstance(mac_str, bytes) and len(mac_str) == 6:
        return mac_str
    try:
        s = mac_str.replace(":", "").replace("-", "").replace(" ", "")
        return ubinascii.unhexlify(s)
    except Exception as e:
        print("⚠ [NOW] MAC 解析失敗 {!r} ({}) → 退回廣播".format(mac_str, e))
        return BCAST_MAC


def on_now_init(ctx, args):
    """0x1301 NOW_INIT —— ESP-NOW 生命週期的**唯一入口**（查詢 / 開 / 關）。

    action（u8，不給就是型別的預設值 0）:
        0 → 查詢（不改變任何狀態）
        1 → 確保開
        2 → 關
        3 → **設頻道**（讀 `channel` 欄位，1..13）—— 2026-10 新增
        其他 → 未知（只印 log，不動裝置）

    ★ `channel`（u8）是 2026-10 追加的**第二個欄位**，向後相容：
      舊客戶端只送 1 byte（`action`）→ SchemaCodec 在 payload 用完時就停
      （`if pos >= plen and tc != 5: break`）→ `args` 裡根本沒有 `channel`
      → 這裡擋掉並提示，不會誤動作。

    ★ 為什麼「設頻道」需要一個 action：在此之前換頻道等於「改 config ＋ 重啟」
      ——`init(channel=N)` 在 STA 已開時會**靜默忽略**（見 `NowBus.set_channel`）。
      真機兩板實測：`sta.config(channel=N)` 執行期當場生效，兩端都換完就通了
      → **不必重啟**。語意上「頻道＝區分不同網路」，不做跨頻道兼容、不需掃描。

    ★ 規則只有一句：**不給參數就是 0，0 就是查詢。**
      沒有「缺席特別代表開」這種例外規則 —— 缺席就是欄位型別的原生預設值。

    ★ 後果（已知且接受）：本指令原本是**空 payload**（只有開），所以舊客戶端
      送空 payload 從「開」變成「查詢」（不再啟動射頻）。這是刻意的取捨：
        ① 語意一致：不給參數 = 0 = 查詢，送錯不會改變裝置狀態
        ② 不會再有「送空 payload 意外啟動射頻」這種事
      要開就**明確**送 `1`。

    ★ 為什麼「關」需要一個指令：ESP-NOW 獨佔 2.4GHz 射頻，要改用 Wi-Fi
      連線時關掉它是實際需求，不是潔癖。

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
    app = ctx.get("app")
    if not app:
        return

    # 不給參數 → 欄位型別的原生預設值 0（查詢）。沒有例外規則。
    action = int(args.get("action") or 0)

    esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {}) or {}
    now = bus.get_service("NowBus")

    if action == 2:
        if now is None or not now.connected:
            print("[NOW] already off")
        else:
            sources = bus.get_service("bus_sources")
            if sources:
                sources.remove(now)      # ① 先摘掉來源
            now.deinit()                 # ② 才關射頻
            print("[NOW] ESP-NOW off")

    elif action == 1:
        if not esp_cfg.get('enable', 0):
            # config 說不要 → 不偷偷開。要開就改 config，語意單一。
            print("[NOW] ESP_now disabled in config → 不開 (改 Network.ESP_now.enable)")
        else:
            _now, what = _now_on(esp_cfg)
            if what == "failed":
                print("[NOW] init failed")

    elif action == 3:
        # ★ 2026-10：執行期換頻道（不必重啟）。見 NowBus.set_channel 的實測記錄。
        if now is None or not now.connected:
            print("[NOW] 要先開 ESP-NOW 才能換頻道（action=1）")
        else:
            ch = args.get("channel")
            if not ch:
                print("[NOW] action=3 需要 channel（1..13）"
                      " —— 舊客戶端只送 action，請補上第二個位元組")
            else:
                now.set_channel(ch)

    elif action != 0:
        print("[NOW] 未知 action={} (0=查詢 1=開 2=關 3=設頻道)".format(action))

    # ── 回報（查詢與動作走同一條路，呼叫端只需讀 enabled）───────────
    now = bus.get_service("NowBus")
    enabled = 1 if (now is not None and now.connected) else 0
    ch = now._channel() if enabled else "-"
    print("[NOW] init action={} → enabled={} ch={} cfg_enable={}".format(
        action, enabled, ch, esp_cfg.get('enable', 0)))


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
      - 服務不存在      → 建一個新的
      - 服務存在但斷線  → **就地重開**（`init()`），不要建第二個
        舊 `on_now_init` 對這種情況只印 "already initialized" 就返回，
        所以「關掉之後再也開不回來」—— 這是 0x1301 加 action 時補的洞。

    ★ config 的 `ESP_now.enable` 是**授權**，不是狀態：
      它說「這台裝置允許用 ESP-NOW」。實際開關是執行期動作（0x1301）。
      所以關掉不會改 config —— 重開機還是回到 config 說的那個狀態。

    ★ 單一實作：`0x1301`、`0x1012 NET_START{iface_type:3}` 都走這裡，
      避免「同一件事多套 init 邏輯」（其中幾套沒有 service 重用檢查，
      會撞 ESP_ERR_ESPNOW_EXIST）。
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


def register(app):
    """0x1301 是 ESP-NOW 的唯一入口（查詢/開/關）。

    0x1304 NOW_CTRL 已於 2026-09 廢除 —— 它與 0x1301 是同一件事的兩條指令，
    能力整併進 0x1301 的 `action`（0=查詢 1=開 2=關；不給參數 = 0 = 查詢）。
    """
    app.disp.on(0x1301, on_now_init)
    app.disp.on(0x1302, on_now_send_hb)
    app.disp.on(0x1303, on_now_stats)
    print("[NOW] ESP-NOW actions registered (0x1301 init/ctrl)")
