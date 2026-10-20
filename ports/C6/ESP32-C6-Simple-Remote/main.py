# main.py — 兩鍵模式遙控器（最簡版 / NC4 / ESP-NOW）
# ══════════════════════════════════════════════════════════════════════
# 兩顆按鈕（邏輯與舊版 C6 相同）：
#   按鈕A：MODE_SET 0x0200（可動模式 = mode_type 2 / mode_id 0）
#   按鈕B：目前值 + 1（16-bit 合併 id 直接 +1；本地推進，不等對方回報）
#
# 開機只做：
#   ① ESP-NOW → 設頻道
#   ② 0x100D 廣播點名 → 0x100E 拿 cid + MAC（協議：cid 的唯一來源）
#   ③ （可選）0x3101 單播問一次清單，只印出來當參考
#      ★ 清單拿不到**不影響按鈕**：按鈕只需要 0x3105
#   ④ 進主迴圈
#
# 刻意不做：
#   · 不做 0x3103 MODE_GET 確認（本樹沒有 handler，問了只會逾時）
#   · 不寫 flash、不做配對（0x1016）
#   · 清單不是按鈕的前置條件
#
# 協議權威：slave/lib/sys/proto.py
#   ┌──────┬─────┬───────┬───────┬───────┬──────────┬────────┐
#   │ SOF  │ VER │ ADDR  │ CMD   │ LEN   │  DATA    │ CRC32  │
#   │ 2B   │ 1B  │ 2B LE │ 2B LE │ 2B LE │ LEN B    │ 4B LE  │
#   └──────┴─────┴───────┴───────┴───────┴──────────┴────────┘
#   CRC 範圍 = buffer[2 : 9+LEN]（不含 SOF、不含 CRC 自己）
# ══════════════════════════════════════════════════════════════════════

import time
import struct
import binascii
import json

try:
    import machine
    import network
    import espnow
except ImportError:                      # CPython（離線自測用）
    machine = None
    network = None
    espnow = None


# ══════════════════════════════════════════════════════════════════
#  ① 設定 —— 全部在 /remote_config.json（本檔只讀，不寫）
# ══════════════════════════════════════════════════════════════════
#   改設定：改 PC 上的 ports/C6/ESP32-C6-Simple-Remote/remote_config.json，
#   上傳到裝置的 /remote_config.json，重開機生效。
#   找不到檔案 → 用下面的預設值（印警告，不會壞）。

CONFIG_PATH = "/remote_config.json"
CFG = {}


def _load_config():
    global CFG
    try:
        with open(CONFIG_PATH) as f:
            CFG = json.load(f)
        print("[CFG] 讀取 {}".format(CONFIG_PATH))
    except Exception as e:
        CFG = {}
        print("⚠️ 讀不到 {}（{}）→ 用預設值".format(CONFIG_PATH, e))


def _sec(name):
    v = CFG.get(name) if isinstance(CFG, dict) else None
    return v if isinstance(v, dict) else {}


def _num(v, default=None):
    """JSON 值 → int；支援 0x 字串。null/空字串 → default。"""
    if v is None or v == "":
        return default
    try:
        return int(v, 0) if isinstance(v, str) else int(v)
    except Exception:
        return default


def _int(v, default):
    """非 nullable 的整數（null/壞值 → default）。"""
    n = _num(v)
    return default if n is None else n


def _bool(v, default=False):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


_load_config()

# ── 按鈕 ─────────────────────────────────────────────────────────
_b = _sec("buttons")
BTN_A_PIN = _int(_b.get("a_pin"), 11)
BTN_B_PIN = _int(_b.get("b_pin"), 12)
DEBOUNCE_MS = 30

# ── 頻道 ─────────────────────────────────────────────────────────
CHANNEL = _num(CFG.get("channel"), 6)

# ── Mon（我們自己）──────────────────────────────────────────────
MY_CID = _int(CFG.get("my_cid"), 0x0101) & 0xFFFF
MASTER_CID = MY_CID
BIND_AS_MASTER = True

# ── 位址模式：own / spoof / ff（三態）────────────────────────────
#   own   = 原廠 MAC（預設）
#   spoof = 冒充 spoof_mac（舊 Mon 的真 MAC）
#   ff    = 電波 FF:FF:FF:FF:FF:FF、協議 addr=0xFFFF
ADDRESS_MODE = str(CFG.get("address_mode") or "").strip().lower()
if ADDRESS_MODE not in ("own", "spoof", "ff"):
    ADDRESS_MODE = "ff" if _bool(CFG.get("broadcast"), False) else "own"
_SPOOF_CFG = CFG.get("spoof_mac") or (_sec("master").get("mac")) or None
SPOOF_MAC = _SPOOF_CFG if ADDRESS_MODE == "spoof" else None
if ADDRESS_MODE == "spoof" and not SPOOF_MAC:
    print("⚠️ address_mode=spoof 但沒有 spoof_mac → 改用 own")
    ADDRESS_MODE = "own"
    SPOOF_MAC = None
SEND_MODE = "broadcast" if ADDRESS_MODE == "ff" else "unicast"

# ── Peer（要控制的 fastLED Master；通常不用填）──────────────────
_p = _sec("peer")
PEER_MAC = _p.get("mac") or None
PEER_CID = _num(_p.get("cid"))

