# action/net_actions.py
# 遠端更新鏈路 第一階段 — 發現/保險/網絡/IP/master 指令集
#
# 指令 (sys 群 0x10xx):
#   請求 (Master→Slave, 本模組註冊 handler):
#     0x100D IDENTIFY_REQ   — 點名; 帶 reply_cid 指定**這一封** 0x100E 回信寄哪
#     0x100F REBOOT         — 延遲後重啟 (保險)
#     0x1010 WREPL_CTRL     — 查詢/確保開/關 WebREPL (保險)
#     0x1012 NET_START      — 依 iface_type 啟動網絡 (lan/wifi/ap/espnow)
#     0x1014 GET_IP         — 取得多介面 IP 清單
#     0x1016 SET_MASTER     — 設定 master_cid（★ 唯一會改「我的 master 是誰」的指令）
#   公告 (對端→控制端, 單向, 不回覆):
#     0x1002 SLAVE_ANNOUNCE — 對端開機/週期性自我介紹 (slave_id+pixel_count+hw)
#                             ★ ESP-NOW 上「唯一」的主動發現途徑 (MAC 無法枚舉)
#   回應 (Slave→Master, 只送出):
#     0x100E IDENTIFY_RSP / 0x1011 WREPL_RSP / 0x1013 NET_START_RSP / 0x1015 IP_RSP
#
# 回應 addr 預設 = bus.master_cid (未設=0xFFFF 廣播)；**0x100E 例外** ——
#   它用 IDENTIFY_REQ 帶來的 reply_cid（單次指定，不影響全域方向）。

import json
import machine
import time
from lib.sys.sys_bus import bus
from lib.sys.proto import Proto, ADDR_BROADCAST
from lib.sys.schema_codec import SchemaCodec
# 來源 MAC 的正規化（bytes → 可存 JSON 的 hex 字串）用 peers 表那一套，不重寫一份
from lib.sys.peer_registry import mac_hex as _mac_hex
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


def _reply(ctx, rsp_cmd, fields, addr=None, delay_ms=0):
    """送出回應幀。addr=None → 回 bus.master_cid (未設=0xFFFF 廣播)。

    `addr` 是「**這一封**回信寄哪」的單次指定 —— 它只是參數，**不改全域方向**。
    `0x100D IDENTIFY_REQ` 的 `reply_cid` 走的就是這條（見 on_identify_req）。

    `delay_ms > 0` → **延後發射**（不等待）：交給解碼鏈的發射出口排程，到期才送。
      handler 只知道「延遲幾毫秒」，不知道 MAC、也不知道哪條管子
      （見 `tasks/bus_decode.py` 的 `_TxOut`）。

    ★ `addr` 這個參數是 2026-10 補上的。在那之前 `_reply()` 只能從
      `bus.master_cid` 拿位址，於是 `on_identify_req` 只能**先把 reply_cid
      塞進 master_cid** 才寄得出 0x100E —— 那是「少了一個參數」的變通，
      副作用是任何一次點名都變成認主人（無聲搶奪）。現在位址從參數來，
      全域狀態就只由 0x1016 改。
    """
    app = ctx["app"]
    cmd_def = app.store.get(rsp_cmd)
    if not cmd_def:
        return
    if addr is None:
        addr = bus.master_cid
    try:
        payload = SchemaCodec.encode(cmd_def, fields)
        frame = Proto.pack(rsp_cmd, payload, addr=addr)
        # ★ 只有真的要延後才多傳第二個參數 —— 其餘自建 ctx["send"] 的呼叫端
        #   （web_ui / stream_task）維持原本的一參數形狀，零影響。
        #   解碼鏈的出口（tasks/bus_decode.py `_TxOut`）兩者都吃。
        if delay_ms > 0:
            ctx["send"](frame, delay_ms)
        else:
            ctx["send"](frame)
    except Exception as e:
        print("❌ [Net] reply {} failed: {}".format(hex(rsp_cmd), e))


_rng = None
_rng_kind = None


