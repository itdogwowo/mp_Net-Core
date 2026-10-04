import time
import struct
from lib.sys.sys_bus import bus
from lib.sys.buffer_hub import AtomicStreamHub
from lib.sys.proto import Proto, ADDR_BROADCAST

import espnow
import network

try:
    import ubinascii
except ImportError:                     # CPython（離線測試）：同一個 API 叫 binascii
    import binascii as ubinascii

MAX_PAYLOAD = 250
BCAST_MAC = b'\xff\xff\xff\xff\xff\xff'

# ESP-IDF 的 ESP-NOW peer 表硬體上限。
#   ⚠️ 廣播位址固定佔 1 格（**未加密**），所以一般情況「使用者可選」上限是
#      MAX_PEERS - 1。加密 peer 另有自己的上限、與總數分開算
#      → 加密時可選 MAX_PEERS_ENCRYPTED 台（廣播那格不佔它）。
MAX_PEERS = 20
MAX_PEERS_ENCRYPTED = 6

# ESP-IDF 的「這個 peer 已經在表裡」錯誤碼（見 `_is_exist_error`）。
ESPNOW_ERR_EXIST = -12395


def _is_exist_error(e):
    """判斷例外是不是 `ESP_ERR_ESPNOW_EXIST`（**已存在，不是失敗**）。

    為什麼不能只比對一種形式：真機上是 `OSError(-12395, 'ESP_ERR_ESPNOW_EXIST')`，
    但離線（CPython）沒有這個錯誤碼，測試得自己造例外出來 —— 所以
    「錯誤碼」與「訊息字串」兩種都認。
    """
    args = getattr(e, "args", ()) or ()
    if args and args[0] == ESPNOW_ERR_EXIST:
        return True
    return "ESPNOW_EXIST" in str(e).upper()


def mac_bytes(mac):
    """`hex 字串` / `bytes` → **6-byte bytes**。認不出來回 `None`。

    ══════════════════════════════════════════════════════════════════
    ★★ 為什麼需要這個（2026-10 **真機實測**抓到，`temp/probe_espnow_macfmt.py`）
    ══════════════════════════════════════════════════════════════════
    MicroPython 的 `espnow` **只接受 6-byte bytes**，字串一概不收：

        e.add_peer(b"\\xff" * 6)          → OK
        e.add_peer("ffffffffffff")       → ERR invalid buffer length
        e.add_peer("ff:ff:ff:ff:ff:ff")  → ERR invalid buffer length
        e.send("ffffffffffff", b"x")     → ERR ValueError invalid buffer length

    而本專案**存 MAC 的地方都是 hex 字串**（`PeerRegistry.mac`、
    `bus.master_mac`）→ 直接餵給 espnow 一定失敗。
    更糟的是 `add_peer()` 原本把例外**吞掉還回 True** —— 靜默說謊。

    ⚠️ 這也解釋了一個**既有潛在 bug**：舊版 `remote._tx()` 的
    `now.write_to(peer["mac"], frame)` 傳的正是 hex 字串 → **定向發射在真機上
    從來沒成功過**，只有廣播能用（`BCAST_MAC` 本來就是 bytes）。
    P5 當年只做「離線冒煙測試」（stub），所以沒抓到。

    → 所以**所有**外部 MAC 都在這裡統一轉一次；`self._peers` 的 key 也一律
      用 bytes（先前 bytes/字串混用會讓 `max_learn_peers` 把同一台算兩次）。
    """
    if mac is None:
        return None
    if isinstance(mac, (bytes, bytearray)):
        return bytes(mac) if len(mac) == 6 else None
    if isinstance(mac, str):
        s = mac.replace(":", "").replace("-", "").replace(" ", "").strip()
        if len(s) != 12:
            return None
        try:
            return ubinascii.unhexlify(s)
        except Exception:
            return None
    return None