# ── 模式 ─────────────────────────────────────────────────────────
_mo = _sec("mode")
MOVABLE_MODE = _int(_mo.get("a"), 0x0200) & 0xFFFF
STEP = _int(_mo.get("step"), 1)
STEP_BASE_IF_UNKNOWN = MOVABLE_MODE

# ── 固定值（不需要設定）──────────────────────────────────────────
BRIGHTNESS = 255          # MODE_SET 的亮度：255 = 不改
START_DELAY_MS = 0
CONFIRM_AFTER_SET = True  # 有 0x3103 handler 才會確認
CONFIRM_DELAY_MS = 80
SCAN_WINDOW_MS = 300
WAIT_MS = 1000
SHOW_MODE_LIST = True
DEBUG = _bool(CFG.get("debug"), True)

# ── 指示燈（清單：pin / brightness 0..100 / active_low）──────────
LED_SPECS = []            # [(pin, duty 0..65535, active_low), ...]
_led_cfg = CFG.get("leds")
if isinstance(_led_cfg, list):
    for item in _led_cfg:
        if not isinstance(item, dict):
            continue
        _pn = _num(item.get("pin"))
        if _pn is None:
            continue
        _pct = max(0, min(100, _int(item.get("brightness"), 100)))
        LED_SPECS.append((_pn, _pct * 65535 // 100,
                          _bool(item.get("active_low"), False)))
else:
    # 沒有設定檔（或沒有 leds 欄位）→ 預設 GPIO48 全亮
    LED_SPECS = [(48, 65535, False)]


# ══════════════════════════════════════════════════════════════════
#  ② NC4 幀（與 slave/lib/sys/proto.py 位元組完全相同）
# ══════════════════════════════════════════════════════════════════

SOF = b"NC"
VER = 4
HDR_LEN = 9
CRC_LEN = 4
MAX_PAYLOAD = 250                       # ESP-NOW 單幀上限
ADDR_BROADCAST = 0xFFFF
BCAST_MAC = b"\xff\xff\xff\xff\xff\xff"

CMD_IDENTIFY_REQ = 0x100D
CMD_IDENTIFY_RSP = 0x100E
CMD_SET_MASTER = 0x1016
CMD_STATUS_GET = 0x1101
CMD_STATUS_RSP = 0x1102
CMD_MODE_LIST = 0x3101
CMD_MODE_LIST_RSP = 0x3102
CMD_MODE_GET = 0x3103
CMD_MODE_GET_RSP = 0x3104
CMD_MODE_SET = 0x3105


def _ticks_ms():
    return time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.time() * 1000)


def _ticks_diff(a, b):
    return time.ticks_diff(a, b) if hasattr(time, "ticks_diff") else (a - b)


def _ticks_add(t, d):
    return time.ticks_add(t, d) if hasattr(time, "ticks_add") else (t + d)


def _sleep_ms(ms):
    if hasattr(time, "sleep_ms"):
        time.sleep_ms(ms)
    else:
        time.sleep(ms / 1000.0)


def _crc32(data):
    return binascii.crc32(bytes(data)) & 0xFFFFFFFF


def _pack(cmd, payload=b"", addr=ADDR_BROADCAST):
    payload = bytes(payload)
    ln = len(payload)
    b = bytearray(HDR_LEN + ln + CRC_LEN)
    struct.pack_into("<2sBHHH", b, 0, SOF, VER, addr & 0xFFFF, cmd & 0xFFFF, ln)
    if ln:
        b[HDR_LEN:HDR_LEN + ln] = payload
    struct.pack_into("<I", b, HDR_LEN + ln, _crc32(b[2:HDR_LEN + ln]))
    return b


def _unpack(frame):
    """一個 ESP-NOW 訊息 = 一個 NC4 幀 → (addr, cmd, payload)；不像就回 None。"""
    n = len(frame)
    if n < HDR_LEN + CRC_LEN or frame[0:2] != SOF or frame[2] != VER:
        return None
    addr, cmd, ln = struct.unpack_from("<HHH", frame, 3)
    if ln > MAX_PAYLOAD or n < HDR_LEN + ln + CRC_LEN:
        return None
    crc = struct.unpack_from("<I", frame, HDR_LEN + ln)[0]
    if crc != _crc32(frame[2:HDR_LEN + ln]):
        return None
    return addr, cmd, bytes(frame[HDR_LEN:HDR_LEN + ln])


def _str_u16len(b, off):
    if len(b) < off + 2:
        return "", off
    n = b[off] | (b[off + 1] << 8)
    raw = bytes(b[off + 2:off + 2 + n])
    try:
        return raw.decode("utf-8"), off + 2 + n
    except Exception:
        return raw.hex(), off + 2 + n


def _mac_str(mac):
    return ":".join("{:02x}".format(b) for b in bytes(mac)) if mac else "?"


def _mac_bytes(s):
    """"aabbccddeeff" / "aa:bb:.." → 6-byte bytes（espnow／WLAN 只吃這個）。"""
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


# ══════════════════════════════════════════════════════════════════
#  ③ 射頻（ESP-NOW）
# ══════════════════════════════════════════════════════════════════

_radio = None
_peer = None            # (6-byte MAC, cid | None) —— None = 還沒有對象（全廣播）
_channel = None
_FACTORY_MAC = None     # 第一次 init 時記下的原廠 STA MAC
_current = MOVABLE_MODE  # 按鈕B 的 +1 起點；每次送出都更新
_HAS_MODE_GET = False    # 開機探測：對方有沒有 0x3103 handler


