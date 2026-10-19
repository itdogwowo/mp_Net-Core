# main.py — 無顯示兩鍵遙控器（NC4 協議 / ESP-NOW 傳輸）
#    實測平台：ESP32-S3（MicroPython 1.29）；ESP32-C6 亦可用，只有 GPIO 編號不同
# ══════════════════════════════════════════════════════════════════════
# 角色：**遙控器**。只發指令，不執行任何模式（不跑 slave 框架、不跑 Router、
#       不跑 UI）。整個固件就是這一個檔案。
#
# 行為（與使用者定案一致）：
#   開機   ：本地模式預設 BOOT_MODE(0) → 送一次 0x3103 MODE_GET 取得對方目前模式
#            （**只有開機這一次**；之後每次發送前不再問）
#   按鈕1  ：進入可動模式 0x0200（= mode_type 2 / mode_id 0）
#   按鈕2  ：下一個模式 = 目前模式 + 1（16-bit 合併 id 直接 +1）
#   兩顆鈕：送出 0x3105 MODE_SET → 延遲 CONFIRM_DELAY_MS → 再 0x3103 確認一次
#           確認逾時 → **不覆蓋本地值**，本地保留 +1 後的結果（後備方案）
#
# 配對（**不自動配對**）：
#   scan()       廣播 0x100D 點名 → 收 0x100E → 把 PEER_MAC / PEER_CID **印出來**
#                給你複製回檔頭（本檔不會自動套用、也不會自己送 0x1016）
#   listen(sec)  純被動監聽，印出每一幀的來源 MAC（對方沒實作 0x100D 時的唯一辦法）
#   觸發方式     長按按鈕1（BTN_LONG_PRESS_MS）或直接在 REPL 打 scan() / listen(10)
#   暫時預設廣播：`PEER_MAC = None` ＋ `PEER_CID = None`（還沒確認就不猜）
#                 位址沒填就自動廣播 → 還沒配對也一定送得到（沒有單播/廣播開關）
#
# 協議（NC4，權威＝slave/lib/sys/proto.py）：
#   ┌──────┬─────┬───────┬───────┬───────┬──────────┬────────┐
#   │ SOF  │ VER │ ADDR  │ CMD   │ LEN   │  DATA    │ CRC32  │
#   │ 2B   │ 1B  │ 2B LE │ 2B LE │ 2B LE │ LEN B    │ 4B LE  │
#   └──────┴─────┴───────┴───────┴───────┴──────────┴────────┘
#   SOF="NC"、VER=4、CRC 範圍 = buffer[2 : 9+LEN]（不含 SOF、不含 CRC 自身）
#
#   ADDR（幀頭）＝**協議層**位址：廣播 0xFFFF 或「對方的 cid」
#   射頻目的地＝**射頻層** MAC：廣播 FF:FF:FF:FF:FF:FF 或 PEER_MAC
#   ★ 這兩層不同、不可互推；本檔在 resolve_target() 一處同時決定兩層。
#
# 檔案：ports/C6/ESP32-C6-Remote/main.py（獨立固件，不覆蓋 slave/）
# ══════════════════════════════════════════════════════════════════════

import os
import sys
import time
import struct
import binascii

try:
    # 硬體模組（MicroPython）。CPython 下不存在 → 讓本檔可離線 import 做協定自測。
    import machine
    import network
    import espnow
except ImportError:                      # pragma: no cover
    machine = None
    network = None
    espnow = None


# ══════════════════════════════════════════════════════════════════
#  ① 使用者參數 —— 只改這一區
# ══════════════════════════════════════════════════════════════════
#  目錄（依「你會依序需要」排；每一項都只有一個住址）：
#    A. 按鈕接線      BTN_MOVABLE_PIN / BTN_NEXT_PIN / BTN_ACTIVE_LOW / DEBOUNCE_MS / BTN_LONG_PRESS_MS
#    B. 射頻與對象    CHANNEL / PEER_MAC / PEER_CID / MY_CID / SEND_TARGET / PAIR_AS_MASTER
#    C. 模式指令      MOVABLE_MODE / BOOT_MODE / STEP / STEP_BASE_IF_UNKNOWN / BRIGHTNESS / START_DELAY_MS
#    D. 手動探索      SCAN_REPLY_DIRECTED / SCAN_WINDOW_MS / SCAN_FORCE
#    E. 回饋 LED      LED_PIN / LED_TYPE / LED_SHOW_MODE
#    F. 時序與除錯    CONFIRM_AFTER_SET / CONFIRM_DELAY_MS / RSP_TIMEOUT_MS / RETRIES / ADDR_FILTER / DEBUG

# ══ A. 按鈕接線（兩顆都接 GND：內部上拉、按下＝低電位）══════════
BTN_MOVABLE_PIN = 11        # ★ 按鈕1「進入可動模式」接哪個 GPIO ← 改成你實際接的
BTN_NEXT_PIN    = 12        # ★ 按鈕2「下一個模式 +1」接哪個 GPIO ← 改成你實際接的
                            #   S3 要避開：0/3/45/46（strapping）、19/20（USB）、33–37（八線 PSRAM）
                            #   C6 要避開：8/9/15（strapping）、12/13（USB）
BTN_ACTIVE_LOW  = True      # 接 GND → True（內部上拉，按下讀到 0）
DEBOUNCE_MS     = 30        # 去抖時間（ms）。太小會連發、太大會漏短按
BTN_LONG_PRESS_MS = 3000    # 按住按鈕1 幾 ms = 觸發 scan()（None = 停用長按）
                            #   短按＝放開才成立；長按＝按住達標就觸發（不會同時切成模式）

# ══ B. 射頻與對象 ══════════════════════════════════════════════
CHANNEL = 6                 # Wi-Fi 頻道 1~13。**必須與對方完全相同**，否則一幀都收不到

# ── 對方（要控制的執行裝置）——**沒確認過就保持 None，不要填猜的值** ──
PEER_MAC = "a0f262eaeb04"   # 對方的 STA MAC（12 個 hex，可含 : - 或空白）
                            #   ★ 目前值＝2026-10 真機 scan() 掃到的節點 A0:F2:62:EA:EB:04
                            #   None = 沒確認 → 廣播；"ffffffffffff" = 明寫射頻廣播
PEER_CID = 0xEB04           # 對方的協議位址（cid）→ 填進 NC4 幀頭 addr
                            #   ★ 目前值＝該節點的 cid（＝MAC 末四碼 EB04，scan() 印出來的）
                            #   None = 沒確認 → 不能單播（會自動走廣播）
MY_CID   = None             # **我們自己的**協議位址（跟「對方認誰當 master」無關）
                            #   ★ None（預設）= 照專案規則自動決定：自己 MAC 末 4 碼
                            #     （`ConfigManager.ensure_cID()` 用的就是這條）。
                            #     本機 e4:b3:23:f9:32:00 → "3200" → 0x3200（開機會印出來）。
                            #   ★ 要覆寫才填（0..0xFFFF）。
                            #   ⚠️ **不要把 `0x1101{node.master_cid}` 查到的值填進來**！
                            #     那是「對端目前回話的對象」（＝面板，cid 0x0001）——是**對方的**
                            #     資料，不是我們的 id。填進去等於冒充面板。
                            #   ★ 這是「我們自己的 cid」**唯一**的存放處，沒有第二個地方要填

# ── 「這一幀送給誰」——**只有這一個地方決定** ────────────────────
#   本樹自己的結論（doc/03_notes/19_remote_control_plan.md §2.6）：
#     「❌ 不需要『廣播/單播設定開關』（原設計方向錯誤）；
#       ✅ 知道目標 → 定向；不知道 → 廣播。」
#   → 「有沒有對象」由 PEER_MAC/PEER_CID 有沒有填回答（資料自己講），
#     這裡只表達**唯一的例外**：什麼時候要刻意對全部講。
SEND_TARGET = "first"       # "first"（預設）= 開機第一幀先廣播（破冰：讓對方學到我們的
                            #     MAC），之後照對象走；沒有對象就一律廣播
                            # "peer"        = 一開始就照對象走（知道就打一台、不知道就廣播）
                            # "all"         = 一律廣播（同頻道每一台都執行，例如全部一起換模式）
PAIR_AS_MASTER = False      # 開機時額外送 0x1016 SET_MASTER{MY_CID}（＝「回覆寄給我」）
                            #   ★ 這就是「自動配對」→ 本檔**刻意預設關**。
                            #     要配對請用 scan() 手動複製位址；要用再自己開
                            #   ⚠️ 與 SCAN_REPLY_DIRECTED 是**不同機制**：這個是「以後都回給我」
                            #      （會寫進對方 flash、可能被拒絕），那個只影響「這一次掃描的回信」

# ══ C. 模式指令參數（0x3105 MODE_SET）══════════════════════════
MOVABLE_MODE   = 0x0200     # 按鈕1：可動模式＝ mode_type 2(SERVO) / mode_id 0 → 合併 id 0x0200
BOOT_MODE      = 0x0000     # 開機時「本地預設」模式（GET 有回覆就被覆蓋）
STEP           = 1          # 按鈕2 的增量（16-bit 合併 id 直接 +1）
STEP_BASE_IF_UNKNOWN = MOVABLE_MODE
                            # 從未同步成功（高 byte = 0，type 0 是系統模式、不可 MODE_SET）時，
                            # +1 改以這個為基準 → 0x0201 而不是無用的 0x0001。設 None = 純 +1
BRIGHTNESS     = 255        # 0~254 = 設定亮度；**255 = 不改**（協議規定，0 是合法值）
START_DELAY_MS = 0          # 對方收到後延遲幾 ms 才起播（0=立即）

