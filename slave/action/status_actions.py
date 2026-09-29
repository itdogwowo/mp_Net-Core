# action/status_actions.py
# ═══════════════════════════════════════════════════════════════════════════
# 0x11xx — 狀態與設定的「取 / 設 / 存」三個出入口
#
#   0x1101 STATUS_GET { keys }                → 0x1102 STATUS_RSP { status_json }
#   0x1103 STATUS_SET { data_json, persist }  → 0x1104 STATUS_ACK { ok, message }
#
# ── 設計共識（動這支之前先讀）────────────────────────────────────────────
# 1. 「取」是取**具名的值** —— provider（任務註冊的即時數值）與 config
#      （bus.shared 的設定樹）都只是「可以具名的東西」，同一個指令、同一組 key。
#    ⚠️ 這是本檔的職責變更：舊設計把 0x1101 當「固定回全部狀態」，
#       `query_type` 宣告了 0/1 但 handler 從來沒讀過 → 只能「全都要」。
#       現在 keys 決定要什麼，**空 = 全部**（＝舊行為，完全相容）。
#
# 2. keys 是**逗號分隔的字串**，不是 bitmask —— bitmask 要查表，而且每加一項
#    就要改協議。用名字當選擇器：新增 provider / config 時零成本、不用新指令號。
#    （OTA 那邊試過 bitmask，最後拆成獨立指令，結果是「一次只能問一個 +
#      指令號一直長」—— 那兩個結果這裡都要避開。）
#
# 3. 「設」與「存」是兩件事，用 `persist` 一個位元組分開：
#      0    = 只寫快取（立即生效、不碰 flash）← 測試用
#      1    = 寫快取 + **逐 key** 落盤
#      0xFF = 不改值，只把 data_json 的 key 當清單落盤（純存檔）
#    沿用本專案既有慣例（SYS_CTRL / WIFI_CTRL 的 0xFF = 不變）。
#    ★ 為什麼不每幀都存：落盤是 flash 寫入，逐次存會把調參數變成大量抹寫；
#      而「先改快取測效果、確定了再存」本來就是正常流程
#      （改快取 vs 落盤，本來就是兩件事，不該綁在一起）。
#
# 4. 落盤一律**逐 key**（save_keys → save_from_bus(update_key=)）—— 那是
#    「在原始檔文字裡只換掉那一段」→ **排版保留**。不帶 update_key 的整檔存
#    會 _pretty_dump 重排全部，所以本檔刻意不走那條路（連「存全部」都不做）。
# ═══════════════════════════════════════════════════════════════════════════

import json
import gc
import os
import time
import machine, ubinascii
from lib.sys.sys_bus import bus
from lib.sys.proto import Proto, MAX_PAYLOAD as PROTO_MAX_PAYLOAD
from lib.sys.schema_codec import SchemaCodec

# 引用其他模組的狀態
from action import stream_actions

_MISSING = object()

# ESP-NOW 的單幀上限（NowBus.MAX_PAYLOAD = 250，其餘留給 HDR 9 + CRC 4）。
#   ⚠️ 只是**提醒用**：handler 不知道這一幀會走哪條線（WS 可到 8192）。
#      超過就只能走 WS/UART，走 ESP-NOW 會送不出去（send() 回 False）。
NOW_FRAME_LIMIT = 250


def get_runtime_info():
    """抓取整合性的實時運行數據"""
    # 獲取文件系統空間
    fs_stat = os.statvfs('/')
    fs_free = (fs_stat[0] * fs_stat[3]) // 1024
    uid = bus.slave_id

    return {
        "id": uid,
        "mem_free": gc.mem_free(),
        "uptime_ms": time.ticks_ms(),
        "fs_free_kb": fs_free,
        # 🚀 整合 Stream 模組的實時數據
        "fps": stream_actions._STREAM_STATE["fps"],
        "frame_count": stream_actions.get_frame_count(),
        "stream_mode": stream_actions.get_mode(),
        "is_streaming": stream_actions.is_streaming()
    }