def _dest(force_bcast=False):
    """這一幀走哪裡 → (射頻 dst, 協議 addr)。

    ★ 兩層要一起決定：射頻 dst = 誰收得到（MAC）；協議 addr = 誰該執行（cid）。
      只知道 MAC、還沒有 cid 時用「射頻單播 + 協議廣播」。
    ★ `SEND_MODE="broadcast"` 時全部走 FF：dst=FF:FF:FF:FF:FF:FF、addr=0xFFFF。
    """
    if force_bcast or SEND_MODE == "broadcast" or _peer is None:
        return BCAST_MAC, ADDR_BROADCAST
    mac, cid = _peer
    return mac, (cid if cid is not None else ADDR_BROADCAST)


def _tx(cmd, payload=b"", force_bcast=False, quiet=False):
    if _radio is None:
        return False
    dst, addr = _dest(force_bcast)
    frame = _pack(cmd, payload, addr)
    try:
        ok = _radio.send(dst, frame)
    except OSError as ex:
        if "NOT_FOUND" in str(ex).upper():
            try:
                _radio.add_peer(dst)
                ok = _radio.send(dst, frame)
            except Exception as ex2:
                print("❌ [TX] 0x{:04X} 失敗：{}".format(cmd, ex2))
                return False
        else:
            print("❌ [TX] 0x{:04X} 失敗：{}".format(cmd, ex))
            return False
    except Exception as ex:
        print("❌ [TX] 0x{:04X} 失敗：{}".format(cmd, ex))
        return False
    if DEBUG and not quiet:
        print("[TX] 0x{:04X} dst={} addr=0x{:04X} len={} {}".format(
            cmd, dst.hex(), addr, len(frame), "ok" if ok else "FAIL（沒 ACK）"))
    return bool(ok)


def _pump(ms):
    """收幀（最多等 ms）→ [(addr, cmd, payload, src_mac), ...]。"""
    out = []
    if _radio is None:
        _sleep_ms(ms)
        return out
    end = _ticks_add(_ticks_ms(), ms)
    while True:
        try:
            mac, data = _radio.recv(0)
        except Exception:
            mac, data = None, None
        if mac is not None and data:
            f = _unpack(data)
            if f is not None:
                if DEBUG:
                    print("[RX] cmd=0x{:04X} addr=0x{:04X} src={} len={}".format(
                        f[1], f[0], _mac_str(mac), len(f[2])))
                out.append((f[0], f[1], f[2], bytes(mac)))
            elif DEBUG:
                print("[RX] 丟棄（非 NC4／CRC 錯）：{}".format(bytes(data).hex()))
        if _ticks_diff(_ticks_ms(), end) >= 0:
            break
        _sleep_ms(2)
    return out


def _radio_init():
    """STA 起來 + 套用 address_mode 的 MAC + ESP-NOW active + 註冊廣播。"""
    global _radio, _FACTORY_MAC
    if machine is None or network is None or espnow is None:
        print("❌ 這個固件沒有 machine/network/espnow")
        return False
    try:
        sta = network.WLAN(network.STA_IF)
        ap = network.WLAN(network.AP_IF)
        # MicroPython 軟重置不會重設 Wi-Fi；先關再開，避免延續上次狀態
        for w in (ap, sta):
            try:
                if w.active():
                    w.active(False)
            except Exception:
                pass
        sta.active(True)
        _sleep_ms(100)
        # 原廠 MAC 一律用 machine.unique_id()：
        # soft reboot 不會還原 STA MAC，若用 sta.config('mac') 會把冒充值當成原廠。
        try:
            uid = machine.unique_id()
            if isinstance(uid, (bytes, bytearray)) and len(uid) == 6:
                _FACTORY_MAC = bytes(uid)
            else:
                _FACTORY_MAC = bytes(sta.config("mac"))
        except Exception:
            try:
                _FACTORY_MAC = bytes(sta.config("mac"))
            except Exception:
                _FACTORY_MAC = None
        if _FACTORY_MAC:
            print("[MAC] factory={}  mode={}".format(
                _mac_str(_FACTORY_MAC), ADDRESS_MODE))
        target = None
        if SPOOF_MAC:
            target = _mac_bytes(SPOOF_MAC)
            if target is None:
                print("⚠️ spoof_mac 格式不對：{!r} → 用原廠".format(SPOOF_MAC))
        if target is None:
            target = _FACTORY_MAC
        try:
            cur = bytes(sta.config("mac"))
        except Exception:
            cur = None
        if target and cur != target:
            try:
                sta.active(False)
                _sleep_ms(100)
                sta.config(mac=target)
                sta.active(True)
                _sleep_ms(200)
                print("[MAC] mode={} → {}".format(
                    ADDRESS_MODE, _mac_str(sta.config("mac"))))
            except Exception as ex:
                print("❌ MAC 套用失敗：{}".format(ex))
        try:
            sta.config(pm=0)            # PM_NONE：省電會讓收訊漏幀
        except Exception:
            pass
        e = espnow.ESPNow()
        if e.active():                  # 清掉上一次的 peer 表（否則 add_peer 會 EXIST）
            e.active(False)
        e.active(True)
        try:
            e.add_peer(BCAST_MAC)
        except OSError as ex:
            if "EXIST" not in str(ex).upper():
                raise
        _radio = e
        try:
            my_mac = _mac_str(sta.config("mac"))
        except Exception:
            my_mac = "?"
        print("✅ ESP-NOW 已啟動  my_mac={}  mode={}".format(my_mac, ADDRESS_MODE))
        return True
    except Exception as ex:
        print("❌ ESP-NOW 初始化失敗：{}".format(ex))
        return False