# ══ D. 手動探索（掃描）——只印出來，不自動套用 ══════════════════
#   觸發：長按按鈕1（BTN_LONG_PRESS_MS）／REPL 打 scan()／REPL 打 listen(15)
#   送出的指令：0x100D IDENTIFY_REQ（廣播），payload = reply_cid(u16) window(u16) force(u8)
SCAN_REPLY_DIRECTED = False # 對方那封回信要「只有我們收」嗎？（0x100D 的 reply_cid）
                            #   False（預設）= 回信用 0xFFFF → **射頻廣播**
                            #        → 我們還沒跟任何人配對也收得到（只需 add_peer(BCAST)）
                            #        代價：同頻道的其他裝置也看得到這封回信（只有 cid/MAC/IP）
                            #   True          = 回信用 MY_CID（定向）→ 只有我們收
                            #        前提：MY_CID 已填 ＋ 對方認識我們的 MAC ＋ 收得到單播
                            #   ★ 這裡只決定「要不要定向」；**cid 的值一律來自 MY_CID**，
                            #     不在這裡再填一次（同一個事實只有一個家）
SCAN_WINDOW_MS = 800        # 抖動視窗：對方會隨機延遲 0~此值才回（避免同時回話碰撞）
HELLO_WINDOW_MS = 400       # 開機「破冰點名」的視窗（不用像手動掃描那麼久）
PEER_PROBE_MS = 5000        # 對端已知不回覆時，背景每隔幾 ms 試探一次它是否恢復
                            #   （0 = 停用）。目的：按鍵路徑不要為了一個不說話的
                            #   對端每次卡 RSP_TIMEOUT_MS；探測改在待機時做。
SCAN_FORCE = True           # 每次掃描都要求重新自我介紹（對方對同一個問的人只回第一次）

# ══ E. 回饋 LED（不用就設 LED_PIN = None）══════════════════════
LED_PIN = None              # 例：8 = C6-DevKitC 板載 RGB；None = 完全不使用
LED_TYPE = "gpio"           # 燈的電氣型態 —— **一個參數講完，不會有前後矛盾**：
                            #   "gpio"     = 一般 LED，高電位亮
                            #   "gpio_low" = 一般 LED，低電位亮（共陽／接 VCC）
                            #   "ws2812"   = 板載可定址 RGB（neopixel）
LED_SHOW_MODE = True        # 開機/切換後用閃燈回報模式（無顯示時唯一的確認手段）

# ══ F. 時序與除錯 ═══════════════════════════════════════════════
CONFIRM_AFTER_SET = True    # MODE_SET 後再 GET 一次確認（MODE_SET 本身無 ACK）
CONFIRM_DELAY_MS  = 80      # 送 SET 後等幾 ms 再 GET（給對方時間套用，避免讀到舊值）
RSP_TIMEOUT_MS    = 300     # 等一次回覆的時間
RETRIES           = 3       # 沒回覆時重送幾次（ESP-NOW 無 ACK，掉幀是常態）
ADDR_FILTER       = True    # 只收「給本機」的幀：addr == 0xFFFF（廣播）或 == MY_CID。
                            #   MY_CID 沒填時＝只收廣播（不會因此漏掉任何東西，因為
                            #   沒有自己的 cid 時對方也只能廣播回）
DEBUG             = True    # 印出每一幀 TX/RX（無顯示時靠 REPL 看）


# ══════════════════════════════════════════════════════════════════
#  ② 協議常數與幀編解碼（與 slave/lib/sys/proto.py 位元組完全相同）
# ══════════════════════════════════════════════════════════════════

SOF            = b"NC"
VER            = 4
ADDR_BROADCAST = 0xFFFF
HDR_LEN        = 9
CRC_LEN        = 4
MAX_PAYLOAD    = 250        # ★ 射頻上限：ESP-NOW 單幀 250B（協議本體是 8192）
BCAST_MAC      = b"\xff\xff\xff\xff\xff\xff"

CMD_SET_MASTER     = 0x1016
CMD_SLAVE_ANNOUNCE = 0x1002
CMD_IDENTIFY_REQ   = 0x100D
CMD_IDENTIFY_RSP   = 0x100E
_LED_TYPES     = ("gpio", "gpio_low", "ws2812")   # LED_TYPE 的合法值（唯一一份）
_SEND_TARGETS  = ("first", "peer", "all")         # SEND_TARGET 的合法值（唯一一份）

CMD_MODE_GET     = 0x3103
CMD_MODE_GET_RSP = 0x3104
CMD_MODE_SET     = 0x3105


def _ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else (a - b)


def _ticks_add(t, d):
    return time.ticks_add(t, d) if hasattr(time, "ticks_add") else (t + d)


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:                                    # CPython（離線自測）
        time.sleep(ms / 1000.0)


def _crc32(data):
    """CRC32（= proto.py 的 binascii.crc32）。"""
    return binascii.crc32(bytes(data)) & 0xFFFFFFFF


def nc4_pack(cmd, payload=b"", addr=ADDR_BROADCAST):
    """組一個 NC4 幀 → bytearray。

    CRC 範圍 = buffer[2 : 9+LEN]（VER..DATA），與 proto.py 的 _write_frame 一致。
    """
    payload = bytes(payload)
    ln = len(payload)
    b = bytearray(HDR_LEN + ln + CRC_LEN)
    struct.pack_into("<2sBHHH", b, 0, SOF, VER, addr & 0xFFFF, cmd & 0xFFFF, ln)
    if ln:
        b[HDR_LEN:HDR_LEN + ln] = payload
    struct.pack_into("<I", b, HDR_LEN + ln, _crc32(b[2:HDR_LEN + ln]))
    return b


class FrameRx:
    """NC4 串流解析（SOF 重同步 + CRC 驗證）。**零刪除**，用 start/end 偏移。

    ══════════════════════════════════════════════════════════════════
    ★★ 為什麼不能用 `del buf[:n]`（**真機實測**，2026-10）
    ══════════════════════════════════════════════════════════════════
    CPython 的 bytearray 支援 slice 刪除，所以離線測試全綠；但 **MicroPython
    的 bytearray 不支援**：

        TypeError: 'bytearray' object doesn't support item deletion

    後果是**收到任何一幀都炸**（pop() 無法丟棄已消費的位元組）→ 開機同步永遠
    逾時、掃描永遠掃不到東西，而且 log 只有一行看不出所以然。
    → 照 `slave/lib/sys/proto.py`（已在真機驗證）的作法：緩衝區只增不刪，
      用 `start`/`end` 兩個偏移表示「有效範圍」，`feed()` 時把未消費的尾巴
      搬到前面（slice 賦值，不是刪除）。
    """

    def __init__(self, max_len=MAX_PAYLOAD):
        self.buf = bytearray(max_len + HDR_LEN + CRC_LEN + 8)
        self.start = 0          # 有效資料起點
        self.end = 0            # 有效資料終點
        self.max_len = max_len

    def _compact(self):
        """把未消費的尾巴搬到緩衝區前面（**不刪除**，只覆寫）。"""
        if self.start:
            keep = self.end - self.start
            if keep:
                self.buf[0:keep] = self.buf[self.start:self.end]
            self.start = 0
            self.end = keep

    def feed(self, data):
        self._compact()
        n = len(data)
        need = self.end + n
        if need > len(self.buf):                 # 單幀上限內不會發生，防禦性擴充
            nb = bytearray(need)
            nb[0:self.end] = self.buf[0:self.end]
            self.buf = nb
        self.buf[self.end:need] = data
        self.end = need

    def pop(self):
        """撈出一個完整幀 → (ver, addr, cmd, payload)；沒有完整幀回 None。"""
        buf = self.buf
        while (self.end - self.start) >= HDR_LEN:
            idx = buf.find(SOF, self.start, self.end)
            if idx < 0:
                self.start = 0
                self.end = 0
                return None
            if idx != self.start:
                self.start = idx
                if (self.end - self.start) < HDR_LEN:
                    return None
            s = self.start
            ver = buf[s + 2]
            ln = buf[s + 7] | (buf[s + 8] << 8)
            if ver != VER or ln > self.max_len:
                self.start += 1                  # 錯位：前移 1 byte 續找
                continue
            total = HDR_LEN + ln + CRC_LEN
            if (self.end - self.start) < total:
                return None                      # 等更多資料
            pe = s + HDR_LEN + ln
            crc = (buf[pe] | (buf[pe + 1] << 8) | (buf[pe + 2] << 16) | (buf[pe + 3] << 24))
            if crc != _crc32(buf[s + 2:pe]):
                self.start += 1                  # CRC 錯：視為錯位資料
                continue
            addr = buf[s + 3] | (buf[s + 4] << 8)
            cmd = buf[s + 5] | (buf[s + 6] << 8)
            payload = bytes(buf[s + HDR_LEN:pe])
            s += total
            if s == self.end:
                self.start = 0
                self.end = 0
            else:
                self.start = s
            return (ver, addr, cmd, payload)
        return None


def mode_set_payload(mode16, brightness=None, start_delay_ms=None):
    """0x3105 MODE_SET 的 payload（5B）：
       mode_type(u8) mode_id(u8) start_delay_ms(u16 LE) brightness(u8)
       ★ mode_type/mode_id 由 16-bit 合併 id 拆回：(id>>8, id&0xFF)
    """
    if brightness is None:
        brightness = BRIGHTNESS
    if start_delay_ms is None:
        start_delay_ms = START_DELAY_MS
    return struct.pack("<BBHB",
                       (mode16 >> 8) & 0xFF,
                       mode16 & 0xFF,
                       start_delay_ms & 0xFFFF,
                       brightness & 0xFF)