# ══════════════════════════════════════════════════════════════════
#  key 解析：具名的值 = 內建 / provider / config 路徑
# ══════════════════════════════════════════════════════════════════
def _split_keys(raw):
    """'a, b ,c' → ['a','b','c']（去空白、去空項、保序、去重）。"""
    out = []
    for part in str(raw or "").split(","):
        k = part.strip()
        if k and k not in out:
            out.append(k)
    return out


def _cfg():
    """ConfigManager 單例。

    延後 import：太早 import 會讓 load_setup 在之後把剛寫進 bus.shared
    的值蓋回檔案裡的舊值（ConfigManager 在 module 層就有副作用）。
    """
    from lib.sys.ConfigManager import cfg_manager
    return cfg_manager


def _is_sensitive(path):
    """這個路徑是不是「不可以公開」的？

    兩條規則與 ConfigManager 存檔時的過濾完全一致（同一套判準，不重複定義）：
      `_` 開頭   —— 執行期全域（_vbtn / _hw_inputs / _core_buf…），不是設定
      `_pw` 結尾 —— 密碼。存檔時會從 config.json 剝離改存 btree，
                   但**開機時 sync_node 又把它還原進 bus.shared**，所以
                   記憶體裡是有值的。
    不擋的話，一次 `GET{"keys":"Network.wifi.ssid_pw"}` 就把 Wi-Fi 密碼送出去了。
    """
    for p in str(path).split("."):
        if p.startswith("_") or p.endswith("_pw"):
            return True
    return False


def _resolve_one(key):
    """解析單一 key → (狀態, 值)。

    狀態：  "ok"      找到了
            "denied"  找到了但不給（密碼 / 執行期全域）
            "unknown" 找不到

    ★ 順序固定：內建 → provider → config 路徑。
      provider 名通常不含點、config 路徑通常含點，但並非保證，所以要說死。
    """
    # ① 內建兩項（get_metrics() 也是特別處理這兩個，它們不是 provider）
    if key == "slave_id":
        return "ok", bus.slave_id
    if key == "mem_free":
        return "ok", gc.mem_free()

    # ② provider（任務註冊的即時數值）—— 只算這一項，不驚動其他
    v = bus.get_metric(key, _MISSING)
    if v is not _MISSING:
        return "ok", v

    # ③ config 路徑（bus.shared 的設定樹）
    exists = _cfg().get_by_path(key, _MISSING) is not _MISSING
    if _is_sensitive(key):
        # 先判敏感再看值，避免連「有沒有這個 key」都變成洩漏管道
        return ("denied", None) if exists else ("unknown", None)
    if not exists:
        return "unknown", None
    v = _cfg().get_by_path(key)
    if isinstance(v, (dict, list)):
        # 子樹要逐層剝離底下的密碼（例如 keys="Network" 會帶到 wifi.ssid_pw）
        v = _cfg().config_snapshot(root=key)
    return "ok", v


def _catalog():
    """可用的 key 目錄（`keys="?"`）—— 呼叫端不用硬記名字。

    ★ 目錄是**動態**的：provider 是註冊制，新掛上去的自動出現；
      config 直接掃 bus.shared 再剝離敏感項。所以加東西不用改協議。
    """
    return {
        "_catalog": {
            "builtin": ["slave_id", "mem_free"],
            "providers": bus.provider_names(),
            "config": _cfg().config_keys(),
        }
    }


def _build_status(keys_raw):
    """keys 字串 → 要回的 dict。

    空      → 全部 provider metrics ＋ mem_free（＝舊行為，完全相容）
    "?"     → 目錄
    其餘    → 逐一解析；找不到/被拒的集中列在 _unknown / _denied
    """
    if keys_raw is None:
        keys_raw = ""
    keys_raw = str(keys_raw).strip()
    if keys_raw == "?":
        return _catalog()
    if keys_raw == "":
        # 舊路徑原封不動：get_metrics() 一次算完全部（它本來就是為「全部」設計的）
        metrics = bus.get_metrics()
        metrics["mem_free"] = gc.mem_free()
        return metrics

    out = {}
    unknown = []
    denied = []
    for k in _split_keys(keys_raw):
        st, v = _resolve_one(k)
        if st == "ok":
            out[k] = v
        elif st == "denied":
            denied.append(k)
        else:
            unknown.append(k)
    if unknown:
        out["_unknown"] = unknown
    if denied:
        out["_denied"] = denied
    return out