def _set_channel(ch, verbose=False):
    """換頻道。回 True/False。

    ★ 留 120ms 讓射頻真的切過去 —— 40ms 時曾出現「回報 5、實際還在 6」的競態。
    """
    global _channel
    try:
        sta = network.WLAN(network.STA_IF)
        sta.config(channel=int(ch))
        _sleep_ms(120)
        got = None
        try:
            got = sta.config("channel")
        except Exception:
            pass
        _channel = int(ch)
        if verbose and DEBUG:
            print("[CH] 掃 {} → 實測回報 {}".format(ch, got))
        if got is not None and got != int(ch):
            print("⚠️ 要求頻道 {}，實測回報 {}".format(ch, got))
        return True
    except Exception as ex:
        print("❌ 設頻道 {} 失敗：{}".format(ch, ex))
        return False


# ══════════════════════════════════════════════════════════════════
#  ④ 指令
# ══════════════════════════════════════════════════════════════════

def _decode_identify(p):
    """0x100E：cid(u16) + slave_id(str_u16len) + ip(str_u16len)。"""
    if len(p) < 2:
        return None
    cid = p[0] | (p[1] << 8)
    sid, off = _str_u16len(p, 2)
    ip, _ = _str_u16len(p, off)
    return {"cid": cid, "slave_id": sid, "ip": ip}


def scan(window_ms=SCAN_WINDOW_MS, wait_ms=None):
    """廣播 0x100D 點名 → 0x100E。回 {mac: {cid, slave_id, ip}}。

    ★ force=1：reply_cid=0xFFFF 時，節點對同一個問的人只回第一次。
    """
    _pump(0)
    _tx(CMD_IDENTIFY_REQ,
        struct.pack("<HHB", ADDR_BROADCAST, window_ms & 0xFFFF, 1),
        force_bcast=True)
    if wait_ms is None:
        wait_ms = window_ms + 500
    end = _ticks_add(_ticks_ms(), wait_ms)
    found = {}
    while _ticks_diff(_ticks_ms(), end) < 0:
        for _addr, cmd, payload, src in _pump(20):
            if cmd != CMD_IDENTIFY_RSP:
                continue
            info = _decode_identify(payload)
            if info is None:
                continue
            found[bytes(src)] = info
            try:
                _radio.add_peer(bytes(src))
            except Exception:
                pass
    return found


def _parse_list(payload, src):
    """0x3102 → {"ids": [...], "src": mac, "mode_type": n}；太短回 None。"""
    if len(payload) < 2:
        return None
    mt, count = payload[0], payload[1]
    raw = payload[2:2 + count * 2]
    ids = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw) - 1, 2)]
    return {"ids": ids, "src": bytes(src), "mode_type": mt}


def query_modes(timeout=WAIT_MS, force_bcast=False, want=None):
    """0x3101 → 0x3102（只給診斷/顯示用；按鈕不依賴它）。

    ★ 協議：`MODE_LIST_QUERY` 是「指定單播、必回覆」。
      有 `_peer` 就用 (dst=MAC, addr=cid) 單播；`force_bcast` 只是相容探針。
    """
    _pump(0)
    quiet = (want is None)
    _tx(CMD_MODE_LIST, b"\x00", force_bcast=force_bcast, quiet=quiet)
    _sleep_ms(50)
    _tx(CMD_MODE_LIST, b"\x00", force_bcast=force_bcast, quiet=quiet)
    end = _ticks_add(_ticks_ms(), timeout)
    while _ticks_diff(_ticks_ms(), end) < 0:
        for _addr, cmd, payload, src in _pump(20):
            if cmd != CMD_MODE_LIST_RSP:
                continue
            if want is not None and bytes(src) != want:
                continue
            r = _parse_list(payload, src)
            if r is not None:
                return r
    return None


def modes(timeout=1200):
    """REPL：0x3101 取模式清單（先單播，沒回再廣播）。回 id list 或 None。

    用法（Ctrl-C 之後）：
        import main as m
        m.modes()
    """
    r = query_modes(timeout=timeout)
    if r is None:
        print("[MODES] 單播沒有回覆 → 改用廣播再試一次")
        r = query_modes(timeout=timeout, force_bcast=True, want=None)
    if r is None:
        print("[MODES] 沒有 0x3102：對方沒有 0x3101 handler，"
              "或它的回覆寄給別的 master（不是我們）")
        return None
    print("[MODES] 共 {} 個模式：{}".format(
        len(r["ids"]), " ".join("0x{:04X}".format(i) for i in r["ids"])))
    return r["ids"]