def _rng16():
    """16-bit 隨機（抖動用）。

    ★ 優先 `urandom`（**真機已驗這塊板有**：`urandom`/`random` 都有 `getrandbits`）。
      為什麼不用 `time.ticks_us()` 省一個 import：**那個做法是錯的** ——
      `ticks_us()` 的差異在「同一台連續呼叫」時只有幾微秒，而且在「同時上電的
      兩台裝置」之間也會很接近 → 抖動會退化成「大家一起延後差不多的時間」，
      等於沒有抖動。（2026-10 由 `test/protocol/test_node_pairing.py` §4 抓到。）
    ★ 退化路徑（理論上用不到）仍保留：`ticks_us` 混入，只保證「不會全部相同」。
    """
    global _rng, _rng_kind
    if _rng is None and _rng_kind is None:
        for _name in ("urandom", "random"):
            try:
                _rng = __import__(_name)
                _rng_kind = _name
                break
            except ImportError:
                continue
        if _rng is None:
            _rng_kind = "ticks_us"
            print("[Net] ⚠ 無 urandom/random → 抖動退化為 ticks_us "
                  "（同時上電的裝置相關性較高）")
    if _rng is not None:
        try:
            return _rng.getrandbits(16)
        except Exception:
            pass
    try:
        return time.ticks_us() & 0xFFFF
    except Exception:
        return 0


def _spread(max_ms):
    """回 [0, max_ms] 的隨機毫秒（抖動用）。max_ms <= 0 → 0。

    ★ 兩個必要性質，缺一抖動就沒用：
        ① 同一台、不同次掃描 → 值要不同（否則每次都用同一個時槽，還是會撞）
        ② 不同台、同一次掃描 → 值要不同（這才是抖動的目的）
      只有真隨機同時滿足兩者 —— 細節見 `_rng16()` 的說明。
    """
    if max_ms <= 0:
        return 0
    return _rng16() % (max_ms + 1)


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
    連同收幀當下的射頻 MAC（ctx["_src_mac"]）記進 PeerRegistry。
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
    """0x100D IDENTIFY_REQ —— **點名**。帶 reply_cid 指定回信寄哪, 回應 cid+slave_id+IP。

    ★ 語意只有「你是誰」—— **不改 `bus.master_cid`**。
      「我以後聽誰的」（方向）只有 `0x1016 SET_MASTER` 一個寫入者。
      點名是**問題**，方向是**狀態**，兩件事不該共用一個變數。

    為什麼以前會改（以及為什麼那是 bug）：`_reply()` 原本只能從
      `bus.master_cid` 拿目的位址，要回這封 `0x100E` 給掃描者，就只能先把
      `reply_cid` 塞進 `master_cid`。這個變通造成四個後果：
        · 面板 B 按一下「掃描」→ 射程內**每台**節點都改認 B（A 不知道）
          = 無聲搶奪；B 從頭到尾沒打算搶，它只是按了掃描
        · 狀態自相矛盾：`master_cid` 有值但 `role` 仍是 None
        · 只改記憶體、不發布 → `0x1101{keys:"node.master_cid"}` 查到**舊值**
        · 不落盤 → 重開機還原，這輪開機期間的歸屬與 btree 不一致
      現在 `reply_cid` 只作用在**這一封 0x100E**（`_reply(..., addr=...)`）。

    回覆的**射頻**去向仍是「剛剛講話的那個人」（NowBus 把來源跟著幀走），
      與此無關；`reply_cid` 決定的是 **NC4 標頭**裡的位址 —— 它的作用是讓
      **其他**節點把這封回信丟掉（`app.handle_stream` 的 ADDR 過濾）。
      所以它必須留著，只是不該變成全域狀態。

    誰依賴它：面板 `_do_scan()` 廣播 `0x100D{reply_cid=自己cid, timeout_ms=500}`，
      期望用 `0x100E` 回來的 cid 認識節點（回覆自動進 PeerRegistry）。

    ★ `timeout_ms` = **抖動視窗**（2026-10 新增）：
        `0`（或缺席，舊客戶端）= 不抖動、立即回（＝舊行為）
        `> 0`                  = 隨機延遲 [0, timeout_ms] 後才回
      為什麼要抖動：廣播點名會讓射程內**所有** Slave 在同一瞬間回話 ——
        在共用媒介上（ESP-NOW 空中／RS485 匯流排）那是**硬碰撞**，會整批掉。
        各自隨機挑一個時間點，就把「同時」攤成「序列」。
      ★ 為什麼只有這裡抖動：全專案「廣播出去、N 台回話」的只有 `0x100D`
        （其餘廣播 `0x1401/0x1501/0x1502/0x3105/0x3106` 都是單向的）。
        所以判準不是「addr 是不是 0xFFFF」，而是「**只有掃描**」——
        靠「寫在這個 handler 裡」來保證，不需要執行期判斷。
      ★ 非阻塞：這裡只**排程**，不等待（`time.sleep_ms` 會卡住 core0 的解碼鏈
        與 UI，而且停擺期間 poll() 不跑 → 幀只能堆在驅動層而掉落）。
    """
    reply_cid = args.get("reply_cid", 0xFFFF) & 0xFFFF
    spread = args.get("timeout_ms", 0) & 0xFFFF
    delay = _spread(spread)
    if spread:
        # 掃描不是熱路徑，這一行是**真機驗證用**的：看得出抽到幾毫秒、
        # 以及「handler 立刻返回、到點才送」是否成立（見 todo/05 §12-1）。
        print("[Net] 0x100D 點名 → 抖動 {}ms 後回覆 (視窗 {})".format(delay, spread))
    _reply(ctx, CMD_IDENTIFY_RSP, {
        "cid": bus.cid,
        "slave_id": bus.slave_id,
        "ip": _ips_json(),
    }, addr=reply_cid, delay_ms=delay)


