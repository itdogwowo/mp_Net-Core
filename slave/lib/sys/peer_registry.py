# lib/sys/peer_registry.py
# PeerRegistry — 「自己素描到哪些節點」的紀錄（單一事實來源）
#
# ═══════════════════════════════════════════════════════════════════════
# 為什麼需要它
# ═══════════════════════════════════════════════════════════════════════
#   0x100D IDENTIFY_REQ 是「逐 address 掃描」（模仿 I2C）—— 自己主動去問
#   「你是誰」，0x100E IDENTIFY_RSP 回 cid + slave_id + ip。
#   問題：回來的東西**沒有地方存**（`bus.master_cid` 只存「master 是誰」，
#   不是「我問到了誰」；`NowBus._peers` 只記 MAC 布林值，供射頻層 add_peer 用）。
#
#   面板（control panel）要做「定向查詢」就必須有這張表 —— 見
#   doc/03_notes/18_pixel_panel_control_path.md §7.2。
#
# ═══════════════════════════════════════════════════════════════════════
# ★ 雙層定址：一個 peer 要記「兩個位址」，缺一不可
# ═══════════════════════════════════════════════════════════════════════
#   NC4 層（邏輯）  cID  —— 填進幀頭的 addr；對方靠 `addr == bus.cid` 判斷
#                          「這幀是給我的」（app.py handle_stream 的 ADDR 過濾）
#   射頻層（實體）  MAC  —— ESP-NOW 要 `add_peer(mac)` 才送得出去
#
#   兩者**不是同一個東西**：cID 是 MAC 末 4 碼推導的（ConfigManager
#   `ensure_cID`），但射頻層只認完整 6-byte MAC。
#   所以 `directed_query(mac, frame_with_addr_peer_cid)` 兩個都要給。
#
# ═══════════════════════════════════════════════════════════════════════
# 設計要點
# ═══════════════════════════════════════════════════════════════════════
#   1. 一個 peer 一筆，key = slave_id（完整 6-byte MAC 大寫 hex，穩定識別）。
#   2. 學習來源兩種，可混用：
#        主動  learn_from_identify_rsp()  ← 0x100E 回覆（淨）
#        被動  learn_from_frame()         ← 任何幀（含對方主動送來的）
#   3. 不持有任何硬體、不 import espnow/network —— CPython 可離線測。
#   4. 持久化：/peers.json。寫入節流（見 _save_min_ms），不留檔也不影響運作。
#   5. 樂觀原則：**登記不代表在線**。`age_ms()` 給呼叫端自己判斷新鮮度，
#      本模組不自行判定離線（沒有心跳就沒有離線的依據）。
#
# 狀態: bus.shared["peers"] = {slave_id: {cid, mac, ip, name, first_seen,
#                                          last_seen, hits, via, ifaces}}
# 服務: bus.register_service("peers", PeerRegistry())（app.py 建立）
import json
import os
import time
from lib.sys.sys_bus import bus
from lib.sys.log_service import get_log

PATH = "/peers.json"

# 「本次開機尚未見過」的哨兵。0 是安全的哨兵值：
#   MicroPython ticks_ms 開機瞬間剛好為 0 的機率是 1/2^30，且後果只是
#   age_ms() 多回一次 None（不影響任何寫入或查表）。
#   不用 None/物件哨兵的原因：last_seen 會被 json.dump 序列化，
#   非數字值會在「載入後未學習就存檔」時讓存檔失敗。
_NEVER = 0


def _now():
    try:
        return time.ticks_ms()
    except Exception:
        return 0


def _diff(a, b):
    try:
        return time.ticks_diff(a, b)
    except Exception:
        return a - b


def _mac_hex(mac):
    """bytes/MAC → 大寫 hex 字串；已是字串就正規化。認不出來回 None。"""
    if mac is None:
        return None
    if isinstance(mac, str):
        s = mac.replace(":", "").replace("-", "").upper()
        return s if s else None
    try:
        return "".join("{:02X}".format(b) for b in mac)
    except Exception:
        return None