def get_node(timeout=600):
    """0x1101 STATUS_GET{keys="node"} → 0x1102；回 {"node": {...}} 或 None。

    這是「讀對方的 Master ID」的來源：
      node.master_cid / node.master_mac / node.bound / node.cid
    """
    _pump(0)
    key = b"node"
    _tx(CMD_STATUS_GET, struct.pack("<H", len(key)) + key, quiet=True)
    end = _ticks_add(_ticks_ms(), timeout)
    want = _peer[0] if _peer is not None else None
    while _ticks_diff(_ticks_ms(), end) < 0:
        for _addr, cmd, payload, src in _pump(20):
            if cmd != CMD_STATUS_RSP or len(payload) < 2:
                continue
            if want is not None and bytes(src) != want:
                continue
            txt, _ = _str_u16len(payload, 0)
            try:
                return json.loads(txt)
            except Exception as e:
                print("[NODE] JSON 解析失敗: {} ({!r})".format(e, txt[:40]))
                return None
    return None


def get_mode(timeout=500):
    """0x3103 MODE_GET → 0x3104；回 dict 或 None。

    回 {"mode","mode_type","mode_id","elapsed_ms","total_ms","running"}。
    ★ 協議：MODE_GET 是「指定單播、必回覆」；但本樹舊版沒有 handler，
      所以「沒回」不代表送錯，只代表對方沒實作（0x3104 不是 ACK）。
    """
    _pump(0)
    _tx(CMD_MODE_GET, b"", quiet=(_peer is None))
    end = _ticks_add(_ticks_ms(), timeout)
    want = _peer[0] if _peer is not None else None
    while _ticks_diff(_ticks_ms(), end) < 0:
        for _addr, cmd, payload, src in _pump(20):
            if cmd != CMD_MODE_GET_RSP or len(payload) < 11:
                continue
            if want is not None and bytes(src) != want:
                continue
            mt, mi, elapsed, total, running = struct.unpack("<BBIIB", payload[:11])
            return {
                "mode": (mt << 8) | mi,
                "mode_type": mt,
                "mode_id": mi,
                "elapsed_ms": elapsed,
                "total_ms": total,
                "running": running,
            }
    return None


def next_mode_of(cur, step=STEP):
    """按鈕B：目前值 + step（環繞）。

    高 byte = 0（系統模式／從未同步）→ 以 STEP_BASE_IF_UNKNOWN 為基準，
    避免送出無用的 0x0001。
    """
    base = cur
    if (base >> 8) == 0 and STEP_BASE_IF_UNKNOWN is not None:
        base = STEP_BASE_IF_UNKNOWN
    return (base + step) & 0xFFFF


def set_mode(id16, tag=""):
    """0x3105 MODE_SET —— 唯一真正換模式的動作（本身無 ACK）。

    id16 拆回 (mode_type, mode_id) 兩欄送出。送出後本地 `_current` 更新，
    下一次按鈕B 就從這裡 +1。
    對方有 0x3103 handler 時（開機探測 `_HAS_MODE_GET`），再用 0x3104 確認一次；
    沒有 handler 就**不問**（避免像舊版一樣每次逾時）。
    """
    global _current
    t = (id16 >> 8) & 0xFF
    i = id16 & 0xFF
    payload = struct.pack("<BBHB", t, i, START_DELAY_MS & 0xFFFF, BRIGHTNESS & 0xFF)
    ok = _tx(CMD_MODE_SET, payload)
    _current = id16 & 0xFFFF
    print("[SET] {}0x{:04X} (type={} id={}) {}".format(
        (tag + "：") if tag else "", id16, t, i, "已送出" if ok else "送不出去"))
    if CONFIRM_AFTER_SET and _HAS_MODE_GET:
        _sleep_ms(CONFIRM_DELAY_MS)
        info = get_mode(timeout=400)
        if info is None:
            print("[SET] ⚠️ 0x3103 沒有回覆 → 確認不了")
        elif info["mode"] == _current:
            print("[SET] ✅ 對方回報 0x{:04X}（確認成功）".format(info["mode"]))
        else:
            print("[SET] ⚠️ 對方回報 0x{:04X}（不是 0x{:04X}）→ 以對方為準".format(
                info["mode"], _current))
            _current = info["mode"]


# ══════════════════════════════════════════════════════════════════
#  ⑤ 按鈕
# ══════════════════════════════════════════════════════════════════

class Button:
    """接 GND 的按鈕：內部上拉、軟體去抖、只回「剛按下的那一次」。"""

    def __init__(self, pin_no, debounce_ms=DEBOUNCE_MS):
        self.pin = machine.Pin(pin_no, machine.Pin.IN, machine.Pin.PULL_UP)
        self.db = debounce_ms
        self.stable = 1
        self.t = _ticks_ms()

    def pressed(self):
        v = self.pin.value()
        if v == self.stable:
            return False
        now = _ticks_ms()
        if _ticks_diff(now, self.t) < self.db:
            return False
        self.t = now
        self.stable = v
        return v == 0


# ══════════════════════════════════════════════════════════════════
#  ⑥ 開機流程
# ══════════════════════════════════════════════════════════════════

def _check_pins():
    reserved = {
        8: "GPIO8 = 板載 RGB LED ＋ strapping",
        9: "GPIO9 = BOOT 鍵 ＋ strapping",
        12: "GPIO12 = USB D-（C6/S3 的原生 USB 埠）",
        13: "GPIO13 = USB D+（C6/S3 的原生 USB 埠）",
        15: "GPIO15 = strapping",
    }
    for tag, pin in (("A", BTN_A_PIN), ("B", BTN_B_PIN)):
        if pin in reserved:
            print("⚠️ 按鈕{} 接 {} → {}".format(tag, pin, reserved[pin]))
    if BTN_A_PIN == BTN_B_PIN:
        print("❌ 兩顆按鈕接同一支腳 GPIO{} → 一顆鍵會同時觸發兩件事".format(BTN_A_PIN))
        return False
    return True