def _key16(key):
    """16-byte 金鑰的 hex 字串 → bytes。不對就回 None。

    ESP-NOW 的 PMK/LMK 都是 **16 bytes**（ESP-IDF 規定），長度不對時
    `set_pmk` 會失敗或靜默做錯事 → 在這裡先驗掉。
    """
    if key is None:
        return None
    if isinstance(key, (bytes, bytearray)):
        return bytes(key) if len(key) == 16 else None
    if not isinstance(key, str):
        return None
    t = key.replace(":", "").replace("-", "").replace(" ", "").strip()
    if len(t) != 32:
        return None
    try:
        b = ubinascii.unhexlify(t)
    except Exception:
        return None
    return b if len(b) == 16 else None


class NowBus:

    def __init__(self, label="NOW-Bus", rx_hub=None):
        self.label = label
        self.connected = False
        self._esp = None
        self._peers = {}
        self._last_src_mac = None
        self._decode_ctx = {}

        # ── 加密（2026-10；使用者定案：**全體共用一組金鑰**）──────────
        #   pmk = 網路金鑰、lmk = 連結金鑰，都是 16 bytes。
        #   存在 btree（`@now.pmk` / `@now.lmk`），**不進 config.json**
        #   —— config 是人可讀、會被 commit 的，金鑰不該在那裡。
        self.encrypt = False
        self.pmk = None
        self.lmk = None

        buf_cfg = bus.shared.get('Buffer', {}) or {}
        self.rx_hub = rx_hub
        self._hub_off = 2
        if self.rx_hub is None:
            slots = int(buf_cfg.get("now_rx_slots", 2) or 0)
            if slots > 0:
                slots = min(slots, 4)
                self.rx_hub = AtomicStreamHub(MAX_PAYLOAD + self._hub_off, num_buffers=slots)
        self._drop_on_full = int(buf_cfg.get("drop_on_full", 0) or 0)
        self._drain_reads = int(buf_cfg.get("drain_reads", 1) or 0)
        if self._drain_reads <= 0:
            self._drain_reads = 1

        self.stats = {"rx": 0, "tx": 0, "tx_ok": 0, "tx_fail": 0, "rx_drop": 0}

        # ── 被動學習的射頻 peer 上限 ──────────────────────────────────
        #   ESP-NOW 的 peer 表是**硬體資源**：ESP-IDF 上限 20（加密時 6），
        #   超過 → esp_now_add_peer 回 ESP_ERR_ESPNOW_NO_MEM。
        #   poll() 會對「每個講話的新 MAC」add_peer（因為 MAC 無法枚舉，
        #   錯過那一刻就再也認不出對方）—— 但來源是**任意的射頻輸入**：
        #   附近別人的 ESP-NOW 裝置、隨機雜訊，都會消耗這張表。
        #   表填滿後，真正要綁定的目標反而進不來（而且沒人會發現）。
        #   → 被動學習**設上限**；使用者明確按「綁定」的 add_peer 不受限
        #     （那是意圖，不是雜訊）。上限可調，預設 16（留 4 格給明確綁定）。
        now_cfg = bus.shared.get('Network', {}).get('ESP_now', {}) or {}
        self.max_learn_peers = int(now_cfg.get("max_learn_peers", 16) or 0)
        if self.max_learn_peers <= 0:
            self.max_learn_peers = 16
        self._learn_full_logged = False

    def init(self, channel=None):
        try:
            sta = network.WLAN(network.STA_IF)
            ap = network.WLAN(network.AP_IF)
            # ══════════════════════════════════════════════════════════
            # ★ ESP-NOW 綁的是 **STA**，不是 AP（2026-10 真機實測）
            #   實測（`temp/board_espnow_iface.py`，ESP32-S3）：
            #     STA-only  → send() 正常
            #     AP-only   → send() 回 **ESP_ERR_ESPNOW_IF (-12396)**
            #     兩者都開  → send() 正常
            #   所以**不論 AP 狀態如何，STA 一定要開**。
            # ══════════════════════════════════════════════════════════
            #   原本的條件是 `not sta.active() and not ap.active()`：
            #   AP 開著時整個分支被跳過 → STA 不開 → 但下面的
            #   `ESPNow().active(True)` 與 `add_peer()` **都會成功**（不報錯），
            #   於是 `connected = True`，而之後**每一次 send() 都失敗**。
            #   而 send() 的錯誤處理只有一個計數器（不印、不回報）
            #   → **UI 的 ESP-NOW 開關顯示 ON，但什麼都送不出去**。
            #   這是典型的靜默失敗，所以修在這裡並把實際介面狀態印出來。
            if not sta.active():
                ch = channel
                if ch is None and ap.active():
                    try:
                        ch = ap.config('channel')      # 跟著 AP 的頻道
                    except Exception:
                        ch = None
                if ch is None:
                    print(f"❌ [{self.label}] 沒有可用頻道（STA 未開、AP 也沒開）")
                    return False
                sta.active(True)
                sta.config(channel=ch)
                time.sleep_ms(100)
            elif channel is not None:
                # ★ 2026-10 修正：舊碼在 STA 已開時**靜默忽略** channel
                #   —— 呼叫端以為設了，其實沒有。改成走 set_channel()，
                #   並把「要求 vs 實測回報」印出來。
                self.set_channel(channel)

            self._esp = espnow.ESPNow()
            self._esp.active(True)
            # ★ 走 add_peer()，不要直接呼叫 espnow.add_peer()：
            #   直接呼叫只會動硬體表，`self._peers` 不知道有這一筆，
            #   之後任何「確保存在」都會撞到 ESP_ERR_ESPNOW_EXIST。
            self.add_peer(BCAST_MAC)
            self.connected = True
            print(f"✅ [{self.label}] ESP-NOW active (iface=STA), channel={self._channel()}")
            return True
        except Exception as e:
            print(f"❌ [{self.label}] Init failed: {e}")
            return False

    def _channel(self):
        try:
            sta = network.WLAN(network.STA_IF)
            if sta.active():
                return sta.config('channel')
            ap = network.WLAN(network.AP_IF)
            if ap.active():
                return ap.config('channel')
        except Exception:
            pass
        return '?'

    def set_channel(self, ch):
        """**執行期**換頻道（不必重啟、不必重開 ESP-NOW）。回 True/False。

        ══════════════════════════════════════════════════════════════════
        ★ 為什麼需要這個（2026-10 真機兩板實測，`temp/probe_channel_switch.py`）
        ══════════════════════════════════════════════════════════════════
        在此之前「換頻道」等於「改 config ＋ 重啟」—— 因為 `init(channel=N)`
        只在 **STA 未啟動**時才設頻道（`if not sta.active():`），
        STA 一開著就**靜默忽略**；而 `0x1301` 也只有 0=查詢／1=開／2=關。
        實測：`sta.config(channel=N)` 在 STA 已 active 時**當場生效**，
        兩端都換完之後通訊立刻恢復（B3：收 12/12 幀）
        → 缺口純粹在這一層，**不是硬體限制**。

        ★ 語意（使用者定案）：**頻道＝區分不同網路**，不是漫遊。
          同頻道的 Master／Slave 才看得到彼此；換頻道＝搬到另一個網路，
          **不做跨頻道兼容、不需要掃描**。
          Master 要跟去別的頻道就是呼叫這裡，不必重啟。

        ★ 為什麼不必重建 peer：`add_peer` 帶的 channel 是 0（＝跟隨當前頻道），
          真機 `get_peer` 回報第 3 欄就是 0 → 換頻道後既有 peer 自動跟著走。
          （per-peer channel 也支援 —— `add_peer(mac, channel=N)` ——
           但「頻道＝網路」的語意下用不到，留給未來。）
        """
        try:
            ch = int(ch)
        except Exception:
            print("⚠️ [{}] 頻道不是數字: {!r}".format(self.label, ch))
            return False
        if not (1 <= ch <= 13):
            print("⚠️ [{}] 頻道 {} 不在 1..13".format(self.label, ch))
            return False
        try:
            sta = network.WLAN(network.STA_IF)
            if not sta.active():
                sta.active(True)
                time.sleep_ms(100)
            sta.config(channel=ch)
            time.sleep_ms(50)
            got = sta.config('channel')
            print("📶 [{}] 頻道 → {}（實測回報 {}）".format(self.label, ch, got))
            return got == ch
        except Exception as e:
            print("❌ [{}] 換頻道失敗: {}".format(self.label, e))
            return False

    def deinit(self):
        try:
            if self._esp:
                self._esp.active(False)
                self._esp = None
            self.connected = False
            self._peers.clear()
            self._last_src_mac = None
            self.encrypt = False      # 射頻關了，加密狀態不保留
            print(f"🔌 [{self.label}] Deinitialized")
        except Exception:
            pass

    def _hw_add_peer(self, mb, enc):
        """真正呼叫 `espnow.add_peer`（處理加密的形式差異）。

        MicroPython 的形狀是 `add_peer(mac, lmk=None, channel=None, ifidx=0, encrypt=False)`
        —— 真機 `get_peer()` 回的就是這 5 欄。加密時要帶 LMK（全體共用那一組）。
        ⚠️ 這裡用位置參數並留 kwarg 後備：不同版本對 `channel`/`encrypt` 的接受度
           不一，而**硬體驗證還沒做**（板子不在），所以兩條路都寫。
        """
        if not enc:
            return self._esp.add_peer(mb)
        if self.lmk:
            try:
                return self._esp.add_peer(mb, self.lmk, None, 0, True)
            except TypeError:
                pass
        return self._esp.add_peer(mb, encrypt=True)

    def add_peer(self, mac, encrypt=None):
        """註冊一個射頻 peer（冪等）。**hex 字串也會自動轉 bytes**（見 `mac_bytes`）。

        `encrypt=None` → 用本管的 `self.encrypt`（`set_encrypt()` 設的）。

        ★ 2026-10：先前例外被吞掉、函式仍回 True —— 拿 hex 字串進來時
          espnow 拋 `invalid buffer length`，這裡卻說成功（**靜默說謊**）。
          現在：認不出來的位址回 False 並印訊息。

        ★ 2026-10（真機抓到）：`ESP_ERR_ESPNOW_EXIST` 也要算**成功**。
          它意思是「硬體表裡已經有了」，正是「確保存在」想要的結果。
          把它當失敗時，`learn_peer()` 會對**已經認識的對象回 False**
          —— 配對流程於是誤判成「學不到對方」，明明對方就在表裡。
        """
        mb = mac_bytes(mac)
        if mb is None:
            print("⚠️ [{}] add_peer 位址格式不合法: {!r}".format(self.label, mac))
            return False
        if mb in self._peers:
            return True
        # ★ encrypt=None ＝「沿用本管的設定」。不能寫成
        #   `bool(encrypt) and self.encrypt` —— None 會被 bool() 壓成 False，
        #   於是加密永遠不生效（測試 §23 抓到）。
        enc = self.encrypt if encrypt is None else bool(encrypt)
        # ★★ 廣播**永遠不加密**：ESP-NOW 的加密只支援**單點**（encrypted
        #    peer 不能用廣播位址送）。讓它帶 encrypt=True 會變成
        #    「加得進去、但廣播從此送不出去」—— 又是靜默失敗。
        #    這也是為什麼 peer_cap() 加密時是 6：廣播那格不佔加密額度。
        if mb == BCAST_MAC:
            enc = False
        try:
            self._hw_add_peer(mb, enc)
        except Exception as e:
            if _is_exist_error(e):
                self._peers[mb] = True
                return True
            print("⚠️ [{}] add_peer({}) 失敗: {}".format(
                self.label, mb.hex(), e))
            self._peers[mb] = True      # 記住「試過了」，避免每次重試洗版
            return False
        self._peers[mb] = True
        return True

    def peer_cap(self, encrypt_on=None):
        """「已選」清單的上限（**不含**廣播那一格）。加密與否上限不同。

        ★ 一般 19 = MAX_PEERS(20) - 廣播(1)：廣播 peer 是**未加密**的，
          與加密 peer 分開算，所以加密時不必從 6 裡面再扣。
        ★ 加密 6 = MAX_PEERS_ENCRYPTED —— ESP-IDF 的加密 peer 上限。
        """
        enc = self.encrypt if encrypt_on is None else bool(encrypt_on)
        return MAX_PEERS_ENCRYPTED if enc else (MAX_PEERS - 1)

    def set_encrypt(self, on, pmk=None, lmk=None):
        """開／關 ESP-NOW 加密（**全體共用一組金鑰**）。回 True/False。

        ══════════════════════════════════════════════════════════════════
        ★ 沒有金鑰就**不給開**（刻意的）
        ══════════════════════════════════════════════════════════════════
        `encrypt=True` 硬體收得下，但對端沒有同一組 PMK/LMK 就解不開
        —— 「加得進去、卻什麼都收不到」是最難查的那種失敗。
        寧可在這裡擋掉並說清楚，也不要讓它靜默不通。

        `pmk`/`lmk` 是 16-byte 的 hex 字串（大小寫不拘、可含冒號）。
        沒給就沿用目前的。
        """
        if on:
            pk = _key16(pmk) if pmk else self.pmk
            lk = _key16(lmk) if lmk else self.lmk
            if not pk or not lk:
                print("⚠️ [{}] 沒有金鑰（pmk/lmk）→ 不開加密".format(self.label))
                return False
            try:
                self._esp.set_pmk(pk)
            except Exception as e:
                print("❌ [{}] set_pmk 失敗: {}".format(self.label, e))
                return False
            self.pmk, self.lmk = pk, lk
        self.encrypt = bool(on)
        print("🔐 [{}] 加密 {}（已選上限 {}）".format(
            self.label, "ON" if on else "OFF", self.peer_cap()))
        return True

    def clear_peers(self):
        """清掉**所有**射頻 peer（含簿記），只留廣播。回移除數。

        「清除所有記錄」（使用者定案）＝**只清 ESP-NOW 的**：
        硬體 peer 表 + 已選清單 —— 節點記錄（`PeerRegistry`）與配對方向
        （`master_cid`）都**不動**。
        """
        n = 0
        for mb in list(self._peers.keys()):
            if mb == BCAST_MAC:
                continue
            if self.del_peer(mb):
                n += 1
        self.encrypt = False          # 清光之後沒有加密對象了
        print("🧹 [{}] peer 表清空：移除 {} 筆（廣播保留）".format(self.label, n))
        return n

    def del_peer(self, mac):
        """從**硬體表**移除一個 peer（簿記一起清）。回 True/False。

        ══════════════════════════════════════════════════════════════════
        ★ 為什麼需要（2026-10 真機確認）
        ══════════════════════════════════════════════════════════════════
        ESP-NOW 的 peer 表是 **MAX_PEERS(20) 格硬體資源**，而在這之前它
        **只能加不能減**：被動學習或誤綁佔走的格子，只能靠**重啟**才清得掉。
        `espnow.del_peer` 一直存在（真機 `dir(espnow.ESPNow())` 確認），
        只是沒接 —— 接上它，「重啟」就不再是清理 peer 表的唯一手段。

        廣播位址（`BCAST_MAC`）**不給刪**：它固定佔一格，是雙向通訊的前提。
        """
        mb = mac_bytes(mac)
        if mb is None:
            print("⚠️ [{}] del_peer 位址格式不合法: {!r}".format(self.label, mac))
            return False
        if mb == BCAST_MAC:
            return False
        self._peers.pop(mb, None)
        try:
            self._esp.del_peer(mb)
        except Exception:
            pass            # 硬體表本來就沒有 → 目標（不在表裡）已達成
        return True

    def apply_peers(self, keep):
        """把 peer 表**重建**成「廣播 ＋ `keep` 清單」。回 `(新增, 移除, 失敗)`。

        ══════════════════════════════════════════════════════════════════
        ★ 為什麼是「重建」而不是「逐一補」（使用者定案的 UI 流程）
        ══════════════════════════════════════════════════════════════════
        UI 是 Windows 式的**兩張表**：`可選`（掃描到的）／`已選`（使用者挑的）。
        按下「套用」時，**已選清單就是唯一事實** ——
        掃描期間被動學到的其他 MAC，只在「知道對方位址」那一刻有用；
        清單決定之後就該讓位，否則雜訊會一直吃硬體格子。

        ★ 滿了**不自動淘汰**（使用者定案）：直接拒絕並提示「請先移除一些」。
          上限＝`MAX_PEERS` 扣掉廣播那 1 格。
        """
        keep_set = set()
        for m in (keep or ()):
            mb = mac_bytes(m)
            if mb is not None and mb != BCAST_MAC:
                keep_set.add(mb)

        cap = self.peer_cap()
        if len(keep_set) > cap:
            print("⚠️ [{}] 已選 {} 台，超過上限 {}（{}）→ 請先移除一些".format(
                self.label, len(keep_set), cap,
                "加密上限" if self.encrypt
                else "硬體表 {} 格扣掉廣播 1 格".format(MAX_PEERS)))
            return 0, 0, len(keep_set)

        before = len(self._peers)
        failed = 0
        for mb in keep_set:
            if not self.add_peer(mb):
                failed += 1
        added = max(0, len(self._peers) - before)

        removed = 0
        for mb in list(self._peers.keys()):
            if mb == BCAST_MAC or mb in keep_set:
                continue
            if self.del_peer(mb):
                removed += 1

        print("📋 [{}] peer 表套用：已選 {} ／新增 {} ／移除 {} ／失敗 {} → 現有 {}".format(
            self.label, len(keep_set), added, removed, failed, self.peer_count))
        return added, removed, failed

    def learn_peer(self, mac):
        """**被動**學會一個射頻位址（收幀時呼叫）—— 受 `max_learn_peers` 限制。

        與 `add_peer()` 的差別就是「誰決定的」：
          add_peer   = 使用者的意圖（UI 綁定 / 定向發射前補通道）→ 不設限
          learn_peer = 射頻上剛好有人講話 → 設限，避免雜訊吃光硬體 peer 表

        為什麼「收幀就必須學」：ESP-NOW 的位址是 MAC，**無法枚舉**，
        所以「知道對方位址」的唯一時機就是它的幀到達射頻層的這一刻。
        不在這裡登記，之後 write()（單播回覆）會拿到 ESP_ERR_NOT_FOUND(-12393)。
        """
        mb = mac_bytes(mac)
        if mb is None:
            return False
        mac = mb
        if mac in self._peers:
            return True
        if len(self._peers) >= self.max_learn_peers:
            if not self._learn_full_logged:
                self._learn_full_logged = True
                print("⚠️ [{}] 被動 peer 表已滿 ({}), 不再自動登記新來源; "
                      "明確綁定不受限".format(self.label, self.max_learn_peers))
            return False
        return self.add_peer(mac)

    def has_peer(self, mac):
        mb = mac_bytes(mac)
        return mb is not None and mb in self._peers

    @property
    def peers(self):
        return list(self._peers.keys())

    @property
    def peer_count(self):
        return len(self._peers)

    def send(self, mac, data):
        """送一幀給 `mac`。`mac` 可以是 **6-byte bytes 或 hex 字串**。

        ★ 收斂點：`resolve_addr()` 回傳的是 `PeerRegistry.mac` /
          `bus.master_mac`（**hex 字串**），而 espnow 只吃 bytes
          —— 全部在這裡轉一次（見 `mac_bytes` 的實測記錄）。
        """
        if not self.connected:
            return False
        if len(data) > MAX_PAYLOAD:
            return False
        mb = mac_bytes(mac)
        if mb is None:
            print("⚠️ [{}] send 位址格式不合法: {!r}".format(self.label, mac))
            self.stats["tx"] += 1
            self.stats["tx_fail"] += 1
            return False
        try:
            ok = self._esp.send(mb, data)
            self.stats["tx"] += 1
            if ok:
                self.stats["tx_ok"] += 1
            else:
                self.stats["tx_fail"] += 1
            return ok
        except Exception:
            self.stats["tx"] += 1
            self.stats["tx_fail"] += 1
            return False

    def broadcast(self, data):
        return self.send(BCAST_MAC, data)

    def resolve(self, data):
        """依**幀頭的 addr** 解析出這一幀該去哪（回傳射頻位址，或 BCAST_MAC）。

        ★ 這是「管子自己解析位址」的實作 —— 協議層（含 UI）只說「把這一幀送出去」，
          位址已經寫在 `addr` 裡了，**MAC 不該外流**（見 todo/05 §6.2 / D14）。

        解析順序（**兩端共用同一張表**，各自讀自己已經存好的記錄）：
          1. `0xFFFF`               → 廣播
          2. `== bus.master_cid`    → `bus.master_mac`   ← **Slave 端**主要路徑
          3. `peers.by_cid(addr)`   → 該 peer 的 mac     ← **Master 端**主要路徑
          4. 都查不到               → 「剛剛講話的人」    ← 保留既有回覆語意，相容

        ★ 第 3 條讓 `PeerRegistry.by_cid()` 從「零呼叫者的死碼」變成必要零件
          —— 它本來就該被這條路徑用（文件 `sys_bus.py:31` 早就這樣寫）。
        """
        try:
            addr = data[3] | (data[4] << 8)      # NC4 幀頭 addr（proto.py:222）
        except Exception:
            addr = ADDR_BROADCAST
        return self.resolve_addr(addr)

    def resolve_addr(self, addr):
        """`resolve()` 的位址版本（好測；也是延後發射要用的同一套規則）。"""
        addr = int(addr) & 0xFFFF
        if addr == ADDR_BROADCAST:
            return BCAST_MAC
        # 2) 我的 Master（Slave 端）—— cid 與 mac 成對存在 bus 上
        try:
            mcid = int(getattr(bus, "master_cid", ADDR_BROADCAST)) & 0xFFFF
            if addr == mcid:
                mm = getattr(bus, "master_mac", None)
                if mm:
                    return mm
        except Exception:
            pass
        # 3) 節點表（Master 端）
        try:
            reg = bus.get_service("peers")
            if reg is not None:
                rec = reg.by_cid(addr)
                if rec and rec.get("mac"):
                    return rec["mac"]
        except Exception:
            pass
        # 4) 回退：剛剛講話的人（`ctx["send"]` 的回覆語意）
        if self._last_src_mac is not None:
            return self._last_src_mac
        return BCAST_MAC

    def write(self, data):
        """把一幀送出去 —— **依幀頭 addr 解析目的地**（不再固定回 `_last_src_mac`）。

        ★ 這是「Router 轉發」能成立的前提：`signal_router._forward()` 只會呼叫
          `dst.write(frame)`，沒有目的地位址參數（`signal_router.py:812`）。
          以前 `write()` 固定回「剛剛講話的人」→ 轉發到 `now` 的幀到不了目標。
        ★ 解析不到時仍回退 `_last_src_mac`，所以**既有的回覆路徑逐字不變**。
        """
        if not self.connected:
            return False
        return self.send(self.resolve(data), data)

    def write_to(self, dst_mac, data):
        """定向送給 `dst_mac`（射頻層）。None → 退回 `write()`（依 addr 解析）。

        命名規則見 `doc/01_protocol/01_nc4_protocol.md`「定址詞彙表」。
        延後發射會用「收幀當下捕獲的 `src_mac`」當這裡的 `dst_mac`。
        """
        if dst_mac is None:
            return self.write(data)
        return self.send(dst_mac, data)

    def poll(self, **extra_ctx):
        if not self.connected:
            return
        if self.rx_hub is None:
            return

        if extra_ctx:
            self._decode_ctx = extra_ctx

        for _ in range(self._drain_reads):
            try:
                peer, msg = self._esp.recv(0)
            except Exception:
                break

            if peer is None or not msg:
                break

            self.stats["rx"] += 1
            n = len(msg)

            # ── 學會「剛剛是誰在講話」─────────────────────────────────
            #   ESP-NOW 的位址是 MAC，而 MAC **無法枚舉** —— 唯一能知道對端
            #   位址的時機，就是它的幀真的到達射頻層的這一刻。
            #   不在這裡登記的話，之後要回它（write() → send(peer)）會拿到
            #   ESP_ERR_NOT_FOUND(-12393)：ESP-NOW 不接受「沒註冊過的 peer」，
            #   即使我們手上已經有它的 MAC。
            #   → 這是「被發現 / 雙向通訊」成立的最小條件，也是 PeerRegistry
            #     被動學習（learn_from_frame）的前提。
            #   用 learn_peer（不是 add_peer）：來源是任意射頻輸入，要設上限，
            #   否則附近的雜訊會把 ESP-NOW 那張 20 格的硬體 peer 表吃光。
            self.learn_peer(peer)

            view = self.rx_hub.get_write_view()
            if view is None:
                self.stats["rx_drop"] += 1
                if not self._drop_on_full:
                    break
                continue

            pv = memoryview(view)[self._hub_off:]
            available = len(pv)

            if n > available:
                n = available
                msg = msg[:n]

            struct.pack_into("<H", view, 0, n)
            pv[:n] = msg
            self.rx_hub.commit()

            self._last_src_mac = peer
            self._decode_ctx["_src_mac"] = peer

        return

    # ══════════════════════════════════════════════════════════════════
    # ★★ `espnow.recv()` 會回 **`(mac, None)`** —— 那不是資料幀，是
    #    peer event／控制訊框。2026-10 真機實測（`temp/probe_channel_switch.py`）：
    #    3 秒的接收迴圈裡出現 **2871 次**，幾乎每次呼叫都是。
    #
    #    ⚠️ 所以「`peer is None` 才算沒東西」是**錯的**。原本 `recv()` 只擋
    #    `peer is None`，接著 `bytes(None)` → `TypeError`，而且它會被
    #    `discover()` 的迴圈**原樣拋出去**。
    #
    #    `poll()` 沒事，因為它用 `not msg` 擋（None 與 b"" 都擋掉）——
    #    這也是為什麼應用程式一直看起來正常。
    #    `discover()` 目前**零呼叫者**，所以這是**潛在** bug
    #    （與 `PeerRegistry.by_cid()` 那件的性質相同：接上去才會咬人）。
    #
    #    → 兩個方法都在這裡一次擋掉 `msg is None`。
    # ══════════════════════════════════════════════════════════════════
    def recv(self):
        """收一幀。回 `(peer, msg)`；沒有東西（含 peer event）回 `(None, None)`。"""
        try:
            peer, msg = self._esp.recv(0)
        except Exception:
            return None, None
        if peer is None or msg is None:
            return None, None
        self.stats["rx"] += 1
        return peer, bytes(msg)

    def recv_timeout(self, timeout_ms):
        """同 `recv()`，但最多等 `timeout_ms`。"""
        try:
            peer, msg = self._esp.recv(timeout_ms)
        except Exception:
            return None, None
        if peer is None or msg is None:
            return None, None
        self.stats["rx"] += 1
        return peer, bytes(msg)

    def discover(self, discover_payload, timeout_ms=2000):
        online = {}
        self.broadcast(discover_payload)

        deadline = time.ticks_ms() + timeout_ms
        while time.ticks_ms() < deadline:
            peer, msg = self.recv()
            if peer is not None and msg:
                online[peer] = msg
                self.add_peer(peer)
            time.sleep_ms(10)

        return online

    def send_proto(self, mac, cmd, payload=b"", addr=0xFFFF):
        frame = Proto.pack(cmd, payload, addr)
        if len(frame) > MAX_PAYLOAD:
            return False
        return self.send(mac, frame)

    def broadcast_proto(self, cmd, payload=b"", addr=0xFFFF):
        return self.send_proto(BCAST_MAC, cmd, payload, addr)
