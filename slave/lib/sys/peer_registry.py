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
#   4. 持久化：**btree `@peer.<slave_id>`（一節點一 key）**，走 ConfigManager 的
#      KV 門面。逐 key 更新 —— 學到一個新節點只寫那一筆，不重寫整包。
#      寫入節流（見 _save_min_ms）；取不到 KV 門面時退化為「不持久化」，運作照常。
#      舊的 /peers.json 會在第一次載入時**一次性遷移**進 btree（舊檔改名 .old）。
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

PATH = "/peers.json"      # ★ 舊格式，僅用於一次性遷移（新資料在 btree）
KV_PREFIX = "peer."       # btree 命名空間前綴（實際 key = @peer.<slave_id>）

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
        # 髒追蹤：分「有變更」與「被刪除」兩組 —— 逐 key 寫入用（btree 的價值）
        self._dirty = False
        self._dirty_sids = set()
        self._removed_sids = set()
        self._last_save = 0
        self._save_min_ms = 2000      # 寫入節流：2 秒內多次更新只寫一次
        self.stats = {"learned": 0, "updated": 0, "loaded": 0,
                      "save_fail": 0, "saved": 0, "migrated": 0}

    def _kv(self):
        """取 btree KV 門面（ConfigManager）。取不到回 None。

        刻意**延遲 import**：peer_registry 宣告「CPython 可離線測」，
        而 ConfigManager 在 module 層就會開 btree 檔（import 有副作用）。
        取不到時本模組退化為「不持久化」，學習與查詢照常運作。
        """
        try:
            from lib.sys.ConfigManager import cfg_manager
            return cfg_manager
        except Exception:
            return None

    # ── 持久化（btree，一節點一 key）────────────────────────────
    def load(self):
        """從 btree `@peer.*` 載入。沒有紀錄 = 正常（第一次跑），回 0。

        若 btree 裡完全沒有 peer 而舊的 /peers.json 還在 → 做一次性遷移。
        """
        if self._loaded:
            return len(self._peers)
        self._loaded = True
        n = 0
        kv = self._kv()
        if kv is not None:
            for k in kv.kv_keys(KV_PREFIX):
                rec = kv.kv_get(k, None)
                if not isinstance(rec, dict):
                    continue
                n += self._adopt(k[len(KV_PREFIX):], rec)
            self.stats["loaded"] = n
            if n == 0:
                n = self._migrate_legacy(kv)
        else:
            get_log().warn("[Peers] 無 KV 門面 → 本次不載入（運作照常，只是不持久化）")
        self._dirty = False
        self._dirty_sids.clear()
        self._removed_sids.clear()
        self._publish()
        return n

    def _adopt(self, sid, rec):
        """把一筆（來自 btree 或舊檔）收進記憶體。回 1/0。

        last_seen 是「上次開機」的 ticks_ms，跨開機無意義 → 標成本次未見過。
        """
        sid = str(sid).upper()
        if not sid or not isinstance(rec, dict):
            return 0
        rec["last_seen"] = _NEVER
        rec["hits"] = int(rec.get("hits", 0) or 0)
        rec["ifaces"] = list(rec.get("ifaces", []) or [])
        self._peers[sid] = rec
        return 1

    def _migrate_legacy(self, kv):
        """一次性遷移舊 /peers.json → btree。搬完把舊檔改名 .old（可回復）。

        只在「btree 裡一個 peer 都沒有」時嘗試 —— 避免覆蓋已經生效的新資料。
        """
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except OSError:
            return 0                      # 沒有舊檔 = 正常
        except Exception as e:
            get_log().warn("[Peers] 舊檔解析失敗，放棄遷移: {}".format(e))
            return 0
        peers = raw.get("peers", raw) if isinstance(raw, dict) else {}
        n = 0
        for sid, rec in (peers or {}).items():
            if self._adopt(sid, rec):
                kv.kv_set(KV_PREFIX + str(sid).upper(), self._peers[str(sid).upper()])
                n += 1
        if n:
            kv.kv_flush()
            try:
                os.rename(self.path, self.path + ".old")
            except Exception:
                pass
            self.stats["loaded"] = n
            self.stats["migrated"] = n
            get_log().info("[Peers] 舊 /peers.json 遷移 {} 筆 → btree（舊檔改名 .old）".format(n))
        return n

    def save(self, force=False):
        """把**有變更的那幾筆**逐 key 寫回 btree（一節點一 key）。

        `force=True` 繞過的是**節流**（我要現在就落盤），**不是** dirty 檢查
        （沒東西可寫就不寫）。兩者語意不同，實作時容易寫反。
        """
        if not self._dirty:
            return False
        now = _now()
        if not force and self._last_save and _diff(now, self._last_save) < self._save_min_ms:
            return False
        kv = self._kv()
        if kv is None:
            self.stats["save_fail"] += 1
            return False
        ok = True
        for sid in list(self._dirty_sids):
            rec = self._peers.get(sid)
            if rec is not None:
                ok = kv.kv_set(KV_PREFIX + sid, rec) and ok
        for sid in list(self._removed_sids):
            kv.kv_del(KV_PREFIX + sid)
        if ok:
            kv.kv_flush()
            self.stats["saved"] = self.stats.get("saved", 0) + 1
        else:
            self.stats["save_fail"] += 1
        self._dirty = False
        self._dirty_sids.clear()
        self._removed_sids.clear()
        self._last_save = now
        return ok

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
        self._dirty_sids.add(sid)      # 逐 key 寫入：只寫這一筆
        self._removed_sids.discard(sid)
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

    def learn_from_announce(self, ctx, args):
        """0x1002 SLAVE_ANNOUNCE handler —— **對端自己送上門**的公告。

        為什麼需要它（ESP-NOW 的結構性事實）：
          射頻位址是 MAC，MAC **不是可枚舉的數值空間** → 「逐 address 掃描」
          （0x100D）在 ESP-NOW 上**無從發起**。所以「被發現」只能等對端送東西來，
          而送來的那一刻，射頻層才會告訴我們來源 MAC（ctx["_peer_mac"]）。

        與 `learn_from_frame` 的關係：互補，不是重複。
          - `learn_from_frame`：任何幀都登記，但**只有 MAC、沒有身份**
          - 這裡：公告**自帶 slave_id**（ESP32 上 slave_id == MAC hex，
            所以 `_record` 的 key 相同 → 寫進同一筆）
        被動學習先寫、這裡後補，結果一致；pixel_count / hw_version 只 print，
        不進記錄（塞進 `name` 會語意錯亂）。
        """
        peer_mac = (ctx or {}).get("_peer_mac")
        sid = args.get("slave_id") or ""
        if not sid and not peer_mac:
            return None
        return self._record(
            sid or peer_mac,
            via="announce",
            mac=peer_mac,
            iface=(ctx or {}).get("transport"),
            cmd=0x1002,
        )

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
            self._dirty_sids.discard(sid)
            self._removed_sids.add(sid)     # 下一次 save 會把 btree 那一筆刪掉
            self._publish()
            return True
        return False

    def clear(self):
        n = len(self._peers)
        self._removed_sids |= set(self._peers.keys())
        self._peers = {}
        self._dirty = True
        self._dirty_sids.clear()
        self._publish()
        return n

    # ── 內部 ─────────────────────────────────────────────────
    def _publish(self):
        """把表推上 bus.shared["peers"]（跨核 / 其他 task 讀同一份）。"""
        bus.shared["peers"] = self._peers

    def housekeep(self):
        """掛在 BusDecodeTask 的尾端（每輪呼叫）：把累積的變更寫回 btree。
        節流在 save() 內，這裡呼叫成本 = 一次時間比較。"""
        if self._dirty:
            self.save()