class PeerRegistry:
    """素描紀錄：誰被我問到過、從哪條線、用哪個位址可以再找到他。"""

    def __init__(self, path=PATH):
        self.path = path
        self._peers = {}
        self._loaded = False
        self._dirty = False
        self._last_save = 0
        self._save_min_ms = 2000      # 寫入節流：2 秒內多次更新只寫一次
        self.stats = {"learned": 0, "updated": 0, "loaded": 0, "save_fail": 0}

    # ── 持久化 ────────────────────────────────────────────────
    def load(self):
        """從 /peers.json 載入。檔案不存在 = 正常（第一次跑），回 0。"""
        if self._loaded:
            return len(self._peers)
        self._loaded = True
        n = 0
        try:
            with open(self.path) as f:
                raw = json.load(f)
            peers = raw.get("peers", raw) if isinstance(raw, dict) else {}
            for sid, rec in (peers or {}).items():
                if not isinstance(rec, dict):
                    continue
                # last_seen 是「上次開機」的 ticks_ms，跨開機無意義 → 標成本次未見過
                rec["last_seen"] = _NEVER
                rec["hits"] = int(rec.get("hits", 0) or 0)
                rec["ifaces"] = list(rec.get("ifaces", []) or [])
                self._peers[str(sid).upper()] = rec
                n += 1
            self.stats["loaded"] = n
        except OSError:
            pass
        except Exception as e:
            get_log().warn("[Peers] 載入失敗（忽略）: {}".format(e))
        self._publish()
        return n

    def save(self, force=False):
        """寫回 /peers.json。

        `force=True` 繞過的是**節流**（我要現在就落盤），**不是** dirty 檢查
        （沒東西可寫就不寫）。兩者語意不同，實作時容易寫反。
        """
        if not self._dirty:
            return False
        now = _now()
        if not force and self._last_save and _diff(now, self._last_save) < self._save_min_ms:
            return False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"version": 1, "peers": self._peers}, f)
        except Exception as e:
            self.stats["save_fail"] += 1
            get_log().warn("[Peers] 寫入 tmp 失敗: {}".format(e))
            return False
        # 原子替換：MicroPython / CPython 的 os.rename 都會覆蓋既有檔
        try:
            os.rename(tmp, self.path)
        except Exception:
            try:
                os.remove(self.path)
                os.rename(tmp, self.path)
            except Exception as e:
                self.stats["save_fail"] += 1
                get_log().warn("[Peers] 替換檔案失敗: {}".format(e))
                return False
        self._dirty = False
        self._last_save = now
        return True

    # ── 學習 ─────────────────────────────────────────────────
    def _record(self, slave_id, via, cid=None, mac=None, ip=None,
                name=None, iface=None, cmd=None):
        """新增或更新一筆。回 "new" / "upd" / None（無效輸入）。"""
        sid = _mac_hex(slave_id)
        if not sid:
            return None
        peers = self._peers
        now = _now()
        rec = peers.get(sid)
        if rec is None:
            rec = {
                "slave_id": sid,
                "cid": int(cid) & 0xFFFF if cid is not None else None,
                "mac": _mac_hex(mac),
                "ip": ip or "",
                "name": name or "",
                "via": via,
                "first_seen": now,
                "last_seen": now,
                "hits": 0,
                "ifaces": [],
            }
            peers[sid] = rec
            self.stats["learned"] += 1
            result = "new"
        else:
            result = "upd"
            self.stats["updated"] += 1

        # 後到的非空值覆蓋（空值不動既有資料 —— 被動學習拿不到 ip 是常態）
        if cid is not None:
            rec["cid"] = int(cid) & 0xFFFF
        if mac:
            rec["mac"] = _mac_hex(mac)
        if ip:
            rec["ip"] = ip
        if name:
            rec["name"] = name
        rec["last_seen"] = now
        rec["hits"] = int(rec.get("hits", 0) or 0) + 1
        if iface and iface not in rec["ifaces"]:
            rec["ifaces"].append(iface)

        self._dirty = True
        self._publish()
        get_log().info("[Peers] {} {} cid={} mac={} via={} iface={}".format(
            "＋" if result == "new" else "↻", sid,
            rec["cid"], rec["mac"] or "-", via, iface or "-"))
        return result

    def learn_from_identify_rsp(self, ctx, args):
        """0x100E IDENTIFY_RSP handler —— 主動掃描的回覆（資料最完整）。

        payload: cid(u16) + slave_id(str_u16len) + ip(str_u16len)
        `_peer_mac` 由 bus 從收幀當下放進 ctx（見 NowBus.poll / NetBus）。
        """
        peer_mac = (ctx or {}).get("_peer_mac")
        return self._record(
            args.get("slave_id"),
            via="identify_rsp",
            cid=args.get("cid"),
            mac=peer_mac,
            ip=args.get("ip"),
            iface=(ctx or {}).get("transport"),
            cmd=0x100E,
        )

    def learn_from_frame(self, src_bus, peer_mac, cmd, ctx=None):
        """被動學習 —— 任何來源的幀都可以。
        只登記「來源位址 + 射頻位址 + 哪條線」，不猜身份；
        身分（slave_id/cid）等 identify_rsp 或有帶身份的幀再補。
        沒有 peer_mac（UART/WS 等無射頻位址的線）→ 不登記，
        因為登記了也沒辦法定向送回去。"""
        mac = _mac_hex(peer_mac)
        if not mac:
            return None
        iface = getattr(src_bus, "label", None) or (ctx or {}).get("transport")
        return self._record(mac, via="frame", mac=mac, iface=iface, cmd=cmd)

    # ── 查詢 ─────────────────────────────────────────────────
    def get(self, slave_id):
        return self._peers.get(_mac_hex(slave_id) or "")

    def knows(self, peer_mac):
        """這個射頻位址是否已登記（給熱路徑做去重，避免逐幀 learn）。"""
        mac = _mac_hex(peer_mac)
        return bool(mac) and mac in self._peers

    def by_cid(self, cid):
        c = int(cid) & 0xFFFF
        for rec in self._peers.values():
            if rec.get("cid") == c:
                return rec
        return None

    def with_mac(self):
        """可定向送出的 peer（有 MAC 才能 espnow unicast）。"""
        return [r for r in self._peers.values() if r.get("mac")]

    def count(self):
        return len(self._peers)

    def age_ms(self, slave_id):
        """距上次見到該 peer 的毫秒數；沒見過/跨開機回 None。
        本模組不判定在線與否 —— 由呼叫端自己決定新鮮度門檻。"""
        rec = self.get(slave_id)
        if not rec:
            return None
        ls = int(rec.get("last_seen", 0) or 0)
        if ls <= 0:
            return None
        return _diff(_now(), ls)

    def snapshot(self):
        """給 STATUS/UI 用：[{slave_id, cid, mac, ip, name, age_ms, hits, ifaces}]"""
        out = []
        for sid, rec in sorted(self._peers.items()):
            out.append({
                "slave_id": sid,
                "cid": rec.get("cid"),
                "mac": rec.get("mac"),
                "ip": rec.get("ip", ""),
                "name": rec.get("name", ""),
                "age_ms": self.age_ms(sid),
                "hits": rec.get("hits", 0),
                "via": rec.get("via", ""),
                "ifaces": list(rec.get("ifaces", [])),
            })
        return out

    def forget(self, slave_id):
        sid = _mac_hex(slave_id)
        if sid and sid in self._peers:
            del self._peers[sid]
            self._dirty = True
            self._publish()
            return True
        return False

    def clear(self):
        n = len(self._peers)
        self._peers = {}
        self._dirty = True
        self._publish()
        return n

    # ── 內部 ─────────────────────────────────────────────────
    def _publish(self):
        """把表推上 bus.shared["peers"]（跨核 / 其他 task 讀同一份）。"""
        bus.shared["peers"] = self._peers

    def housekeep(self):
        """掛在 BusDecodeTask 的尾端（每輪呼叫）：把累積的變更寫回檔案。
        節流在 save() 內，這裡呼叫成本 = 一次時間比較。"""
        if self._dirty:
            self.save()