def _find_peer(chans):
    """逐頻道 0x100D 點名；回 (found, channel)。"""
    for ch in chans:
        if not _set_channel(ch, verbose=(len(chans) > 1)):
            continue
        found = scan()
        if found:
            return found, ch
    return {}, None


def _fixed_peer():
    """PEER_MAC/PEER_CID → (mac_bytes, cid|None)；沒填或格式錯回 None。"""
    if PEER_MAC in (None, ""):
        return None
    mb = _mac_bytes(PEER_MAC)
    if mb is None:
        print("❌ PEER_MAC 格式不對：{!r}".format(PEER_MAC))
        return None
    cid = None
    if PEER_CID is not None:
        try:
            cid = int(PEER_CID) & 0xFFFF
        except Exception:
            print("❌ PEER_CID 格式不對：{!r}".format(PEER_CID))
            return None
    return (mb, cid)


def boot():
    """回 True = 可以進主迴圈。"""
    global _peer, _channel, _HAS_MODE_GET, _current

    if not _radio_init():
        return False
    if not _check_pins():
        return False

    chans = (CHANNEL,) if CHANNEL is not None else tuple(range(1, 14))
    found, ch = _find_peer(chans)
    if found:
        _channel = ch
        print("[SCAN] 頻道 {}｜回應 0x100D 的節點：".format(ch))
        for mac in sorted(found):
            info = found[mac]
            print("       MAC={}  cid=0x{:04X}  slave_id={}".format(
                _mac_str(mac), info["cid"], info["slave_id"] or "-"))
    else:
        print("⚠️ 0x100D 沒有節點回應（若 PEER_MAC 有填，仍會照它送）")

    fixed = _fixed_peer()
    if fixed is not None:
        mac, cid = fixed
        if cid is None and mac in found:
            cid = found[mac]["cid"]          # 探索有回就用它的 cid
        _peer = (mac, cid)
        try:
            _radio.add_peer(mac)
        except Exception:
            pass
        print("[TARGET] 固定 PEER_MAC={}  cid={}".format(
            _mac_str(mac),
            "0x{:04X}".format(cid) if cid is not None else "（無 → 協議廣播）"))
    elif found:
        mac = sorted(found)[0]
        _peer = (mac, found[mac]["cid"])
        print("[TARGET] 自動選 MAC={}  cid=0x{:04X}".format(_mac_str(mac), _peer[1]))
    else:
        _peer = None
        print("⚠️ 沒有對象 → 之後每一幀都用廣播")

    print("[CFG] SEND_MODE={}（{}）".format(
        SEND_MODE,
        "FF 廣播 / addr=0xFFFF" if SEND_MODE == "broadcast"
        else "單播 dst=PEER / addr=cid"))
    if SEND_MODE == "broadcast":
        print("⚠️ SEND_MODE=broadcast：0x1016/0x1101/0x3103 也會對同頻道所有節點送")

    # ── 讀對方的 Master ID（0x1101 node）＋ 可選 0x1016 綁定 ──────
    if _peer is not None:
        node = get_node()
        if node is None:
            print("ℹ️ 對方沒有回 node 狀態（0x1101/0x1102）")
        else:
            n = node.get("node", node)
            print("[NODE] cid=0x{:04X}  master_cid=0x{:04X}  master_mac={}  bound={}"
                  .format(int(n.get("cid", 0) or 0) & 0xFFFF,
                          int(n.get("master_cid", 0xFFFF) or 0) & 0xFFFF,
                          n.get("master_mac") or "-", bool(n.get("bound"))))
        if BIND_AS_MASTER:
            print("[BIND] 0x1016 SET_MASTER{{master_cid=0x{:04X}}}".format(MASTER_CID))
            _tx(CMD_SET_MASTER, struct.pack("<H", MASTER_CID & 0xFFFF))
            _sleep_ms(250)
            node2 = get_node()
            if node2 is None:
                print("[BIND] ⚠️ 沒有回 node 狀態 → 無法確認")
            else:
                n2 = node2.get("node", node2)
                mc = int(n2.get("master_cid", 0xFFFF) or 0) & 0xFFFF
                print("[BIND] 對方回報 master_cid=0x{:04X} master_mac={} bound={}"
                      .format(mc, n2.get("master_mac") or "-", bool(n2.get("bound"))))
                if mc != (MASTER_CID & 0xFFFF):
                    print("[BIND] ⚠️ 沒有改掉（本次開機已被別的 master 認領？重啟接收端再試）")

    # ── 探測 0x3103：對方有 handler 才做 SET 後確認 ────────────
    if _peer is not None:
        info = get_mode(timeout=400)
        if info is not None:
            _HAS_MODE_GET = True
            print("ℹ️ MODE_GET 可用：目前 0x{:04X} (type={} id={} running={} {}ms)"
                  .format(info["mode"], info["mode_type"], info["mode_id"],
                          info["running"], info["elapsed_ms"]))
            _current = info["mode"]
        else:
            print("ℹ️ 對方沒有回 0x3104（MODE_GET 未實作）→ 不做 SET 後確認")

    # ── 清單只印出來參考，不是按鈕的前置條件 ──────────────────
    if SHOW_MODE_LIST:
        r = query_modes(timeout=WAIT_MS)
        if r is not None:
            print("ℹ️ 模式清單（共 {} 個）：{}".format(
                len(r["ids"]),
                " ".join("0x{:04X}".format(i) for i in r["ids"]) or "（空）"))
        else:
            print("ℹ️ 對方沒有回 0x3102（沒有 0x3101 handler？）→ 不影響按鈕")

    print("✅ 按鈕A（GPIO{}）→ 0x{:04X}（固定）".format(BTN_A_PIN, MOVABLE_MODE))
    print("✅ 按鈕B（GPIO{}）→ 目前值 +{}（本地推進，起始 0x{:04X}）".format(
        BTN_B_PIN, STEP, _current))
    return True