def decode_mode_rsp(payload):
    """0x3104 MODE_GET_RSP（11B）→ dict；長度不足回 None。

       mode_type(u8) mode_id(u8) elapsed_ms(u32) total_ms(u32) running(u8)
    """
    if len(payload) < 11:
        return None
    mt, mi, elapsed, total, running = struct.unpack("<BBIIB", payload[:11])
    return {
        "mode": (mt << 8) | mi,
        "mode_type": mt,
        "mode_id": mi,
        "elapsed_ms": elapsed,
        "total_ms": total,
        "running": running,
    }


def next_mode_of(cur, step=STEP):
    """按鈕2 的「下一個模式」＝16-bit 合併 id + step（環繞）。

    高 byte = 0 代表「沒同步過／系統模式」（協議規定 Master 不可 MODE_SET type 0）
    → 退到 STEP_BASE_IF_UNKNOWN 當基準（預設＝可動模式），避免送出無效的 0x0001。
    """
    base = cur
    if (base >> 8) == 0 and STEP_BASE_IF_UNKNOWN is not None:
        base = STEP_BASE_IF_UNKNOWN
    return (base + step) & 0xFFFF


# ── 配對用（0x100D / 0x100E / 0x1002）──────────────────────────
#   ★ 全部自己手寫 struct：本檔不 import slave/lib，但也因此在離線測試裡
#     與權威 SchemaCodec 逐位元組比對（test/protocol/test_c6_remote.py §9）。

def decode_str_u16len(payload, off=0):
    """協議原生 `str_u16len`：2B LE 長度 + UTF-8 內容 → (str, 下一個 offset)。"""
    if len(payload) < off + 2:
        return "", off
    n = payload[off] | (payload[off + 1] << 8)
    raw = bytes(payload[off + 2:off + 2 + n])
    try:
        return raw.decode("utf-8"), off + 2 + n
    except Exception:                        # 不是合法 UTF-8 → 印 hex，不要吞掉
        return raw.hex(), off + 2 + n


def identify_req_payload(reply_cid=0xFFFF, timeout_ms=SCAN_WINDOW_MS, force=False):
    """0x100D IDENTIFY_REQ payload（5B）：reply_cid(u16) timeout_ms(u16) force(u8)。

    `reply_cid` = 這一封 0x100E 的**回信位址（NC4 幀頭 addr）**：
      0xFFFF → 對方把回信**廣播**出去（我們不用配對就收得到；對方只對同一個
               問的人回第一次，所以重掃要 force=1）
      具體   → 定向回覆（只有那個 cid 收），每次都回
    `timeout_ms` = 抖動視窗（對方隨機延遲 0~此值才回，避免 N 台同時回話碰撞）。
    """
    return struct.pack("<HHB", reply_cid & 0xFFFF, timeout_ms & 0xFFFF,
                       1 if force else 0)


def decode_identify_rsp(payload):
    """0x100E IDENTIFY_RSP：cid(u16) + slave_id(str_u16len) + ip(str_u16len)。

    `cid` = 對方的**協議**位址（→ PEER_CID）；`slave_id` = 對方的 MAC hex
    （歷史名，見協議文件定址詞彙表）。**射頻**目的地仍以收幀當下的來源 MAC 為準。
    """
    if len(payload) < 2:
        return None
    cid = payload[0] | (payload[1] << 8)
    sid, off = decode_str_u16len(payload, 2)
    ip, _ = decode_str_u16len(payload, off)
    return {"cid": cid, "slave_id": sid, "ip": ip}


def decode_announce(payload):
    """0x1002 SLAVE_ANNOUNCE：slave_id(str_u16len) + pixel_count(u16) + hw_version(str_u16len)。

    ★ 公告**不含 cid** → 只靠公告無法配對（`0x100E` 才是 cid 的唯一來源）。
    """
    sid, off = decode_str_u16len(payload, 0)
    pc = 0
    if len(payload) >= off + 2:
        pc = payload[off] | (payload[off + 1] << 8)
    hw, _ = decode_str_u16len(payload, off + 2)
    return {"slave_id": sid, "pixel_count": pc, "hw_version": hw}


def _mac_str(mac):
    """6-byte bytes → "aa:bb:cc:dd:ee:ff"（人看的）。"""
    if not mac:
        return "?"
    return ":".join("{:02x}".format(b) for b in bytes(mac))


# ── 未填參數的哨兵處理 ＋ 參數驗證 ──────────────────────────────
#   ★ 為什麼需要：使用者參數預設是 None（**沒確認過就不要猜**），而 None 一旦流進
#     `& 0xFFFF` / `{:04X}` 就會直接丟 TypeError。其中一處還在**每一幀的接收路徑**上
#     —— 一個沒填的參數不該讓整台遙控器在 RX 上炸掉。
#   ★ 但「不炸」不等於「可以隨便接受」：舊版 `_cid_or_bcast()` 用 int(v) 硬吞，
#     結果 `"0x0002"`（字串）→ 0xFFFF、`65536` → 0x0000、`True` → 0x0001、
#     `1.5` → 0x0001，而且 `_cid_str()` 還把 0xFFFF **印出來騙人**。
#     → 改成「**嚴格解析**（認得的三種寫法才收）＋ **開機一次總檢查**（❌ 講清楚）」，
#       而不是在熱路徑上靜默轉換。