def on_slave_announce(ctx, args):
    """0x1002 SLAVE_ANNOUNCE —— **收**方（控制端）：對端開機／週期性公告。

    ★ 這條指令補的是 ESP-NOW 上「無法掃描」的那個洞：
      射頻位址是 MAC，MAC **不是可枚舉的數值空間**，所以 0x100D 那種
      「逐 address 掃」在 ESP-NOW 上無從發起 —— 主動發現只能靠對端送上門。
      對端廣播 0x1002 → 本板在**收到的那一刻**才知道它的 MAC（ctx["_src_mac"]）
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
    所以本板收到它 → 記住對方**兩個位址**，且**本板是被控方**（role = "slave"）。
    反之，本板主動發它 → 是告訴對方「你的 master 是我」。

    ★ 這裡是**唯一會改「方向」的地方**（也是唯一為它寫 flash 的地方）：
      - 本指令是明確動作（人按了綁定，或對方明確告知）→ 值得寫 flash
      - `0x100D IDENTIFY_REQ` 的 `reply_cid` 只是**那一封**的回信位址，
        **不再改 master_cid**（見 on_identify_req）—— 方向不會被點名偷改。
        修掉之前，一次廣播掃描就會把射程內所有節點的方向改掉；現在不會。
      - 開機由 `ConfigManager.load_node()` 從 btree 還原、解除由 `clear_node()`。

    ★ **`master_cid == 0xFFFF` = 解除配對**（不是「設成廣播」）。
      `0xFFFF` 在本專案本來就是「未設定」的哨兵（`bus.master_cid` 初值、
      `node_state()` 的 `bound` 推導），所以這不是新規則 —— 是讓既有語意完整。
      解除要**清乾淨**：`master_cid` 回哨兵、`master_mac` 清空、`role` 清空，
      否則本板會停在「我是某人的 slave」但其實沒有 master 的矛盾狀態。
      解除**不看 `_claimed`**（它是還原，不是認領），而且要**重新開放認領**
      → `pair_claimed = False`，讓新的 master 不必等重開機就能接手。

    ★ **兩個位址一起記**（`master_cid` 協議層 + `master_mac` 射頻層）——
      這是「配對是雙邊對稱的記錄」的另一半：
        Master 端記每一台的 `(cid, mac)`；Slave 端記 Master 的 `(master_cid, master_mac)`。
      `master_mac` 取自**收幀當下的射頻來源**（`ctx["_src_mac"]`），
      與 `0x100E` 帶回 cid 的方式完全對稱。
      沒有它：解析 `addr == master_cid` 只能回退到「剛剛講話的人」，
      而**延後發射（抖動）時那早就換人了** → 會回錯對象。

    ★ **本次開機已被認領 → 忽略新的 master**（`todo/05` D8 / §7.3）：
      master_cid 是**半永久**的，光靠它會變成「永遠不能被換」。加了
      `pair_claimed`（純記憶體，開機歸零）就得到「**重啟一下，再讓新的 Master 執行**」
      —— 每次開機先到先得，且節點與 master 失聯後能自癒。
      （同一個 master 重複確認不算換手，照常受理。）
      ⚠️ 「拒絕」為什麼不回報：被拒絕時 `bus.master_cid` 還是**舊** master，
        而 `_reply()` 一律送到 `bus.master_cid` —— 回覆會跑到舊 master 那裡，
        新的請求者永遠收不到。且 0x1016 沒有對應的 RSP 指令。
        請求者要用 `0x1101 STATUS_GET{keys:"node.master_cid"}` 查對方認的是誰
        （見 `todo/04_remote_pairing.md` D1）—— 拒絕因此是**看得見**的。

    ★ **值沒變就不寫 flash**：掃描會重複確認同一台，而 `save_node()` 是
      3 個 btree key + `flush()`（`kv_set` 沒有變更偵測）→ 沒這個守衛就是
      「每輪每台一次抹寫」。用**前後快照比對**（不重算條件），以後改上面的
      賦值邏輯，這個守衛自動跟著對。

    持久化失敗不影響本次設定（記憶體已生效），只印訊息。
    """
    mc = args.get("master_cid", 0xFFFF) & 0xFFFF
    psrc = ctx.get("_src_mac") if isinstance(ctx, dict) else None
    pmac = _mac_hex(psrc)

    # ── 解除：清乾淨、重新開放認領（不看 _claimed）────────────────
    if mc == ADDR_BROADCAST:
        prev = (bus.master_cid, bus.master_mac, getattr(bus, "role", None))
        bus.master_cid = ADDR_BROADCAST
        bus.master_mac = None
        bus.role = None
        bus.pair_claimed = False
        if (bus.master_cid, bus.master_mac, getattr(bus, "role", None)) == prev:
            return                       # 本來就沒配對 → 不碰 flash
        print("[Net] SET_MASTER 解除配對（master_cid/master_mac/role 清空，重新開放認領）")
        _save_node()
        return

    # ── 認領：本次開機已被別人認領過 → 忽略（要換就重啟）──────────
    if getattr(bus, "pair_claimed", False):
        cur = int(getattr(bus, "master_cid", ADDR_BROADCAST)) & 0xFFFF
        if cur != ADDR_BROADCAST and cur != mc:
            print("[Net] SET_MASTER 忽略 0x{:04X}：本次開機已被 0x{:04X} 認領"
                  "（要換 master 請重啟本板）".format(mc, cur))
            return

    prev = (bus.master_cid, bus.master_mac, getattr(bus, "role", None))
    bus.master_cid = mc
    bus.master_mac = pmac                # 射頻層來源（ESP-NOW = MAC；共用線 = None）
    bus.pair_claimed = True              # 純記憶體，不落盤
    if not getattr(bus, "role", None):
        # 有人明確告知方向 → 本板是被控方
        bus.role = "slave"
    if (bus.master_cid, bus.master_mac, getattr(bus, "role", None)) == prev:
        return                           # ★ 半永久狀態沒變 → 不碰 flash
    # ★ 這一行同時是「**閃寫計數器**」：上面的早退讓它「一次認主只印一次」，
    #   重複確認（掃描、對方重送、同一台再按一次綁定）都不會再印。
    #   真機要驗「值沒變就不寫 flash」時，數這行就等於數抹寫次數
    #   —— 不必去讀檔案 mtime，而進 REPL 讀檔本身就會觸發存檔（會污染量測）。
    #   （見 `todo/05_node_pairing.md` §12-7。）
    print("[Net] SET_MASTER 認主 0x{:04X} (mac={}, role={})".format(
        bus.master_cid, bus.master_mac, getattr(bus, "role", None)))
    _save_node()


def _save_node():
    """存節點狀態（失敗只印訊息 —— 記憶體已生效）。"""
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