def loop():
    global _current
    try:
        a = Button(BTN_A_PIN)
        b = Button(BTN_B_PIN)
    except Exception as ex:
        print("❌ 按鈕初始化失敗：{}（GPIO{} / GPIO{} 在這塊板子可用嗎？）".format(
            ex, BTN_A_PIN, BTN_B_PIN))
        return
    print("[RUN] 待機中：A=GPIO{} / B=GPIO{}（Ctrl-C 後可打 listen()/refresh()）"
          .format(BTN_A_PIN, BTN_B_PIN))
    while True:
        if a.pressed():
            set_mode(MOVABLE_MODE, "A")
        if b.pressed():
            nxt = next_mode_of(_current)
            set_mode(nxt, "B")
        _sleep_ms(5)


def pair(cid=None):
    """REPL：0x1016{my_cid} → 綁定（預設 MY_CID），再讀 node 確認。"""
    c = MY_CID if cid is None else (int(cid) & 0xFFFF)
    print("[PAIR] 0x1016{0x%04X}" % c)
    _tx(CMD_SET_MASTER, struct.pack("<H", c))
    _sleep_ms(300)
    n = get_node()
    print("[PAIR] node =", n)
    return n


def unpair():
    """REPL：0x1016{0xFFFF} → 解除（本版合約：任何來源都可；目前 owner 一定可）。"""
    print("[UNPAIR] 0x1016{0xFFFF}")
    _tx(CMD_SET_MASTER, struct.pack("<H", 0xFFFF))
    _sleep_ms(300)
    n = get_node()
    print("[UNPAIR] node =", n)
    return n


def set_address_mode(mode):
    """REPL：own / spoof / ff；會重啟 ESP-NOW 並套用 MAC。

    own   = 原廠 MAC（預設）
    spoof = 冒充 spoof_mac（舊 Mon 的真 MAC）
    ff    = 電波 FF:FF:FF:FF:FF:FF / addr=0xFFFF
    """
    global ADDRESS_MODE, SPOOF_MAC, SEND_MODE
    mode = str(mode).strip().lower()
    if mode not in ("own", "spoof", "ff"):
        print("❌ 可用：own / spoof / ff（收到 {!r}）".format(mode))
        return False
    if mode == "spoof" and not _SPOOF_CFG:
        print("⚠️ 沒有 spoof_mac → 用原廠 MAC")
    ADDRESS_MODE = mode
    SPOOF_MAC = _SPOOF_CFG if mode == "spoof" else None
    SEND_MODE = "broadcast" if mode == "ff" else "unicast"
    if not _radio_init():
        return False
    if _channel is not None:
        _set_channel(_channel)
    if _peer is not None:
        try:
            _radio.add_peer(_peer[0])
        except OSError:
            pass
    print("[CFG] ADDRESS_MODE={}  SEND_MODE={}  spoof={}".format(
        ADDRESS_MODE, SEND_MODE, SPOOF_MAC or "-"))
    return True


def spoof_unbind(rebind=True):
    """以防萬一：冒充 spoof_mac（舊 Mon）單播 0x1016{0xFFFF} 解綁。

    rebind=True（預設）：解除後立刻用真 MAC 重新綁定 MY_CID。
    前提：spoof_mac 是目前 owner；舊面板必須關機。
    若目前 owner 不是 spoof_mac，解綁會被拒（node 仍 bound）。
    """
    if not _SPOOF_CFG:
        print("❌ 沒有 spoof_mac，無法冒充解綁")
        return False
    print("[SPOOF-UNBIND] 冒充 {} 解綁（rebind={}）".format(
        _SPOOF_CFG, rebind))
    unbound = False
    try:
        if not set_address_mode("spoof"):
            return False
        _tx(CMD_SET_MASTER, struct.pack("<H", 0xFFFF))
        _sleep_ms(300)
        n = get_node()
        print("[SPOOF-UNBIND] after unbind =", n)
        node = n.get("node", n) if n else {}
        unbound = not bool(node.get("bound"))
    finally:
        set_address_mode("own")
    if not unbound:
        print("[SPOOF-UNBIND] ⚠️ 解綁失敗（spoof_mac 不是目前 owner？）")
        return False
    if not rebind:
        print("[SPOOF-UNBIND] ✅ 已解除（目前 own，未重新綁定）")
        return True
    _tx(CMD_SET_MASTER, struct.pack("<H", MY_CID))
    _sleep_ms(300)
    n2 = get_node()
    print("[SPOOF-UNBIND] after rebind =", n2)
    node2 = n2.get("node", n2) if n2 else {}
    own = (_mac_str(_FACTORY_MAC) if _FACTORY_MAC else "").replace(":", "").upper()
    mac = str(node2.get("master_mac") or "").replace(":", "").upper()
    ok = bool(node2.get("bound")) and own != "" and mac == own
    print("[SPOOF-UNBIND] {}".format(
        "✅ 完成（回到自己的 MAC）" if ok else "⚠️ 未完成"))
    return ok