def _is_int(v):
    """真整數（bool 不算：`True` 是 int 的子類，但把它當 cid 一定不是你想要的）。"""
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_cid(v):
    """cid 的合法寫法 → `(值 | None, 錯誤訊息 | None)`。**不猜、不環繞**。

    合法三種：
      `None`           未設定（＝沒有這個概念，不是 0）
      `0..0xFFFF` 的 int
      `"0x1234"` / `"1234"` / `"0002"` 字串（`0x` 前綴＝十六進位，其餘＝十進位）
    其他（bool／浮點／超範圍／看不懂的字串）→ 回錯誤訊息，由呼叫端**印出來**，
    不要靜默當成 0xFFFF。
    """
    if v is None:
        return None, None
    if isinstance(v, bool):
        return None, "bool 不是 cid（True/False 想表達什麼？）"
    if _is_int(v):
        if 0 <= v <= 0xFFFF:
            return v, None
        return None, "超出 0..0xFFFF：{}".format(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            n = int(s, 16) if s[:2].lower() == "0x" else int(s, 10)
        except ValueError:
            return None, "看不懂的字串 {!r}（要 0..0xFFFF 的整數，可用 0x 前綴）".format(v)
        if 0 <= n <= 0xFFFF:
            return n, None
        return None, "超出 0..0xFFFF：{}".format(v)
    return None, "型別不對（{}）".format(type(v).__name__)


def _cid_or_bcast(v):
    """cid → 0..0xFFFF。**未設定或解析失敗**一律回 `0xFFFF`（＝只廣播）這個哨兵。

    解析失敗不是靜默：`_validate_config()` 會在開機時把 ❌ 印出來。
    """
    n, _err = _parse_cid(v)
    return ADDR_BROADCAST if n is None else n


def _cid_str(v):
    """cid → 顯示字串。**不騙人**：未設定印「未填」、有問題印 ❌ 加原值。"""
    n, err = _parse_cid(v)
    if err:
        return "❌{!r}".format(v)
    return "未填" if n is None else "0x{:04X}".format(n)


def _derive_my_cid(mac_bytes_6=None):
    """自己的 cid → `(值, 來源說明)`。**照專案規則，不猜**。

    規則（`slave/lib/sys/ConfigManager.py::ensure_cID`）：
      `System.cID` 空著 → 用 `machine.unique_id()` 的 hex **末 4 碼**，
      例：`e4b323f93200` → `"3200"` → `0x3200`。
    這裡多一個選擇：`MY_CID` 有填就覆寫（例如要跟另一台 master 區分）。
    """
    n, err = _parse_cid(MY_CID)
    if n is not None:
        return n, "MY_CID 指定"
    if err:
        return ADDR_BROADCAST, "MY_CID 看不懂：" + err
    if mac_bytes_6 is None:
        mac_bytes_6 = _MY_MAC
    if mac_bytes_6:
        try:
            return int(bytes(mac_bytes_6).hex().upper()[-4:], 16), "自己 MAC 末 4 碼"
        except Exception:
            pass
    return ADDR_BROADCAST, "取不到自己的 MAC"


def _addr_ok(addr):
    """這一幀是不是「給本機」的？＝ `ADDR_FILTER` 的判定（抽出來才測得到）。

    MY_CID 沒填時只收廣播 —— 這不會漏東西：沒有自己的 cid，對方也只能廣播回。
    """
    if not ADDR_FILTER:
        return True
    return addr == ADDR_BROADCAST or addr == _MY_CID16


def _validate_config():
    """檢查**所有**使用者參數 → `(errors, warnings)` 兩串人看的訊息。

    ★ 為什麼要有這一關：這些值多半在**熱路徑**（每一幀的 `resolve_target()`、
      payload 打包、按鈕中斷後的處理）才被用到。型別錯了會在最不方便的時刻爆，
      或更糟 —— 靜默變成別的行為（見檔頭 `_parse_cid` 的說明）。
      在開機一次講清楚，勝過在無顯示的裝置上猜半天。
    """
    errs, warns = [], []

    # ── 射頻 ──
    if not _is_int(CHANNEL) or not (1 <= CHANNEL <= 13):
        errs.append("CHANNEL={!r} 必須是 1..13 的整數（Wi-Fi 頻道）".format(CHANNEL))

    # ── 位址（三種 cid 寫法都要能解析）──
    for name, val in (("PEER_CID", PEER_CID), ("MY_CID", MY_CID)):
        _n, err = _parse_cid(val)
        if err:
            errs.append("{}={!r}：{}".format(name, val, err))
    if PEER_MAC not in (None, "") and mac_bytes(PEER_MAC) is None:
        errs.append("PEER_MAC={!r} 不是合法 MAC（要 12 個 hex，可含 : -）".format(PEER_MAC))

    # ── 模式 id 與步進 ──
    for name, val in (("MOVABLE_MODE", MOVABLE_MODE), ("BOOT_MODE", BOOT_MODE)):
        if not _is_int(val) or not (0 <= val <= 0xFFFF):
            errs.append("{}={!r} 必須是 0..0xFFFF 的整數（16-bit 合併 id）".format(name, val))
    if STEP_BASE_IF_UNKNOWN is not None and (
            not _is_int(STEP_BASE_IF_UNKNOWN) or not (0 <= STEP_BASE_IF_UNKNOWN <= 0xFFFF)):
        errs.append("STEP_BASE_IF_UNKNOWN={!r} 必須是 0..0xFFFF 或 None".format(STEP_BASE_IF_UNKNOWN))
    if not _is_int(STEP) or not (0 <= STEP <= 0xFFFF):
        errs.append("STEP={!r} 必須是 0..0xFFFF 的整數".format(STEP))
    elif STEP == 0:
        warns.append("STEP=0 → 按鈕2 永遠送同一個模式")

    # ── payload 參數（打到對方身上的）──
    if not _is_int(BRIGHTNESS) or not (0 <= BRIGHTNESS <= 255):
        errs.append("BRIGHTNESS={!r} 必須是 0..255（255=不改；0 是合法亮度）".format(BRIGHTNESS))
    if not _is_int(START_DELAY_MS) or not (0 <= START_DELAY_MS <= 0xFFFF):
        errs.append("START_DELAY_MS={!r} 必須是 0..65535".format(START_DELAY_MS))

    # ── 時序 ──
    for name, val in (("RSP_TIMEOUT_MS", RSP_TIMEOUT_MS), ("CONFIRM_DELAY_MS", CONFIRM_DELAY_MS),
                      ("SCAN_WINDOW_MS", SCAN_WINDOW_MS), ("DEBOUNCE_MS", DEBOUNCE_MS)):
        if not _is_int(val) or val < 0:
            errs.append("{}={!r} 必須是 >=0 的整數（ms）".format(name, val))
    if not _is_int(RETRIES) or RETRIES < 1:
        errs.append("RETRIES={!r} 必須是 >=1 的整數（0 就一次都不送）".format(RETRIES))
    if BTN_LONG_PRESS_MS is not None and (
            not _is_int(BTN_LONG_PRESS_MS) or BTN_LONG_PRESS_MS <= 0):
        errs.append("BTN_LONG_PRESS_MS={!r} 必須是 >0 的整數或 None（停用）".format(BTN_LONG_PRESS_MS))

    # ── 腳位 ──
    for name, val in (("BTN_MOVABLE_PIN", BTN_MOVABLE_PIN), ("BTN_NEXT_PIN", BTN_NEXT_PIN)):
        if not _is_int(val) or val < 0:
            errs.append("{}={!r} 必須是 GPIO 編號（>=0 的整數）".format(name, val))
    if BTN_MOVABLE_PIN == BTN_NEXT_PIN:
        errs.append("BTN_MOVABLE_PIN 與 BTN_NEXT_PIN 都是 {} → 一顆按鈕會同時觸發兩件事"
                    .format(BTN_MOVABLE_PIN))
    if LED_PIN is not None and (not _is_int(LED_PIN) or LED_PIN < 0):
        errs.append("LED_PIN={!r} 必須是 GPIO 編號或 None".format(LED_PIN))

    # ── 列舉與布林（字串 "False" 是**真**的，這種錯最難查）──
    if SEND_TARGET not in _SEND_TARGETS:
        errs.append("SEND_TARGET={!r} 不認得（可用：{}）".format(
            SEND_TARGET, " / ".join(_SEND_TARGETS)))
    if LED_TYPE not in _LED_TYPES:
        errs.append("LED_TYPE={!r} 不認得（可用：{}）".format(
            LED_TYPE, " / ".join(_LED_TYPES)))
    for name, val in (("PAIR_AS_MASTER", PAIR_AS_MASTER),
                      ("SCAN_REPLY_DIRECTED", SCAN_REPLY_DIRECTED),
                      ("SCAN_FORCE", SCAN_FORCE), ("ADDR_FILTER", ADDR_FILTER),
                      ("DEBUG", DEBUG), ("LED_SHOW_MODE", LED_SHOW_MODE),
                      ("CONFIRM_AFTER_SET", CONFIRM_AFTER_SET),
                      ("BTN_ACTIVE_LOW", BTN_ACTIVE_LOW)):
        if not isinstance(val, bool):
            errs.append('{}={!r} 必須是 True/False（字串 "False" 是**真**的，會靜默生效）'
                        .format(name, val))

    # ── 交叉檢查（型別都對，但組合起來是空轉）──
    # MY_CID=None 是**正常**的（＝照專案規則由自己的 MAC 推導），不是缺漏。
    if PEER_MAC not in (None, "") and mac_bytes(PEER_MAC) is not None \
            and _parse_cid(PEER_CID)[0] is None:
        warns.append("PEER_MAC 填了但 PEER_CID 沒填 → 還是廣播（單播要兩層齊備）")
    if SEND_TARGET == "all" and PEER_MAC not in (None, ""):
        warns.append('SEND_TARGET="all"：就算填了位址也會對全部講')
    return errs, warns


def _report_config():
    """開機印一次參數檢查。回 True/False（有 ❌ 就不該繼續跑）。"""
    errs, warns = _validate_config()
    for w in warns:
        print("⚠️ [CFG] " + w)
    for e in errs:
        print("❌ [CFG] " + e)
    if errs:
        print("❌ [CFG] 共 {} 個參數有問題 → 修好再上電"
              "（本檔不會拿猜的值繼續跑）".format(len(errs)))
        return False
    return True


# ══════════════════════════════════════════════════════════════════
#  ③ 硬體：按鈕（去抖）與 LED（可選）
# ══════════════════════════════════════════════════════════════════

class Button:
    """一顆接 GND 的按鈕：內部上拉、按下＝低電位、軟體去抖，支援短按／長按。

    ★ 事件用**計數器**排隊（pending / long_pending），不是「當下讀電位」——
      因為等回覆（最多 RETRIES×RSP_TIMEOUT_MS）期間主迴圈不在跑，
      只記電位會漏掉「按一下又放開」。只要在等待迴圈裡呼叫 poll() 就不會漏。

    ★ 短按＝**放開時**才成立（才能區分長按）；長按＝按住達標**立刻**觸發，
      並且吃掉那一次短按 → 同一顆鈕不會又掃描又切模式。
    """

    def __init__(self, pin_no, active_low=True, debounce_ms=DEBOUNCE_MS, long_ms=None):
        self.active_low = bool(active_low)
        pull = machine.Pin.PULL_UP if self.active_low else machine.Pin.PULL_DOWN
        self.pin = machine.Pin(pin_no, machine.Pin.IN, pull)
        self.db = debounce_ms
        self.long_ms = long_ms
        self.raw = self._read()
        self.stable = self.raw
        self.t_chg = _ticks_ms()
        self.pending = 0                         # 短按事件數
        self.long_pending = 0                    # 長按事件數
        self.t_down = 0
        self.held = False
        self.long_fired = False

    def _read(self):
        v = self.pin.value()
        return (v == 0) if self.active_low else (v == 1)

    def poll(self):
        r = self._read()
        now = _ticks_ms()
        if r != self.raw:                        # 還在跳 → 重新計時
            self.raw = r
            self.t_chg = now
        elif r != self.stable and _ticks_diff(now, self.t_chg) >= self.db:
            self.stable = r                      # 去抖後的穩定邊緣
            if r:
                self.t_down = now
                self.held = True
                self.long_fired = False
            else:
                self.held = False
                if not self.long_fired:
                    self.pending += 1            # 短按＝放開且沒觸發過長按
        if (self.long_ms and self.held and not self.long_fired
                and _ticks_diff(now, self.t_down) >= self.long_ms):
            self.long_fired = True
            self.long_pending += 1               # 長按（不必等放開）

    def pressed(self):
        """取一次「短按」事件（沒有回 False）。"""
        self.poll()
        if self.pending:
            self.pending -= 1
            return True
        return False

    def long_pressed(self):
        """取一次「長按」事件（沒有回 False）。"""
        self.poll()
        if self.long_pending:
            self.long_pending -= 1
            return True
        return False


class Led:
    """極簡回饋燈。LED_PIN=None 時全部變成 no-op（不會出錯）。

    語意：tx()=發射、ok()=收到回覆、err()=逾時/失敗、show_mode()=閃出模式編號。
    `led_type` 是「燈的電氣型態」**唯一**的參數（見檔頭 LED_TYPE）——
    不會出現「選了 ws2812 又設低電位亮」這種自相矛盾卻靜默生效的組合。
    """

    _TYPES = _LED_TYPES

    def __init__(self, pin_no, led_type="gpio"):
        self.ok_hw = False
        self.np = None
        self.pin = None
        self.active_low = False
        if pin_no is None or machine is None:
            return
        if led_type not in self._TYPES:
            print("❌ LED_TYPE={!r} 不認得（可用：{}）→ 當作 \"gpio\"".format(
                led_type, " / ".join(self._TYPES)))
            led_type = "gpio"
        try:
            if led_type == "ws2812":
                import neopixel
                self.np = neopixel.NeoPixel(machine.Pin(pin_no), 1)
            else:
                self.pin = machine.Pin(pin_no, machine.Pin.OUT)
                self.active_low = (led_type == "gpio_low")
                self._gpio(False)
            self.ok_hw = True
        except Exception as ex:
            print("[LED] 初始化失敗（忽略）：{}".format(ex))

    def _gpio(self, on):
        if self.pin is not None:
            self.pin.value((0 if on else 1) if self.active_low else (1 if on else 0))

    def set_rgb(self, r, g, b):
        if self.np is not None:
            self.np[0] = (r, g, b)
            self.np.write()
        elif self.pin is not None:
            self._gpio(r + g + b > 0)

    def blink(self, n, on_ms=40, off_ms=60, rgb=(255, 255, 255), gap_ms=0):
        if not self.ok_hw:
            return
        for i in range(n):
            self.set_rgb(*rgb)
            _sleep_ms(on_ms)
            self.set_rgb(0, 0, 0)
            _sleep_ms(off_ms if i < n - 1 else gap_ms)
            _poll_buttons()                  # 閃燈期間不漏按鍵

    def tx(self):
        self.blink(1, 15, 0, (0, 0, 255))

    def ok(self):
        self.blink(2, 30, 60, (0, 255, 0))

    def err(self):
        self.blink(5, 30, 60, (255, 0, 0))

    def show_mode(self, mode16):
        """閃出模式：先 mode_type 次（藍），停一拍，再 mode_id+1 次（白）。

        0x0200 → 藍×2、停、白×1。超過 MAX_BLINK 次時改用連續快閃表示「很大」。
        """
        if not self.ok_hw or not LED_SHOW_MODE:
            return
        MAX_BLINK = 12
        mt = (mode16 >> 8) & 0xFF
        mi = mode16 & 0xFF
        _sleep_ms(200)
        if mt > MAX_BLINK or (mi + 1) > MAX_BLINK:
            self.blink(6, 25, 25, (255, 128, 0))
            return
        self.blink(mt, 90, 150, (0, 0, 255), gap_ms=500)
        self.blink(mi + 1, 90, 150, (255, 255, 255), gap_ms=0)


# ══════════════════════════════════════════════════════════════════
#  ④ 射頻（ESP-NOW）與收發
# ══════════════════════════════════════════════════════════════════

_RADIO = None
_RX = FrameRx()
_BTNS = []
_LED = Led(None)        # 空物件（不點燈）：REPL 直接呼叫 scan()/do_set_mode() 也不會炸
_PEER_MAC_B = None
_PEER_MAC_ERR = "沒填"   # PEER_MAC 不可用的原因（radio_init 更新）；"" = 可用
_MY_MAC = None
_MY_CID16 = ADDR_BROADCAST   # 解析後的自己的 cid（radio_init 決定；未決定前＝只收廣播）
_MODE = BOOT_MODE
_HELLO_DONE = False     # 「破冰」（SEND_TARGET="first" 的第一幀）做過了沒
_STATS = {"loop_err": 0, "rx_err": 0}   # 被護欄吞掉的例外數（REPL 打 _STATS 可查）
_LAST_PROBE = 0         # 上次背景探測的 ticks_ms
_PEER_RESPONDS = None   # 對端最近一輪有沒有回覆：None=未知 / True / False
                        #   → False 時每次 GET 只試 1 次（不要為了一個已知不回話的
                        #     對端，每按一次鈕就卡 3×RSP_TIMEOUT_MS）


def mac_bytes(s):
    """hex 字串 → 6-byte bytes。認不出來回 None。

    ★ MicroPython 的 espnow **只吃 6-byte bytes**：
        add_peer("aabbccddeeff") / add_peer("aa:bb:..") → invalid buffer length
      這是最常見的「看起來設好了、其實從來沒送出去」的坑。
    """
    if isinstance(s, (bytes, bytearray)):
        return bytes(s) if len(s) == 6 else None
    if not isinstance(s, str):
        return None
    t = s.replace(":", "").replace("-", "").replace(" ", "").strip()
    if len(t) != 12:
        return None
    try:
        return binascii.unhexlify(t)
    except Exception:
        return None


def radio_init():
    """STA 開起來 + 設頻道 + ESPNow active + 註冊 peer。回 True/False。

    ★ ESP-NOW 綁的是 **STA** 介面（不是 AP）。沒開 STA 時 add_peer/send 都可能
      「成功」但其實送不出去 —— 典型的靜默失敗。
    """
    global _RADIO, _PEER_MAC_B, _PEER_MAC_ERR, _MY_MAC
    if espnow is None or network is None:
        print("❌ 這個固件沒有 espnow/network 模組（請換含 ESP-NOW 的 MicroPython 版本）")
        return False
    try:
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        _sleep_ms(100)
        try:
            sta.config(channel=CHANNEL)
        except Exception as ex:
            print("⚠️ 設定頻道失敗（{}）；若對方同頻道仍可運作".format(ex))
        _sleep_ms(50)

        e = espnow.ESPNow()
        e.active(True)
        e.add_peer(BCAST_MAC)                       # 收廣播必須註冊廣播位址
        try:                                        # ★ 先讀自己的 MAC：下面要用它擋「填到自己」
            _MY_MAC = bytes(sta.config("mac"))
        except Exception:
            _MY_MAC = None

        # ── 自己的 cid（master 的 ID）：照專案規則由 MAC 推導，不猜 ──────
        global _MY_CID16
        _MY_CID16, _cid_src = _derive_my_cid(_MY_MAC)
        print("ℹ️  自己的 cid = 0x{:04X}（{}）".format(_MY_CID16, _cid_src))

        # ── PEER_MAC 的四種情況（單一鏈，最後只有「可用」才 add_peer）────────
        _PEER_MAC_B = mac_bytes(PEER_MAC)
        if _PEER_MAC_B is None:
            if PEER_MAC in (None, ""):
                _PEER_MAC_ERR = "沒填"
                print("ℹ️  PEER_MAC 沒填 → 走廣播（先 scan() 找對方，再填進來改單播）")
            else:
                _PEER_MAC_ERR = "格式錯"
                print("⚠️ PEER_MAC 格式不合法：{!r} → 退回廣播"
                      "（不會靜默單播到不存在的位址）".format(PEER_MAC))
        elif _PEER_MAC_B == BCAST_MAC:
            _PEER_MAC_ERR = ""
            print("ℹ️  PEER_MAC = 廣播位址 → 走廣播")
        elif _MY_MAC and _PEER_MAC_B == _MY_MAC:
            # ★ 把自己的 MAC 填進 PEER_MAC 是很容易犯的錯（兩個 MAC 都印在 log 上）：
            #   單播打到自己 → 自己的幀又被自己收到 → log 一團亂、對方永遠沒收到。
            #   這是不可能正確的設定，所以直接退回廣播並講清楚。
            _PEER_MAC_ERR = "是自己的 MAC"
            print("❌ PEER_MAC = 你自己的 MAC（{}）—— 那是我方，不是對方！"
                  "→ 退回廣播（請填掃描到的『對方』MAC）".format(_mac_str(_MY_MAC)))
            _PEER_MAC_B = None
        else:
            _PEER_MAC_ERR = ""
            e.add_peer(_PEER_MAC_B)                 # 單播前必須先把對方加進 peer 表

        _pcid = _parse_cid(PEER_CID)[0]
        if _PEER_MAC_B is not None and _PEER_MAC_B != BCAST_MAC:
            # ★ 交叉檢查：cid 通常是 MAC 末四碼（ensure_cID）。不同不一定是錯
            #   （對方可能設過 System.cID），但**填錯時完全沒症狀**，所以印出來對一眼。
            try:
                _suffix = int(_PEER_MAC_B.hex().upper()[-4:], 16)
            except Exception:
                _suffix = None
            if _pcid is None:
                print("ℹ️  peer = {} ／ cid 未填 → 只能廣播".format(_mac_str(_PEER_MAC_B)))
            elif _suffix == _pcid:
                print("ℹ️  peer = {} ／ cid 0x{:04X}（＝MAC 末四碼 ✓）".format(
                    _mac_str(_PEER_MAC_B), _pcid))
            else:
                print("ℹ️  peer = {} ／ cid 0x{:04X}（MAC 末四碼是 0x{:04X}）"
                      "—— 不同不一定是錯（對方設過 System.cID），但請確認沒填錯".format(
                          _mac_str(_PEER_MAC_B), _pcid, _suffix or 0))
        _RADIO = e
        try:
            ch_now = sta.config("channel")
        except Exception:
            ch_now = CHANNEL
        _dst, _addr, _why = resolve_target()
        print("✅ ESP-NOW active  my_mac={}  ch={}".format(
            _mac_str(_MY_MAC) if _MY_MAC else "?", ch_now))
        print("   送出去：{}   dst={}  addr=0x{:04X}   （{}{}）".format(
            "單播" if _why == "單播" else "廣播",
            _dst.hex(), _addr, _why,
            "，PEER_CID=0x{:04X}".format(PEER_CID) if _why == "單播" else ""))
        return True
    except Exception as ex:
        print("❌ ESP-NOW 初始化失敗：{}".format(ex))
        return False


def _send_target():
    """`SEND_TARGET` 的正規化：不認得的值 → `"first"`（最保守：先廣播一次，再照對象走）。

    為什麼要正規化而不是讓它自然失敗：打錯字（例如 "First"）不該讓遙控器
    安靜地變成別的行為 —— 由 `radio_init()` 印 ❌ 講清楚，行為則退回最保守的那個。
    """
    t = SEND_TARGET if isinstance(SEND_TARGET, str) else None
    return t if t in _SEND_TARGETS else "first"


def resolve_target(force_broadcast=False, ignore_hello=False, force_unicast=False):
    """決定「這一幀走哪裡」→ `(dst_mac, addr16, why)`。

    ★ 這是**唯一**決定廣播/單播的地方（協議 addr 與射頻 dst 一起決定，兩層不可分開想）。
      判斷順序（任一成立就是廣播）：

        1. `force_broadcast=True`      呼叫端要求（點名、破冰這種「本來就對全部講」的）
        2. `SEND_TARGET = "all"`       使用者要對全部講
        3. `SEND_TARGET = "first"`     還沒破冰 → 第一幀先廣播（讓對方學到我們的 MAC）
        4. `PEER_MAC` 沒填/格式錯      **沒對象就只能廣播** —— 防「靜默單播到不存在的
                                       位址」（那種失敗完全沒有症狀）
        5. `PEER_MAC = ffffffffffff`   明寫廣播
        6. `PEER_CID` 沒填/看不懂     單播要「射頻 MAC ＋ 協議 cid」兩層齊備

      其餘才是單播：射頻 dst = `PEER_MAC`、幀頭 addr = `PEER_CID`。
      ★ 沒有「單播/廣播開關」：有沒有對象由**資料**回答（見檔頭 SEND_TARGET 的說明）。
    """
    mb = _PEER_MAC_B
    if force_broadcast:
        return BCAST_MAC, ADDR_BROADCAST, "強制廣播"
    t = _send_target()
    if not force_unicast:                       # 綁定（0x1016）這類指令必須單播
        if t == "all":
            return BCAST_MAC, ADDR_BROADCAST, 'SEND_TARGET="all"'
        if t == "first" and not _HELLO_DONE and not ignore_hello:
            return BCAST_MAC, ADDR_BROADCAST, "破冰：開機第一幀"
    if mb is None:
        # 原因用 radio_init 記下來的（沒填／格式錯／是自己的 MAC）——同樣是廣播，
        # 但修法完全不同，所以不要從常數猜。
        return BCAST_MAC, ADDR_BROADCAST, "PEER_MAC " + (_PEER_MAC_ERR or "不可用")
    if mb == BCAST_MAC:
        return BCAST_MAC, ADDR_BROADCAST, "PEER_MAC=廣播位址"
    cid, cid_err = _parse_cid(PEER_CID)
    if cid is None:
        return BCAST_MAC, ADDR_BROADCAST, ("PEER_CID " +
                                          ("看不懂" if cid_err else "沒填"))
    return mb, cid, "單播"


def tx(cmd, payload=b"", force_broadcast=False, force_unicast=False, quiet=False):
    """送一幀（協議層 addr 與射頻層目的地由 `resolve_target()` 一起決定）。回 True/False。"""
    global _HELLO_DONE
    if _RADIO is None:
        return False
    dst, addr, why = resolve_target(force_broadcast, force_unicast=force_unicast)
    frame = nc4_pack(cmd, payload, addr)
    try:
        ok = _RADIO.send(dst, frame)
    except Exception as ex:
        ok = False
        if DEBUG:
            print("[TX] 例外：{}".format(ex))
    if ok and not force_broadcast:
        _HELLO_DONE = True      # 破冰只做一次（強制廣播的點名不算，見 scan()）
    if DEBUG and not quiet:
        print("[TX] cmd=0x{:04X} addr=0x{:04X} dst={} len={} {} ({})".format(
            cmd, addr, dst.hex(), len(frame), "ok" if ok else "FAIL", why))
    return bool(ok)


def _poll_buttons():
    for b in _BTNS:
        b.poll()


def _log_frame(f):
    ver, addr, cmd, payload, src = f
    if DEBUG:
        print("[RX] cmd=0x{:04X} addr=0x{:04X} src={} len={} {}".format(
            cmd, addr, _mac_str(src), len(payload), payload.hex()))


def pump(ms):
    """收幀（最多等 ms）→ list of (ver, addr, cmd, payload, src_mac)。

    ★ src_mac 是**射頻層**的來源（「這一幀誰講的」），由射頻附帶、不用對方講
      —— ESP-NOW 的位址無法枚舉，所以這是唯一能知道對方位址的時機。
    ★ 等待期間仍呼叫 _poll_buttons()：按鍵不會因為我們在等回覆而漏掉。
    """
    out = []
    if _RADIO is None:
        _sleep_ms(ms)
        return out
    deadline = _ticks_add(_ticks_ms(), ms)
    while True:
        try:
            peer, data = _RADIO.recv(0)          # 非阻塞
        except Exception:
            peer, data = None, None
        if peer is not None and data:
            _RX.feed(data)
            while True:
                # ★ 一幀的處理出錯不該讓整個接收路徑停擺（無顯示的裝置最怕這個）：
                #   吞掉、計數、印一次，然後跳過這一幀繼續收。
                try:
                    f = _RX.pop()
                    if f is None:
                        break
                    f = (f[0], f[1], f[2], f[3], bytes(peer))
                    if not _addr_ok(f[1]):       # MY_CID 沒填時只收廣播（見 _addr_ok）
                        if DEBUG:
                            print("[RX] 丟棄（addr 不是本機）：0x{:04X}".format(f[1]))
                        continue
                    _log_frame(f)
                    out.append(f)
                except Exception as ex:
                    _STATS["rx_err"] += 1
                    if _STATS["rx_err"] <= 3:    # 只印前 3 次，避免壞資料洗版
                        print("❌ [RX] 這一幀處理失敗（已跳過）：{!r}".format(ex))
                    break
        _poll_buttons()
        if _ticks_diff(_ticks_ms(), deadline) >= 0:
            break
        _sleep_ms(2)
    return out


def flush_rx():
    """把已經堆在射頻層的舊幀收乾淨（避免把「上一輪的遲到回覆」當成這一輪的答案）。"""
    pump(0)


def wait_rsp(cmd, timeout_ms=RSP_TIMEOUT_MS):
    """等一個指定 cmd 的回覆 → payload 或 None（其他幀只記錄、丟棄）。"""
    deadline = _ticks_add(_ticks_ms(), timeout_ms)
    while True:
        for f in pump(5):
            if f[2] == cmd:
                return f[3]
        if _ticks_diff(_ticks_ms(), deadline) >= 0:
            return None


def request_mode_get(force_broadcast=False, quiet=False):
    """0x3103 MODE_GET → 等 0x3104。回 dict 或 None。

    ★ 重試次數是**自適應**的：對端上一次整輪都沒回覆（`_PEER_RESPONDS is False`）
      → 這次只試 1 次。理由：已知不回話的對端，重試只是讓按鈕多卡 600ms。
      它一開口（收到任何回覆）就自動恢復 RETRIES 次。
    """
    global _PEER_RESPONDS
    tries = 1 if _PEER_RESPONDS is False else max(1, RETRIES)
    for i in range(tries):
        flush_rx()
        tx(CMD_MODE_GET, b"", force_broadcast=force_broadcast, quiet=quiet)
        p = wait_rsp(CMD_MODE_GET_RSP)
        if p is not None:
            info = decode_mode_rsp(p)
            if info:
                _PEER_RESPONDS = True
                if DEBUG:
                    print("[GET] 模式=0x{:04X} (type={} id={}) elapsed={}ms total={}ms running={}".format(
                        info["mode"], info["mode_type"], info["mode_id"],
                        info["elapsed_ms"], info["total_ms"], info["running"]))
                return info
            if DEBUG:
                print("[GET] 回覆長度不足（{}B），忽略".format(len(p)))
        if DEBUG and not quiet:
            print("[GET] 第 {}/{} 次沒有回覆（這是 0x3103 查詢；MODE_SET 本來就無 ACK）".format(
                i + 1, tries))
    if _PEER_RESPONDS is not False:
        print("[GET] 查詢沒有回覆 → 之後每次只試 1 次查詢（它一開口就自動恢復 {} 次）".format(
            RETRIES))
    _PEER_RESPONDS = False
    return None


def do_set_mode(mode16, tag=""):
    """0x3105 MODE_SET → （可選）再 GET 確認。確認失敗**不覆蓋**本地值。

    「本地值」就是後備方案：GET 拿不到時，下一次 +1 仍從這裡繼續，
    不會因為一次掉幀就跳回 0。
    """
    global _MODE
    _MODE = mode16
    tx(CMD_MODE_SET, mode_set_payload(mode16))
    # 對端已知不回覆時，不要在按鍵路徑上等確認 → 併成同一行輸出（按一次鍵 = 一行）
    _skip = CONFIRM_AFTER_SET and _PEER_RESPONDS is False
    print("[SET] {}0x{:04X} (type={} id={} bri={} delay={}ms){}".format(
        (tag + " ") if tag else "", mode16, (mode16 >> 8) & 0xFF, mode16 & 0xFF,
        BRIGHTNESS, START_DELAY_MS,
        "（已知查詢不回覆→不確認）" if _skip else ""))
    if CONFIRM_AFTER_SET and _PEER_RESPONDS is not False:
        _sleep_ms(CONFIRM_DELAY_MS)
        info = request_mode_get()
        if info is not None:
            _MODE = info["mode"]
            if _MODE == mode16:
                print("[SET] ✅ 確認成功：對方目前模式 0x{:04X}".format(_MODE))
                _LED.ok()
            else:
                print("[SET] ⚠️ 對方回報 0x{:04X}（不是 0x{:04X}）→ 以對方為準".format(_MODE, mode16))
                _LED.err()
        else:
            print("[SET] ⚠️ 確認查詢逾時（0x3103 無回覆）→ 本地保留 0x{:04X}"
                  "（16-bit +1 後備）".format(_MODE))
            _LED.err()
    else:
        _LED.tx()
    _LED.show_mode(_MODE)


# ══════════════════════════════════════════════════════════════════
#  ④·5 手動探索／配對（**只印出來，不自動套用**）
# ══════════════════════════════════════════════════════════════════
#  設計原則（與 todo/05 / doc/02_guides/18_node_discovery.md 一致）：
#    · 身分 ≠ 方向 —— 「你是誰」(0x100D/0x100E) 與「我以後聽誰的」(0x1016) 分開，
#      掃描**不改任何人的方向**，本檔也**不自動送 0x1016**（PAIR_AS_MASTER 預設關）。
#    · 探索只負責「看見」，配對的動作是「把印出來的兩行貼回檔頭」——由人決定。
#    · ESP-NOW 的位址是 MAC、無法枚舉 → 能知道對方位址的時機只有兩個：
#        ① 它主動講話（公告／心跳／任何幀）→ src_mac（listen）
#        ② 我們點名、它回話（0x100D → 0x100E）→ src_mac + cid（scan）

def _report_frame(f):
    """把「主動進來」的幀印成人看得懂的一行。回傳解出的 dict 或 None。"""
    ver, addr, cmd, payload, src = f
    if cmd == CMD_IDENTIFY_RSP:
        info = decode_identify_rsp(payload)
        if info:
            print("  [0x100E] cid=0x{:04X}  slave_id={}  ip={}  ← src={}".format(
                info["cid"], info["slave_id"] or "-", info["ip"] or "-", _mac_str(src)))
            return info
    elif cmd == CMD_SLAVE_ANNOUNCE:
        info = decode_announce(payload)
        print("  [0x1002] 公告 slave_id={} pixels={} hw={}  ← src={}"
              "（公告不含 cid，配對要靠掃描）".format(
                  info["slave_id"] or "-", info["pixel_count"],
                  info["hw_version"] or "-", _mac_str(src)))
        return info
    return None


def _print_found(found):
    """把掃到的節點印成**可以直接複製**的兩行。"""
    print("─" * 64)
    if not found:
        print("[SCAN] 沒有任何回覆。依序檢查：")
        print("       1) 頻道是否相同（本機 ch={}，對方 config 的 Network.ESP_now.channel）".format(CHANNEL))
        print("       2) 對方有沒有實作 0x100D（若是另一套系統，可能只發公告不回點名）")
        print("       3) 距離／天線；單播不是問題（點名是廣播出去的）")
        print("       → 改用 listen(15)：純被動監聽，只要對方有在講話就看得到它的 MAC")
        print("─" * 64)
        return
    print("[SCAN] 共 {} 台。要單播就複製下面兩行回 main.py『① 使用者參數』"
          "（本檔不會自動套用）：".format(len(found)))
    for i, (mac, info) in enumerate(found.items(), 1):
        m = bytes(mac).hex()
        print("  #{}  src_mac={}  cid=0x{:04X}  slave_id={}  ip={}".format(
            i, _mac_str(mac), info["cid"], info["slave_id"] or "-", info["ip"] or "-"))
        print("        PEER_MAC = \"{}\"".format(m))
        print("        PEER_CID = 0x{:04X}".format(info["cid"]))
        if info["slave_id"] and info["slave_id"].lower() != m:
            print("        ⚠️ slave_id({}) 與射頻來源 MAC 不同 → PEER_MAC 請用射頻來源".format(
                info["slave_id"]))
    print("  ★ 把上面兩行填回檔頭就變單播 —— **沒有第二個開關要開**"
          "（不填就維持廣播：沒對象就對全部講）")
    print("  ★ 單播時對方也要認識我們，把本機 MAC 給對方：my_mac = {}".format(
        _mac_str(_MY_MAC) if _MY_MAC else "?"))
    print("  ★ 我的 cid = 0x{:04X}（{}）—— 綁定（pair()）時會用這個值；"
          "對方就是記這個來回信".format(_MY_CID16, _derive_my_cid()[1]))
    print("  ★ 本檔只印不套用；改完記得重新上傳 main.py 或按 reset")
    print("─" * 64)


def scan(window_ms=SCAN_WINDOW_MS, reply_cid=None, force=None, quiet=False):
    """手動探索：廣播 0x100D 點名 → 收集 0x100E → 印出可複製的 PEER_MAC / PEER_CID。

    ★ reply_cid 預設 0xFFFF（＝**廣播回覆**）：對方回的 0x100E 在射頻層也是廣播，
      所以我們**還沒跟任何人配對也收得到**（只需 add_peer(BCAST)，開機就做了）。
      這是 ESP-NOW 上唯一不需要先互相認識就能完成的探索路徑。
      `SCAN_REPLY_DIRECTED=True` 才改成用 `MY_CID` 定向回覆 —— 只有我們收得到，
      但需要對方已知道我們的 MAC，而且我們要收得到單播（見 README §7）。
    ★ 不改任何設定、不送 0x1016：掃描是「問你是誰」，不是「認主人」。
    """
    if _RADIO is None:
        print("[SCAN] 射頻沒起來（radio_init 失敗？）")
        return []
    if window_ms is None:
        window_ms = SCAN_WINDOW_MS
    if reply_cid is None:
        if SCAN_REPLY_DIRECTED:
            reply_cid = _MY_CID16                   # 定向：回信只寄給我們（自己的 cid）
            if reply_cid == ADDR_BROADCAST:
                print("⚠️ SCAN_REPLY_DIRECTED=True 但自己的 cid 取不到 → 改用廣播回覆")
        else:
            reply_cid = ADDR_BROADCAST              # 廣播回覆：不需配對就收得到（預設）
    if force is None:
        force = SCAN_FORCE
    flush_rx()
    print("[SCAN] 廣播 0x100D IDENTIFY_REQ{{reply_cid=0x{:04X}, window={}ms, force={}}} …".format(
        reply_cid, window_ms, 1 if force else 0))
    if _LED and not quiet:
        _LED.blink(2, 30, 60, (0, 255, 255))     # 青閃×2 = 開始掃描（無顯示時的提示）
    # ★ 點名**一律廣播**（force_broadcast）：它是「問全部『你是誰』」，
    #   不是「問我認識的那台還在不在」。少了這個，配對之後掃描會變成單播 →
    #   其他節點根本沒收到（addr 不是它們），等於再也掃不到新節點。
    tx(CMD_IDENTIFY_REQ, identify_req_payload(reply_cid, window_ms, force),
       force_broadcast=True)
    found = {}
    # 對方最晚在 window_ms 才回 → 再多等 600ms 收尾（空中排隊 + 抖動）
    deadline = _ticks_add(_ticks_ms(), window_ms + 600)
    while _ticks_diff(_ticks_ms(), deadline) < 0:
        for f in pump(20):
            src = f[4]
            info = _report_frame(f)
            if info is not None and f[2] == CMD_IDENTIFY_RSP and src:
                found[bytes(src)] = info
    if quiet:
        # 開機破冰用：只印一行，不要整段「可複製位址」
        if found:
            print("[SCAN] 點名 → {} 台：{}".format(len(found), "，".join(
                "{} cid=0x{:04X}".format(_mac_str(m), i["cid"]) for m, i in found.items())))
        else:
            print("[SCAN] 點名 → 沒有回應")
    else:
        _print_found(found)
    if _LED and not quiet:
        if found:                                # 綠閃×N = 掃到 N 台（最多 5 下）
            _LED.blink(min(len(found), 5), 60, 120, (0, 255, 0))
        else:
            _LED.err()                           # 紅閃×5 = 沒人回
    return list(found.items())


def listen(sec=15):
    """純被動監聽：印出這段時間內**每一幀**的來源 MAC（不發任何東西）。

    用途：對方就算沒實作 0x100D（點名），只要它有在講話就看得到它的 MAC；
    這也是「對方其實有沒有在射頻上」最直接的證據。
    """
    print("[LISTEN] 被動監聽 {} 秒（Ctrl-C 可中斷）…".format(sec))
    seen = {}
    deadline = _ticks_add(_ticks_ms(), int(sec * 1000))
    while _ticks_diff(_ticks_ms(), deadline) < 0:
        for f in pump(50):
            src = f[4]
            _report_frame(f)
            if src:
                seen[bytes(src)] = seen.get(bytes(src), 0) + 1
    print("─" * 64)
    if not seen:
        print("[LISTEN] 一幀都沒收到 → 頻道不同／對方沒在發／距離太遠")
    else:
        print("[LISTEN] 看到 {} 台（幀數／來源）：".format(len(seen)))
        for mac, n in seen.items():
            print("  {:>4} 幀  src_mac={}   PEER_MAC = \"{}\"".format(n, _mac_str(mac), bytes(mac).hex()))
    print("─" * 64)
    return list(seen.items())


# ══════════════════════════════════════════════════════════════════
#  ④·6 查詢與綁定（專案流程：掃描 → **綁定** → 操作）
# ══════════════════════════════════════════════════════════════════
#  ★ 為什麼一定要綁定（0x1016）才收得到回覆：專案的 `_reply()` 把回應的 addr
#    預設填成 `bus.master_cid`（`slave/action/net_actions.py`）—— 也就是「寄給它
#    認定的 master」。**唯一例外是 0x100E**（用 reply_cid 明確指定），所以
#    「掃描得到、其他指令沒人回」正是「還沒綁定」的正常症狀。

def query_node(keys="node.master_cid"):
    """0x1101 STATUS_GET → 0x1102 STATUS_RSP：**用指令**讀對端的具名狀態。

    `node.master_cid` 就是「它現在認誰當 master」—— 這就是「用指令取得 master 的 ID」。
    `0x1016` 沒有 ACK，所以綁定到底成不成功，也只能靠這條查回來（專案文件 §8.3）。

    ★ 真機實測（2026-10）：這塊對端**只認得 `node.master_cid` 這一個 key**
      （`slave_id`、`?`、`System.cID`、逗號多 key 全部無回覆）→ 預設只問這一個。
      回覆是**廣播**（addr=0xFFFF），所以不需要綁定也收得到；
      但**請求**必須定址到它的 cid（`addr=PEER_CID`），填錯就靜默無回覆。
    """
    if _RADIO is None:
        print("[QUERY] 射頻沒起來")
        return None
    flush_rx()
    data = keys.encode() if hasattr(keys, "encode") else bytes(keys)
    payload = struct.pack("<H", len(data)) + data
    tx(0x1101, payload)
    p = wait_rsp(0x1102, RSP_TIMEOUT_MS * 2)
    if p is None:
        print("[QUERY] 沒有回覆 —— 若還沒綁定，這是**正常**的"
              "（對方的回信寄給它目前的 master，不是寄給我們）")
        return None
    txt, _ = decode_str_u16len(p, 0)
    print("[QUERY] {}".format(txt))
    return txt


def pair(verify=True):
    """0x1016 SET_MASTER：把**對方**的方向設成「回話回給我」＝專案流程的『綁定』。

    ★ 刻意**只做手動呼叫**（REPL 打 `pair()`）：它會改對方的持久狀態
      （btree `@node.master_cid`），而且會**搶走它現在的 master**。
    ★ 必須**單播**：廣播會讓射頻範圍內每一台都認你當 master
      （專案文件記載過的真實事故，`doc/02_guides/18_node_discovery.md` §3）。
    ★ 沒有 ACK：所以預設用 `query_node()` 查回來確認。
    """
    if _PEER_MAC_B is None or _PEER_MAC_B == BCAST_MAC:
        print("❌ 綁定必須單播：先填 PEER_MAC/PEER_CID（scan() 會把值印出來）")
        return False
    print("[PAIR] 0x1016 SET_MASTER{{master_cid=0x{:04X}}} → {}".format(
        _MY_CID16, _mac_str(_PEER_MAC_B)))
    tx(CMD_SET_MASTER, struct.pack("<H", _MY_CID16), force_unicast=True)
    if not verify:
        return True
    _sleep_ms(CONFIRM_DELAY_MS)
    got = query_node()
    if got is None:
        print("[PAIR] ⚠️ 查不到（可能被拒絕：對方本次開機已被別的 master 認領，"
              "或它沒有 0x1101）")
        return False
    print("[PAIR] ✅ 查得到回覆 → 方向已生效")
    return True


# ══════════════════════════════════════════════════════════════════
#  ⑤ 主流程
# ══════════════════════════════════════════════════════════════════

def on_button_movable():
    do_set_mode(MOVABLE_MODE, "可動模式")


def on_button_next():
    nxt = next_mode_of(_MODE)
    do_set_mode(nxt, "下一個")


def boot_sync():
    """開機同步：本地先預設 BOOT_MODE，再問一次對方現在的模式（**只此一次**）。"""
    global _MODE
    _MODE = BOOT_MODE
    print("[BOOT] 本地預設模式 = 0x{:04X}".format(_MODE))
    if PAIR_AS_MASTER:
        pair()          # 專案流程的『綁定』（單播 + 查回來確認）
    # ── 破冰：用 0x100D 點名（廣播；**這是對方一定會回的指令**）──────────
    #   舊版是「把第一條指令偷偷改成廣播」，但如果第一條是指令類（replies 需
    #   定址到它的 cid），那一條就註定失敗。改成明確的 hello：順便核對它的 cid。
    global _HELLO_DONE
    if _send_target() == "first" and not _HELLO_DONE and _PEER_MAC_B is not None \
            and _PEER_MAC_B != BCAST_MAC:
        found = scan(window_ms=HELLO_WINDOW_MS, force=True, quiet=True)
        _HELLO_DONE = True
        _expect = _parse_cid(PEER_CID)[0]
        _hit = [i for m, i in found if bytes(m) == _PEER_MAC_B]
        if _hit:
            if _expect is not None and _hit[0]["cid"] != _expect:
                print("⚠️ 對方的 cid 變了：檔頭 0x{:04X} → 實際 0x{:04X}"
                      "（請更新 PEER_CID）".format(_expect, _hit[0]["cid"]))
            else:
                print("[BOOT] 對方在線（{} cid 0x{:04X} ✓）".format(
                    _mac_str(_PEER_MAC_B), _hit[0]["cid"]))
        else:
            print("[BOOT] ⚠️ 點名沒有回應（對方沒開／頻道不同／射程外）")

    info = request_mode_get()
    if info is not None:
        _MODE = info["mode"]
        print("[BOOT] ✅ 取得對方目前模式 0x{:04X}".format(_MODE))
        _LED.ok()
    else:
        print("[BOOT] ⚠️ 模式查詢（0x3103）沒有回覆 → 沿用本地預設 0x{:04X}"
              "（之後 +1 以 {} 為基準）".format(
                  _MODE, "0x{:04X}".format(STEP_BASE_IF_UNKNOWN)
                  if STEP_BASE_IF_UNKNOWN is not None else "本地值"))
        _LED.err()
    _LED.show_mode(_MODE)


def _loop_step():
    """主迴圈的**一次**內容（探測 → 按鈕 → 收幀）。例外交給 loop() 的護欄。"""
    global _LAST_PROBE, _MODE
    if not _BTNS:
        _sleep_ms(10)
        return
    # ── 背景探測：對端已知沉默時，每隔 PEER_PROBE_MS 試一次它是否恢復 ──
    #   ★ 為什麼不放在按鍵路徑：按一下鈕要等 RSP_TIMEOUT_MS 才完成，手感很差；
    #     而「它有沒有回來」跟「你按了什麼」無關 → 放待機時做。
    if PEER_PROBE_MS and _PEER_RESPONDS is False \
            and _ticks_diff(_ticks_ms(), _LAST_PROBE) >= PEER_PROBE_MS:
        _LAST_PROBE = _ticks_ms()
        info = request_mode_get(quiet=True)     # 探測失敗不洗版，只有成功才出聲
        if info is not None:
            _MODE = info["mode"]
            print("[PROBE] 對端恢復回應 → 目前模式 0x{:04X}".format(_MODE))
            _LED.ok()
    if _BTNS[0].long_pressed():              # 長按＝掃描（同一次按壓不會又切模式）
        scan()
    elif _BTNS[0].pressed():                 # 短按＝進入可動模式
        on_button_movable()
    if len(_BTNS) > 1 and _BTNS[1].pressed():
        on_button_next()
    pump(10)                                 # 收下任何主動進來的幀（公告等）


def _guard(step):
    """跑一次 `step`，**任何例外都不讓它冒出去** → 回 True/False。

    ★ 為什麼要這個：這是**無顯示、無人看守**的裝置。一顆按鈕讀取失敗、
      一幀資料解錯、一次記憶體不足，都不該讓整台遙控器停在 traceback 上
      （使用者只會看到「按了沒反應」）。吞掉的例外計入 `_STATS["loop_err"]`，
      第一次會印完整 traceback（REPL 可查），之後只計數不洗版。
    """
    try:
        step()
        return True
    except Exception as ex:
        _STATS["loop_err"] += 1
        print("❌ [RUN] 主迴圈例外（已忽略並繼續）：{!r}".format(ex))
        if _STATS["loop_err"] == 1:
            try:
                sys.print_exception(ex)      # MicroPython 才有
            except Exception:
                pass
        _sleep_ms(50)                        # 別在錯誤裡空轉燒 CPU
        return False


def loop():
    print("[RUN] 待機中… 按鈕1=可動模式 0x{:04X}（長按 {}s=掃描） / 按鈕2=下一個(+{})".format(
        MOVABLE_MODE, (BTN_LONG_PRESS_MS or 0) / 1000.0, STEP))
    print("[RUN] REPL 也可直接打：scan() / listen(15) / pair()（綁定）/ "
          "query_node()（查對方認誰當 master）/ _MODE / _STATS")
    while True:
        _guard(_loop_step)


def main():
    global _LED, _BTNS
    print("=== 兩鍵遙控器（NC4 / ESP-NOW，無顯示）===")
    try:
        _u = os.uname()
        print("[SYS] {} / MicroPython {}".format(_u.machine, _u.release))
    except Exception:
        pass
    if not _report_config():        # ★ 參數先過一次總檢查，有 ❌ 就不跑
        return
    if not radio_init():
        return
    _LED = Led(LED_PIN, LED_TYPE)
    try:
        _BTNS = [Button(BTN_MOVABLE_PIN, BTN_ACTIVE_LOW, DEBOUNCE_MS, BTN_LONG_PRESS_MS),
                 Button(BTN_NEXT_PIN, BTN_ACTIVE_LOW, DEBOUNCE_MS, None)]
    except Exception as ex:
        print("❌ 按鈕初始化失敗：{}".format(ex))
        return
    # ignore_hello=True：顯示「穩定狀態」的原因（破冰只是第一幀的暫時策略）
    dst, _addr, why = resolve_target(ignore_hello=True)
    print("[CFG] 可動=GPIO{} / 下一個=GPIO{} / 去抖={}ms / my_cid=0x{:04X} / peer_cid={}".format(
        BTN_MOVABLE_PIN, BTN_NEXT_PIN, DEBOUNCE_MS, _MY_CID16, _cid_str(PEER_CID)))
    if why == "單播":
        print("[CFG] 目前是單播 → {}（PEER_MAC + PEER_CID 都齊了）".format(dst.hex()))
    else:
        print("[CFG] 目前是廣播 — {}".format(why))
    if why != "單播":
        print("[CFG] 廣播＝同頻道的每一台都會執行 MODE_SET。"
              "要只打一台 → 先 scan()，把印出的 PEER_MAC/PEER_CID 填回檔頭即可"
              "（沒有第二個開關要開）")
    boot_sync()
    loop()


if __name__ == "__main__":
    main()