def on_status_get(ctx, args):
    """0x1101: 依 keys 取具名的值（provider / config），回 0x1102。

    keys 空 = 全部（舊行為）；"?" = 目錄；其餘 = 逗號分隔清單。
    """
    app = ctx["app"]
    try:
        status_json = json.dumps(_build_status(args.get("keys")))
    except Exception as e:
        print("❌ [Status] 組裝失敗: {}".format(e))
        status_json = json.dumps({"_err": str(e)})

    # 太大就別送 —— 寧可回一個講清楚的錯誤，也不要送一個註定失敗的幀
    if len(status_json) > PROTO_MAX_PAYLOAD - 64:
        print("⚠️ [Status] 回應 {} B 超過協議上限".format(len(status_json)))
        status_json = json.dumps({
            "_err": "回應太大 ({} B)".format(len(status_json)),
            "_hint": "用 keys 縮小範圍；keys='?' 可列出可用名稱",
        })

    try:
        cmd_def = app.store.get(0x1102)
        payload = SchemaCodec.encode(cmd_def, {"status_json": status_json})
        if len(payload) + 13 > NOW_FRAME_LIMIT:
            # ESP-NOW 單幀 250B。走不通不影響 WS/UART，所以只提醒不擋。
            print("ℹ️ [Status] 回應 {} B > ESP-NOW 上限 {} B（WS/UART 仍可）".format(
                len(payload) + 13, NOW_FRAME_LIMIT))
        if "send" in ctx:
            ctx["send"](Proto.pack(0x1102, payload))
    except Exception as e:
        print(f"❌ [Status] Error: {e}")


# ══════════════════════════════════════════════════════════════════
#  0x1103 STATUS_SET —— 設（改快取）/ 存（逐 key 落盤）
# ══════════════════════════════════════════════════════════════════
PERSIST_CACHE = 0x00      # 只寫快取
PERSIST_SAVE = 0x01       # 寫快取 + 落盤
PERSIST_SAVE_ONLY = 0xFF  # 不改值，只落盤（key 清單取自 data_json）


def _reply_ack(ctx, app, ok, message):
    """統一回覆 0x1104 STATUS_ACK。"""
    try:
        cmd_def = app.store.get(0x1104)
        payload = SchemaCodec.encode(cmd_def, {"ok": 1 if ok else 0,
                                               "message": str(message or "")})
        if "send" in ctx:
            ctx["send"](Proto.pack(0x1104, payload))
    except Exception as e:
        print("❌ [Status] ACK 失敗: {}".format(e))