def set_send_mode(mode):
    """REPL：切換 "unicast"（預設）／"broadcast"（FF 廣播）。

    用法（Ctrl-C 之後）：
        import main as m
        m.set_send_mode("broadcast")   # 之後每一幀都走 FF / 0xFFFF
        m.main()                       # 或 m.loop() 恢復按鈕
    """
    global SEND_MODE
    if mode not in ("unicast", "broadcast"):
        print("❌ 可用：'unicast' / 'broadcast'（收到 {!r}）".format(mode))
        return False
    SEND_MODE = mode
    print("[CFG] SEND_MODE = {}（{}）".format(
        mode,
        "FF:FF:FF:FF:FF:FF / addr=0xFFFF" if mode == "broadcast"
        else "dst=PEER_MAC / addr=PEER_CID"))
    return True


def listen(sec=15):
    """REPL：被動監聽，印出這段時間收到的每一幀（不發東西）。"""
    print("[LISTEN] 被動監聽 {} 秒…".format(sec))
    seen = {}
    end = _ticks_add(_ticks_ms(), int(sec * 1000))
    while _ticks_diff(_ticks_ms(), end) < 0:
        for _addr, _cmd, _payload, src in _pump(50):
            seen[src] = seen.get(src, 0) + 1
    if not seen:
        print("[LISTEN] 一幀都沒收到")
        return []
    for mac, n in seen.items():
        print("  {:>4} 幀  src={}".format(n, _mac_str(mac)))
    return list(seen.items())


def refresh():
    """REPL：重新點名（0x100D）並重印清單；跑完打 loop() 恢復按鈕。"""
    global _peer
    chans = (CHANNEL,) if CHANNEL is not None else tuple(range(1, 14))
    found, ch = _find_peer(chans)
    if not found:
        print("❌ 0x100D 沒有節點回應")
        return
    _peer = (sorted(found)[0], found[sorted(found)[0]]["cid"])
    print("[REFRESH] 頻道 {}｜目標 MAC={} cid=0x{:04X}".format(
        ch, _mac_str(_peer[0]), _peer[1]))
    r = query_modes()
    if r is not None:
        print("[REFRESH] 模式清單：{}".format(
            " ".join("0x{:04X}".format(i) for i in r["ids"])))
    print("[REFRESH] 完成；打 loop() 恢復按鈕")


# ══════════════════════════════════════════════════════════════════
#  ⑦ 指示燈（LED_SPECS：pin / brightness；全部 PWM 單色）
# ══════════════════════════════════════════════════════════════════

_leds = []       # [(PWM, ready_duty, active_low), ...]


def _pwm_new(pin):
    if machine is None:
        return None
    try:
        p = machine.PWM(machine.Pin(pin), freq=1000)
        _pwm_set(p, 0)
        return p
    except Exception as ex:
        print("⚠️ 指示燈 GPIO{} 初始化失敗：{}".format(pin, ex))
        return None


def _pwm_set(p, duty):
    d = max(0, min(65535, int(duty)))
    try:
        try:
            p.duty_u16(d)
        except Exception:
            p.duty(int(d * 1023 / 65535))
    except Exception:
        pass


def _leds_init():
    """依 LED_SPECS 建 PWM。失敗只警告，不擋開機。"""
    global _leds
    _leds = []
    if machine is None:
        return
    for pin, duty, al in LED_SPECS:
        p = _pwm_new(pin)
        if p is not None:
            _leds.append((p, duty, al))


def _led_write(entry, duty):
    p, _, al = entry
    d = max(0, min(65535, int(duty)))
    _pwm_set(p, (65535 - d) if al else d)


def led_boot():
    """開機／匯入中：全暗。"""
    for e in _leds:
        _led_write(e, 0)


def led_ready(ok=True):
    """準備好：依 brightness 亮；失敗：暗 ＋ 快閃 6 下。"""
    if ok:
        for e in _leds:
            _led_write(e, e[1])
        return
    for e in _leds:
        _led_write(e, 0)
    for _ in range(6):
        for e in _leds:
            _led_write(e, 65535)
        _sleep_ms(120)
        for e in _leds:
            _led_write(e, 0)
        _sleep_ms(120)


def pwm_led(idx, percent):
    """REPL：第 idx 個指示燈亮度 0..100。"""
    if idx < 0 or idx >= len(_leds):
        print("❌ 指示燈索引 {} 不存在（已啟用 {} 個）".format(idx, len(_leds)))
        return
    pct = max(0, min(100, int(percent)))
    _led_write(_leds[idx], pct * 65535 // 100)
    print("[LED] #{} brightness={}%".format(idx, pct))


def main():
    _leds_init()
    led_boot()
    print("=== 兩鍵模式遙控器（A=0x{:04X} / B=+1）===".format(MOVABLE_MODE))
    if not boot():
        led_ready(False)
        print("❌ 開機失敗（指示燈紅）")
        return
    led_ready(True)
    loop()


if __name__ == "__main__":
    main()