def on_status_set(ctx, args):
    """0x1103: 設定 config（by key）＋ 選擇性落盤。回 0x1104。

    data_json = JSON 物件，key 是點分路徑，可一次多個：
        {"Network.ESP_now.enable": 1, "System.cID": "0002"}

    persist:
        0    只寫快取（測試，不碰 flash）
        1    寫快取 → 再逐 key 落盤
        0xFF 不改值，只把 data_json 的 key 當清單落盤（純存檔）
    """
    app = ctx["app"]
    data_raw = args.get("data_json") or ""
    persist = int(args.get("persist", 0) or 0) & 0xFF

    # ── 解析 ────────────────────────────────────────────────
    if isinstance(data_raw, (bytes, bytearray, memoryview)):
        data_raw = bytes(data_raw).decode("utf-8")
    try:
        data = json.loads(data_raw) if str(data_raw).strip() else {}
    except Exception as e:
        return _reply_ack(ctx, app, False, "data_json 不是合法 JSON: {}".format(e))
    if not isinstance(data, dict):
        return _reply_ack(ctx, app, False,
                          "data_json 必須是物件 {{key: value}}，收到 {}".format(
                              type(data).__name__))

    note_unknown_persist = False
    if persist not in (PERSIST_CACHE, PERSIST_SAVE, PERSIST_SAVE_ONLY):
        persist = PERSIST_CACHE
        note_unknown_persist = True

    keys = [str(k) for k in data.keys()]
    cfg = _cfg()

    if persist == PERSIST_SAVE_ONLY and not keys:
        # 刻意**不做**「全量存檔」：那條路（save_from_bus 不帶 update_key）
        # 會 _pretty_dump 整檔重排縮排 —— 正是要避開的排版破壞。
        return _reply_ack(ctx, app, False,
                          "persist=0xFF 需指定要存的 key"
                          "（不做全量存檔 —— 那會整檔重排排版）")

    msgs = []
    ok = True

    # ── ① 寫快取（persist=0xFF 時只取 key 當清單，不寫值）─────
    changed = []
    if persist != PERSIST_SAVE_ONLY:
        for k in keys:
            if _is_sensitive(k):
                ok = False
                msgs.append("{}: 拒絕（密碼/執行期鍵）".format(k))
                continue
            good, why = cfg.set_by_path(k, data[k])
            if not good:
                ok = False
                msgs.append("{}: {}".format(k, why))
                continue
            changed.append(k)
            hit = cfg._REDLINE.get(k)
            if hit:
                msgs.append("⚠️ {} 已設定 —— {}".format(k, hit))
        msgs.append("寫入快取 {} 個 key".format(len(changed)))

    # ── ② 落盤（逐 key）─────────────────────────────────────
    if persist in (PERSIST_SAVE, PERSIST_SAVE_ONLY):
        save_list = keys if persist == PERSIST_SAVE_ONLY else changed
        n_ok = n_rw = n_skip = 0
        for k, st, why in cfg.save_keys(save_list):
            if st == cfg.SAVE_OK:
                n_ok += 1
            elif st == cfg.SAVE_REWRITTEN:
                n_rw += 1
                msgs.append("{}: {}".format(k, why))
            else:
                n_skip += 1
                ok = False
                msgs.append("{}: {}（未落盤）".format(k, why))
        msgs.append("存檔 {} 個 key{}".format(
            n_ok + n_rw,
            "，其中 {} 個走整檔重寫（排版被重排）".format(n_rw) if n_rw else ""))
        if n_skip:
            ok = False

    if note_unknown_persist:
        msgs.append("persist 值無效，已視為 0（只寫快取）")

    msg = "; ".join(msgs)
    print("🔹 [Status] SET persist={} ok={} :: {}".format(persist, ok, msg))
    return _reply_ack(ctx, app, ok, msg)


def register(app):
    """註冊狀態與設定指令"""
    app.disp.on(0x1101, on_status_get)
    app.disp.on(0x1103, on_status_set)

    # 多介面 IP 清單 provider (STATUS_GET 0x1101 的 metrics 帶 ips)
    def _ips_provider():
        nm = bus.get_service("network_manager")
        if nm is None:
            return {}
        try:
            return nm.get_ips()
        except Exception:
            return {}

    bus.register_provider("ips", _ips_provider)

    # 🔧 目前渲染幀間隔 provider：直接回報儲存的原始數字（System.frame_interval_ms），
    # 不做任何換算；換算由 PC 端自己做。
    def _frame_interval_ms_provider():
        try:
            return bus.shared.get("System", {}).get("frame_interval_ms", 0)
        except Exception:
            return 0

    bus.register_provider("frame_interval_ms", _frame_interval_ms_provider)

    # 🔧 掃描忙碌旗標: 供 PC 端在「掃描 → 下載 manifest → 比對」前輪詢。
    #    覆蓋兩種掃描:
    #      - root flash 背景重掃 (bus.shared["fs_scan_requested"], core1 FsScanTask)
    #      - SD 主動掃描 (bus.shared["fs_scan_sd_busy"], 0x200B target=1)
    def _fs_scan_busy_provider():
        if bus.shared.get("fs_scan_requested", False):
            return 1
        if bus.shared.get("fs_scan_sd_busy", False):
            return 1
        return 0

    bus.register_provider("fs_scan_busy", _fs_scan_busy_provider)
    print("✅ [Action] Status actions integrated (GET 0x1101 / SET 0x1103)")
