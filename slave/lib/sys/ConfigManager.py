import btree
import json
import os
import time
from lib.sys.dispatch import dprint
from lib.sys.sys_bus import bus

# 「路徑不存在」與「值就是 None」的區別。get_by_path 的 default 用 None 時
# 分不出這兩者，save_keys 需要分（分不出就會去存一個不存在的路徑，
# 觸發 save_from_bus 的整檔重寫 fallback → 排版被重排）。
_MISSING = object()


def _cfg_now():
    """單調毫秒（MicroPython ticks_ms；CPython 離線測試退化為 monotonic）。"""
    try:
        return time.ticks_ms()
    except Exception:
        try:
            return int(time.monotonic() * 1000) & 0x3FFFFFFF
        except Exception:
            return 0


class ConfigManager:
    """
    進化版配置管理器
    核心變動：
    - 忽略所有以 '_obj' 結尾的鍵：不加載、不儲存、不保留。
    - 專注於數據持久化，不干涉運行時物件。
    """
    def __init__(self, sys_bus, config_path='config.json', db_path='secrets.db'):
        self.bus = sys_bus
        self.path = config_path
        self.db_path = db_path
        self._db = None
        self._f = None
        self._layout = {}  # 用於記錄原始鍵順序
        # 模式表快取（整存一份清單；btree 只做寫穿 + 節流落盤）
        self._mode_list = []          # [{"id": int, "name": str}, ...] 有序
        self._mode_dirty = False      # 記憶體有未落盤的變更
        self._mode_last_save = 0
        self._mode_save_min_ms = 2000  # 落盤節流（同 PeerRegistry 的 2 秒）
        self._open_db()

    def _open_db(self):
        try:
            self._f = open(self.db_path, 'r+b')
        except OSError:
            self._f = open(self.db_path, 'w+b')
        self._db = btree.open(self._f)

    def _scan_layout(self, content):
        """掃描 JSON 字符串以記錄鍵的順序"""
        layout = {}
        path_stack = [""]
        last_key = None
        i = 0
        n = len(content)
        
        while i < n:
            c = content[i]
            
            if c == '"':
                # 提取字符串
                start = i + 1
                i += 1
                while i < n:
                    if content[i] == '"' and content[i-1] != '\\':
                        break
                    i += 1
                key = content[start:i]
                
                # 檢查是否為鍵（後接冒號）
                j = i + 1
                while j < n and content[j] in ' \t\r\n':
                    j += 1
                
                if j < n and content[j] == ':':
                    current_path = path_stack[-1]
                    if current_path not in layout:
                        layout[current_path] = []
                    # 避免重複（理論上 JSON 不應有重複鍵）
                    if key not in layout[current_path]:
                        layout[current_path].append(key)
                    last_key = key
                    i = j  # 移動到冒號
                else:
                    # 值字符串，不改變 last_key 狀態
                    pass

            elif c == '{':
                new_path = path_stack[-1]
                if last_key:
                    new_path = (new_path + "." + last_key) if new_path else last_key
                path_stack.append(new_path)
                last_key = None
                
            elif c == '[':
                new_path = path_stack[-1]
                if last_key:
                    new_path = (new_path + "." + last_key) if new_path else last_key
                path_stack.append(new_path)
                last_key = None

            elif c == '}' or c == ']':
                if len(path_stack) > 1:
                    path_stack.pop()
                last_key = None
            
            elif c == ',':
                last_key = None
                
            i += 1
            
        self._layout = layout

    def _pretty_dump(self, obj, stream, indent=0, path=""):
        """格式化寫入，同時過濾掉 _obj，並清除 _pw 值，且依照原始順序排列"""
        space = "    "
        current_space = space * indent
        next_space = space * (indent + 1)

        if isinstance(obj, dict):
            # 獲取該層級的鍵順序
            ordered_keys = self._layout.get(path, [])
            
            # 過濾掉所有以 _obj 結尾的鍵，並跳過非字串 key（runtime 全域可能塞非字串）
            current_keys = [k for k in obj.keys() if isinstance(k, str) and not k.endswith('_obj')]
            
            # 排序：先按原始順序，再放新增的鍵
            sorted_keys = []
            seen_keys = set()
            
            for k in ordered_keys:
                if k in current_keys:
                    sorted_keys.append(k)
                    seen_keys.add(k)
            
            for k in current_keys:
                if k not in seen_keys:
                    sorted_keys.append(k)
            
            stream.write("{\n")
            for i, k in enumerate(sorted_keys):
                v = obj[k]
                stream.write(f'{next_space}"{k}": ')
                
                if k.endswith('_pw'):
                    stream.write('""')
                else:
                    new_path = (path + "." + k) if path else k
                    self._pretty_dump(v, stream, indent + 1, path=new_path)
                    
                if i < len(sorted_keys) - 1:
                    stream.write(",")
                stream.write("\n")
            stream.write(current_space + "}")
            
        elif isinstance(obj, list):
            stream.write("[\n")
            for i, item in enumerate(obj):
                stream.write(next_space)
                # 列表內的對象繼承當前路徑的佈局規則
                # (假設列表中所有對象共享結構，或者混合結構都記錄在同一路徑下)
                self._pretty_dump(item, stream, indent + 1, path=path)
                if i < len(obj) - 1:
                    stream.write(",")
                stream.write("\n")
            stream.write(current_space + "]")
            
        elif isinstance(obj, bool):
            stream.write("true" if obj else "false")
        elif isinstance(obj, (int, float)):
            stream.write(str(obj))
        elif isinstance(obj, str):
            stream.write(json.dumps(obj))
        elif obj is None:
            stream.write("null")
        else:
            stream.write(json.dumps(str(obj)))

    def _clean_passwords_preserve_format(self):
        """讀取當前文件內容，僅替換密碼字段為空字符串，保留所有格式"""
        try:
            with open(self.path, 'r') as f:
                content = f.read()
            
            # 簡單狀態機：尋找 "key_pw" : "value"
            # 注意：這裡假設格式是合法的 JSON
            
            output = []
            i = 0
            n = len(content)
            
            while i < n:
                c = content[i]
                
                if c == '"':
                    # 處理字串
                    start = i
                    i += 1
                    while i < n:
                        if content[i] == '"' and content[i-1] != '\\':
                            break
                        i += 1
                    # content[start:i+1] 是完整的引號包圍字串
                    token_str = content[start+1:i] # 去掉引號
                    i += 1 # 移過結尾引號
                    
                    # 檢查是否為以 _pw 結尾的鍵
                    # 尋找下一個非空白字符是否為冒號
                    j = i
                    while j < n and content[j] in ' \t\r\n':
                        j += 1
                    
                    if j < n and content[j] == ':' and token_str.endswith('_pw'):
                        # 這是一個密碼鍵，寫入鍵和冒號
                        output.append(content[start:j+1])
                        i = j + 1
                        
                        # 跳過後面的值
                        # 尋找值的開始
                        while i < n and content[i] in ' \t\r\n':
                            output.append(content[i])
                            i += 1
                        
                        # 值的結束位置判定
                        if i < n:
                            if content[i] == '"':
                                # 字串值
                                i += 1
                                while i < n:
                                    if content[i] == '"' and content[i-1] != '\\':
                                        break
                                    i += 1
                                i += 1
                            elif content[i] in 'tf': # true/false
                                while i < n and content[i] in 'truefalse':
                                    i += 1
                            elif content[i] == 'n': # null
                                while i < n and content[i] in 'null':
                                    i += 1
                            elif content[i] in '-0123456789': # number
                                while i < n and content[i] in '-0123456789.eE':
                                    i += 1
                            # 對於物件或陣列，暫不支援原地替換（太複雜），直接寫入空字串會導致語法錯誤嗎？
                            # 密碼通常是字串。如果原本是 null，也可以替換。
                            
                        # 寫入替換後的值
                        output.append('""')
                    else:
                        # 不是密碼鍵，或者只是普通字串值
                        output.append(content[start:i])
                else:
                    # 其他字符直接複製
                    output.append(c)
                    i += 1
            
            new_content = "".join(output)
            with open(self.path, 'w') as f:
                f.write(new_content)
                
            dprint(f"[Config] ✓ 密碼已清除 (保留原始格式)")
            
        except Exception as e:
            dprint(f"[Config] ✗ 格式保留清除失敗，回退到標準保存: {e}")
            self.save_from_bus()

    def load_setup(self):
        """讀取配置，並過濾敏感字眼"""
        if self.path not in os.listdir():
            self.save_from_bus()
            # ⚠️ 這條提早 return 也要建立身份/節點狀態，否則首次開機
            #    bus.cid 停在 0xFFFF、bus.shared["node"] 整個不存在。
            self.ensure_cID()
            self.load_node()
            self.load_modes()
            return

        try:
            with open(self.path, 'r') as f:
                content = f.read().strip()
                # 先掃描並記錄鍵的順序
                if content:
                    try:
                        self._scan_layout(content)
                    except Exception as e:
                        dprint(f"[Config] 佈局掃描失敗: {e}")
                
                # 使用標準 json.loads (不再依賴 OrderedDict)
                data = json.loads(content) if content else {}
        except Exception as e:
            dprint(f"[Config] 讀取解析出錯: {e}")
            data = {}

        needs_cleaning = False

        def sync_node(node, prefix=""):
            nonlocal needs_cleaning
            if isinstance(node, dict):
                for key, value in node.items():
                    db_key = f"{prefix}{key}"
                    if isinstance(value, (dict, list)):
                        sync_node(value, prefix=db_key + ".")
                    elif key.endswith('_pw'):
                        if value not in (None, "", "null"):
                            dprint(f"[Config] 🔐 入庫密碼: {key}")
                            self._db[db_key.encode()] = json.dumps(value).encode()
                            needs_cleaning = True
                        else:
                            stored = self._db.get(db_key.encode())
                            if stored:
                                node[key] = json.loads(stored.decode())
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    sync_node(item, prefix=f"{prefix}{i}.")

        sync_node(data)
        
        # 標準更新
        self.bus.shared.update(data)

        if needs_cleaning:
            self._db.flush()
            # 使用新的保留格式清除方法
            self._clean_passwords_preserve_format()

        # T0: 建立 + 推動 cID (ConfigManager 單一擁有; 解碼層直接讀 bus.cid 不重算)
        self.ensure_cID()

        # T0: 載入節點狀態（角色 / 目標）—— 與 cID 同一個「身份」職責
        self.load_node()

        # T0: 載入模式表（儲存模式 / 遠端清單）—— 遙控器顯示用
        self.load_modes()


    def ensure_cID(self):
        """cID 的唯一建立者 + 推動者 (T0 由 load_setup 呼叫)。

        建立: System.cID 為空 → 自行以 machine.unique_id() 末 4 碼填入
              (不依賴 bus.slave_id, 該值此時尚未設定); 取不到 → "FFFF"。
        推動: 將 cID 轉 uint16 寫進 bus.cid, 解碼層 ADDR 過濾單一讀取,
              不在熱路徑重複 hex→int。已填則沿用不覆寫, 但 bus.cid 恆同步。"""
        sys_cfg = self.bus.shared.get("System")
        if sys_cfg is None:
            sys_cfg = {}
            self.bus.shared["System"] = sys_cfg
        if not sys_cfg.get("cID"):
            try:
                import machine, ubinascii
                sid = ubinascii.hexlify(machine.unique_id()).decode().upper()
            except Exception:
                sid = ""
            sys_cfg["cID"] = sid[-4:] if sid else "FFFF"
            try:
                self.save_from_bus(update_key="System.cID")
                dprint(f"[Config] ✓ cID auto-filled: {sys_cfg['cID']}")
            except Exception as e:
                dprint(f"[Config] ⚠ cID persist failed: {e}")
        # 推動 uint16 到 bus.cid (單一真相; 解碼層直接讀, 不重算)
        try:
            self.bus.cid = int(sys_cfg.get("cID") or "FFFF", 16) & 0xFFFF
        except ValueError:
            self.bus.cid = 0xFFFF

    # ══════════════════════════════════════════════════════════════════
    # 節點狀態（遙控器用）—— 身份 / 角色 / 目標
    #
    #   身份  cid + mac + hostname：cid 由 ensure_cID 建立、mac 由 boot.py 設
    #         （load_node 執行時 slave_id 可能還沒設 → mac 是**快照時才讀**）
    #   角色  role    : "master" | "slave" | None
    #   目標  master_cid（當前那一個）+ targets（清單，多目標切換用）
    #
    #   持久化：@node.role / @node.master_cid / @node.targets（btree，逐 key）
    #   執行期真相：bus.role / bus.master_cid / bus.targets
    #   發布：bus.shared["node"]（跨核；UI 直接讀這一份）
    #
    #   ⚠️ 目標的 MAC **不在此存** —— 由 peers 表 by_cid() 查（避免兩份真相）
    # ══════════════════════════════════════════════════════════════════

    _NODE_KEYS = ("node.role", "node.master_cid", "node.master_mac", "node.targets")

    def node_state(self):
        """目前節點狀態快照（給 UI 讀 / 給持久化寫）。不 raise。"""
        try:
            mac = getattr(self.bus, "slave_id", "") or ""
        except Exception:
            mac = ""
        sys_cfg = self.bus.shared.get("System") or {}
        try:
            cid = int(getattr(self.bus, "cid", 0xFFFF)) & 0xFFFF
        except Exception:
            cid = 0xFFFF
        try:
            mcid = int(getattr(self.bus, "master_cid", 0xFFFF)) & 0xFFFF
        except Exception:
            mcid = 0xFFFF
        return {
            "cid": cid,
            "mac": mac,
            "hostname": sys_cfg.get("hostname", ""),
            "role": getattr(self.bus, "role", None),
            "master_cid": mcid,
            # master_mac：Master 的**射頻層**位址（大寫 hex；其他管子可為 None）。
            #   與 master_cid 成對 —— cid 填幀頭、mac 送出去，兩者缺一送不到。
            #   （2026-10 新增，見 todo/05_node_pairing.md D9/§5.2）
            "master_mac": getattr(self.bus, "master_mac", None),
            "bound": mcid != 0xFFFF,
            "targets": list(getattr(self.bus, "targets", None) or []),
        }

    def publish_node(self):
        """把節點狀態推上 bus.shared["node"]（UI / 其他 task 讀同一份）。"""
        try:
            self.bus.shared["node"] = self.node_state()
        except Exception:
            pass

    def load_node(self):
        """從 btree 載入節點狀態 → 推到 bus。開機呼叫一次（load_setup 尾端）。

        沒有紀錄 = 正常（第一次跑）：維持預設（role=None、master_cid=0xFFFF）。
        """
        self.bus.role = self.kv_get("node.role", None) or None
        mcid = self.kv_get("node.master_cid", None)
        if mcid is not None:
            try:
                self.bus.master_cid = int(mcid) & 0xFFFF
            except Exception:
                pass
        mmac = self.kv_get("node.master_mac", None)
        self.bus.master_mac = mmac if isinstance(mmac, str) and mmac else None
        #   ★ 只收字串（mac_hex 的產物）。壞資料/舊格式 → None，不 raise。
        self.bus.targets = list(self.kv_get("node.targets", None) or [])
        self.publish_node()
        if self.bus.role or self.bus.targets or self.bus.master_cid != 0xFFFF:
            dprint("[Config] ✓ node state loaded: role={} master=0x{:04X}/{} targets={}".format(
                self.bus.role, self.bus.master_cid,
                self.bus.master_mac or "-", len(self.bus.targets)))

    def save_node(self):
        """把節點狀態寫回 btree 並落盤。回 True/False（不 raise）。

        呼叫時機：**明確的方向確認動作**（收到 0x1016 SET_MASTER，或 UI 按了綁定）。
        不要掛在熱路徑 —— 每次呼叫都會寫 flash。
        """
        ok = False
        try:
            ok = self.kv_set("node.role", getattr(self.bus, "role", None))
            ok = self.kv_set("node.master_cid",
                             int(getattr(self.bus, "master_cid", 0xFFFF)) & 0xFFFF) and ok
            ok = self.kv_set("node.master_mac",
                             getattr(self.bus, "master_mac", None)) and ok
            ok = self.kv_set("node.targets",
                             list(getattr(self.bus, "targets", None) or [])) and ok
        except Exception as e:
            dprint(f"[Config] ⚠ save_node 失敗: {e}")
            return False
        if ok:
            self.kv_flush()
        self.publish_node()
        return ok

    def clear_node(self):
        """清掉節點狀態（解除綁定）：回預設 + 刪 btree key + 落盤。"""
        self.bus.role = None
        self.bus.master_cid = 0xFFFF
        self.bus.master_mac = None
        self.bus.pair_claimed = False
        self.bus.targets = []
        for k in self._NODE_KEYS:
            self.kv_del(k)
        self.kv_flush()
        self.publish_node()
        return True

    # ══════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════
    # ESP-NOW 傳輸層設定（2026-10）
    #
    #   為什麼放 btree 而不是 config.json 的 `Network.ESP_now`：
    #     ① **金鑰不能進 config.json** —— 那個人可讀、會被 commit
    #     ② 「已選 peer 清單」是半永久資料，與 `@node.targets` 同性質
    #   命名空間 `@now.*`，與 `@node.*` / `@peer.*` 一致（同一個 secrets.db）。
    #
    #   金鑰（使用者定案）：**全體共用一組** PMK/LMK，不是每個 peer 一組。
    # ══════════════════════════════════════════════════════════════════
    _NOW_KEYS = ("now.selected", "now.encrypt", "now.pmk", "now.lmk")

    def now_state(self):
        """ESP-NOW 傳輸層設定快照（給 UI 讀 / 給持久化寫）。不 raise。

        `selected` = 「已選」的 MAC 清單（大寫 hex，**不含 FF**）。
        空清單 = 只選廣播（FF）—— 那是 UI 的預設，也是「全上」的語意。
        """
        sel = self.kv_get("now.selected", None)
        if not isinstance(sel, list):
            sel = []
        return {
            "selected": [x for x in sel if isinstance(x, str) and x],
            "encrypt": 1 if self.kv_get("now.encrypt", 0) else 0,
            "pmk": self.kv_get("now.pmk", "") or "",
            "lmk": self.kv_get("now.lmk", "") or "",
        }

    def load_now(self):
        """載入 ESP-NOW 設定 → 推到 `bus.shared["now_setting"]`（UI 讀這一份）。"""
        st = self.now_state()
        try:
            self.bus.shared["now_setting"] = st
        except Exception:
            pass
        return st

    def save_now(self, state=None):
        """寫回 btree 並落盤。回 True/False（不 raise）。"""
        st = state or (self.bus.shared.get("now_setting") or {})
        ok = False
        try:
            ok = self.kv_set("now.selected", list(st.get("selected") or []))
            ok = self.kv_set("now.encrypt", 1 if st.get("encrypt") else 0) and ok
            ok = self.kv_set("now.pmk", st.get("pmk") or "") and ok
            ok = self.kv_set("now.lmk", st.get("lmk") or "") and ok
        except Exception as e:
            dprint("[Config] ⚠ save_now 失敗: {}".format(e))
            return False
        if ok:
            self.kv_flush()
        try:
            self.bus.shared["now_setting"] = self.now_state()
        except Exception:
            pass
        return ok

    def clear_now(self):
        """清掉 ESP-NOW 設定（＝UI「清除所有記錄」的持久化那半）。

        ★ 只碰 `@now.*`：節點記錄（`@peer.*`）與配向（`@node.*`）**不動**
          —— 這是使用者定案的範圍。
        """
        for k in self._NOW_KEYS:
            self.kv_del(k)
        self.kv_flush()
        try:
            self.bus.shared["now_setting"] = self.now_state()
        except Exception:
            pass
        return True

    # 模式表（遙控器用）—— 儲存模式 / 整存清單
    #
    #   儲存模式（source）回答一個問題：**這份模式表是誰給的？**
    #     "local"  —— 本機 PixelTask 載入 /pixel/modes/*.json 後覆蓋
    #                  （＝本板是執行端，有自己的模式池）
    #     "remote" —— 從對方查詢取得（0x3102 清單 + 0x3108 逐一細節）
    #                  （＝本板是控制端，自己沒有模式池）
    #     None     —— 還沒有任何來源（開機後、雙方都還沒動作）
    #
    #   為什麼需要它：面板沒有 PixelTask → `pixel_maps` 永遠不存在 →
    #   gmode.mode_pool() 永遠是空的（doc/03_notes/18 §1.1）。面板的清單
    #   必須「從對方取得並存起來」，而 UI 只要讀同一個地方。
    #
    #   流程（定案）：
    #     ① 開機：PixelTask 有跑 → 覆蓋一次（source=local）
    #              沒跑        → 沒人覆蓋（source 保持 remote / None）
    #     ② 指令重新取得：0x3101 → 0x3102（清單）→ set_remote_list()
    #     ③ 逐一取得細節：0x3107 → 0x3108（name）→ set_remote_detail()
    #
    #   ★ 只存兩個 key（**整存，不拆**）：
    #       @mode.source   "local" / "remote"
    #       @mode.list     [{"id": 2, "name": "跑馬燈"}, ...]   ← 完整列表
    #
    #   為什麼整存而不是「一筆一 key」：
    #     - **寫入次數**：逐一取細節時只改記憶體，落盤時整包寫一次（1 次），
    #       拆成一筆一 key 會變成 N+1 次 flash 寫入 —— 整存反而快。
    #     - **原子性**：不會出現「清單更新了、細節只到一半」的半成品。
    #     - **可讀**：一份 JSON 看完，不必拼 key。
    #     清單規模小（數十筆），整包的絕對成本可忽略。
    #
    #   ★ 只有 remote 需要持久化：local 的明細來自 /pixel/modes/*.json，
    #     每次開機必被 PixelTask 重載覆蓋 → 存了也是白存（還會與檔案不同步）。
    #     所以 set_local_modes() 只標 source，並**刪掉** @mode.list。
    #
    #   落盤時機：節流（_mode_save_min_ms，沿用 PeerRegistry 的做法）——
    #     逐一取細節期間最多每 2 秒寫一次；最後一筆由 flush_modes() 補寫
    #     （掛在 BusDecodeTask.housekeep，與 peers 同一個模式）。
    #   執行期發布：bus.shared["mode_table"]（UI 讀這一份）
    # ══════════════════════════════════════════════════════════════════

    def mode_source(self):
        """儲存模式：'local' / 'remote' / None。"""
        return self.kv_get("mode.source", None)

    def mode_list(self):
        """完整模式列表（副本）：[{"id":.., "name":..}, ...]（有序）。"""
        return [dict(e) for e in self._mode_list]

    def mode_ids(self):
        """模式 id 清單（有序）。"""
        return [e["id"] for e in self._mode_list]

    def mode_detail(self, mid):
        """單一模式的細節（沒有回 {}）。"""
        mid = int(mid)
        for e in self._mode_list:
            if e["id"] == mid:
                return dict(e)
        return {}

    def mode_table(self):
        """給 UI 的合併表：source + 每一筆 {id, hex, name}。"""
        out = [{"id": e["id"], "hex": "0x{:04X}".format(e["id"]),
                "name": e.get("name", "")} for e in self._mode_list]
        return {"source": self.kv_get("mode.source", None),
                "count": len(out), "entries": out}

    def publish_modes(self):
        """把模式表推上 bus.shared["mode_table"]（UI / 其他 task 讀同一份）。"""
        try:
            self.bus.shared["mode_table"] = self.mode_table()
        except Exception:
            pass

    def set_mode_source(self, src, flush=True):
        """設定儲存模式。回 True/False。"""
        self._mode_dirty = True
        ok = self.kv_set("mode.source", src)
        if ok and flush:
            self.flush_modes(force=True)
        self.publish_modes()
        return ok

    def flush_modes(self, force=False):
        """把整份模式列表落盤（節流；只寫有變更的）。

        `force=True` 繞過節流。`_mode_dirty` 為 False 時直接跳過
        （沒東西可寫就不寫 —— 與 PeerRegistry.save 同一語意）。
        """
        if not self._mode_dirty:
            return False
        now = _cfg_now()
        if not force and self._mode_last_save and \
                (now - self._mode_last_save) < self._mode_save_min_ms:
            return False
        kv = self.kv_set("mode.list", self._mode_list)
        if kv:
            self._mode_dirty = False
            self._mode_last_save = now
            self.kv_flush()
        self.publish_modes()
        return bool(kv)

    # ── 寫入來源①：本機 PixelTask（開機覆蓋一次）──────────────
    def set_local_modes(self, modes):
        """本機 PixelTask 載入模式後呼叫：source=local，覆蓋記憶體清單。

        `modes` = {id: mode_dict}（PixelTask._init_modes 的產物）。
        ★ 不把清單寫進 btree —— 事實來源是 /pixel/modes/*.json，每次開機都會
          重載；寫進去只會多一份會不同步的副本。
        ★ 會刪掉舊的 @mode.list（來源切換 → 舊清單失效）。
        """
        try:
            ids = sorted(int(k) for k in (modes or {}).keys())
        except Exception:
            ids = []
        lst = []
        for mid in ids:
            m = (modes or {}).get(mid) or (modes or {}).get(str(mid)) or {}
            lst.append({"id": mid, "name": m.get("name", "")})
        self._mode_list = lst
        self._mode_dirty = False          # local 不持久化清單
        self.kv_del("mode.list")          # 舊的 remote 清單失效
        self.kv_set("mode.source", "local")
        self.kv_flush()
        self.publish_modes()
        dprint("[Config] ✓ modes(local): {} 個".format(len(lst)))

    # ── 寫入來源②：遠端查詢（0x3102 清單）──────────────────
    def set_remote_list(self, ids):
        """收到 0x3102 模式清單後呼叫：**整份替換**，source=remote，立即落盤。

        細節（name）先留空，等 set_remote_detail() 逐一補上（只改記憶體）。
        """
        try:
            new_ids = sorted(int(i) for i in (ids or []))
        except Exception:
            new_ids = []
        self._mode_list = [{"id": i, "name": ""} for i in new_ids]
        self._mode_dirty = True
        self.kv_set("mode.source", "remote")
        self.flush_modes(force=True)      # 清單是整份替換 → 直接落地
        self.publish_modes()
        dprint("[Config] ✓ modes(remote): {} 個".format(len(new_ids)))
        return True

    # ── 寫入來源②：遠端查詢（0x3108 逐一細節）──────────────
    def set_remote_detail(self, mid, name):
        """收到 0x3108 單一模式細節後呼叫：**只改記憶體**，落盤交給節流。

        逐一取得 N 筆細節時，這裡不會產生 N 次 flash 寫入 ——
        最後由 flush_modes()（housekeep）整包寫一次。
        """
        mid = int(mid)
        found = False
        for e in self._mode_list:
            if e["id"] == mid:
                e["name"] = name or ""
                found = True
                break
        if not found:
            # 不在清單裡也記下來（避免細節遺失），依 id 排序插入
            self._mode_list.append({"id": mid, "name": name or ""})
            self._mode_list.sort(key=lambda x: x["id"])
        self._mode_dirty = True
        self.flush_modes()                # 節流：2 秒內只寫一次
        self.publish_modes()
        return True

    # ── 載入 / 清空 ───────────────────────────────────────
    def load_modes(self):
        """開機從 btree 載入模式表（load_setup 尾端呼叫）。

        只有 remote 的清單在 btree；local 的那份會在 PixelTask 啟動時覆蓋。
        所以開機後 UI 可能先看到「上次的 remote 清單」，等 PixelTask 起來才換掉。
        """
        raw = self.kv_get("mode.list", None) or []
        lst = []
        if isinstance(raw, list):
            for e in raw:
                if isinstance(e, dict) and "id" in e:
                    try:
                        lst.append({"id": int(e["id"]), "name": e.get("name", "")})
                    except Exception:
                        continue
        self._mode_list = lst
        self._mode_dirty = False
        self.publish_modes()
        src = self.kv_get("mode.source", None)
        if src:
            dprint("[Config] ✓ modes loaded: source={} count={}".format(src, len(lst)))

    def clear_modes(self):
        """清空模式表（含 btree）：連 source 一起清，回到「還沒有來源」。"""
        self._mode_list = []
        self._mode_dirty = False
        self.kv_del("mode.list")
        self.kv_del("mode.source")
        self.kv_flush()
        self.publish_modes()
        return True

    def _update_value_preserve_format(self, key_path, new_value):
        """
        嘗試在不破壞格式的情況下更新單個值。
        支援路徑如 "System.c_lum" 或 "Network.wifi.ssid"。
        支援基礎類型 (str, int, float, bool, null) 以及 dict/list (強制轉為單行緊湊 JSON)。
        """
        try:
            with open(self.path, 'r') as f:
                content = f.read()
            
            # 分割路徑
            keys = key_path.split('.')
            
            # 狀態機變量
            i = 0
            n = len(content)
            current_depth = 0
            key_idx = 0
            target_key = keys[key_idx]
            
            # 尋找目標值的起始與結束位置
            value_start = -1
            value_end = -1
            
            while i < n:
                c = content[i]
                
                if c == '"':
                    # 處理字串
                    start = i
                    i += 1
                    while i < n:
                        if content[i] == '"' and content[i-1] != '\\':
                            break
                        i += 1
                    # content[start:i+1] 是完整的引號包圍字串
                    token_str = content[start+1:i] # 去掉引號
                    i += 1 # 移過結尾引號
                    
                    # 檢查是否為當前層級的目標鍵
                    # 尋找下一個非空白字符是否為冒號
                    j = i
                    while j < n and content[j] in ' \t\r\n':
                        j += 1
                    
                    if j < n and content[j] == ':':
                        #這是一個鍵
                        if token_str == target_key:
                            # 找到了當前層級的鍵
                            i = j + 1 # 移過冒號
                            
                            # 如果這是最後一個鍵，準備定位值
                            if key_idx == len(keys) - 1:
                                # 跳過空白找到值的開始
                                while i < n and content[i] in ' \t\r\n':
                                    i += 1
                                value_start = i
                                
                                # 確定值的結束位置
                                # 我們需要根據值的類型來正確跳過它
                                if i < n:
                                    if content[i] == '"':
                                        i += 1
                                        while i < n:
                                            if content[i] == '"' and content[i-1] != '\\':
                                                break
                                            i += 1
                                        i += 1
                                    elif content[i] in 'tf': # true/false
                                        while i < n and content[i] in 'truefalse':
                                            i += 1
                                    elif content[i] == 'n': # null
                                        while i < n and content[i] in 'null':
                                            i += 1
                                    elif content[i] in '-0123456789': # number
                                        while i < n and content[i] in '-0123456789.eE':
                                            i += 1
                                    elif content[i] == '{':
                                        # 跳過物件區塊
                                        depth = 1
                                        i += 1
                                        while i < n and depth > 0:
                                            if content[i] == '{': depth += 1
                                            elif content[i] == '}': depth -= 1
                                            elif content[i] == '"': # 跳過字串以免誤判大括號
                                                i += 1
                                                while i < n:
                                                    if content[i] == '"' and content[i-1] != '\\':
                                                        break
                                                    i += 1
                                            i += 1
                                    elif content[i] == '[':
                                        # 跳過陣列區塊
                                        depth = 1
                                        i += 1
                                        while i < n and depth > 0:
                                            if content[i] == '[': depth += 1
                                            elif content[i] == ']': depth -= 1
                                            elif content[i] == '"': # 跳過字串
                                                i += 1
                                                while i < n:
                                                    if content[i] == '"' and content[i-1] != '\\':
                                                        break
                                                    i += 1
                                            i += 1
                                
                                value_end = i
                                # 找到目標，跳出循環
                                break
                            else:
                                # 還有下一層，繼續
                                key_idx += 1
                                target_key = keys[key_idx]
                                # 這裡不需要特殊處理，只要繼續掃描即可
                                pass
                        else:
                            # 不是目標鍵，跳過它的值
                            i = j + 1
                            # 跳過空白
                            while i < n and content[i] in ' \t\r\n':
                                i += 1
                            # 簡單跳過值（不支援嵌套結構的完整跳過，這裡有風險）
                            pass

                elif c == '{' or c == '[':
                    current_depth += 1
                    i += 1
                elif c == '}' or c == ']':
                    current_depth -= 1
                    i += 1
                else:
                    i += 1
            
            if value_start != -1 and value_end != -1:
                # 執行替換
                
                # 特殊處理：如果是 dict 或 list，強制轉為單行 JSON
                # 這滿足使用者的需求：「當我update字典的時候,直接轉換為json一行過」
                if isinstance(new_value, (dict, list)):
                    # 使用 separators=(',', ':') 來產生最緊湊的 JSON (無空白)
                    # MicroPython 的 json.dumps 可能不支援 separators，
                    # 但默認 dumps 出來的通常就是緊湊的或者帶空格的單行
                    new_value_str = json.dumps(new_value)
                    
                    # 確保它是單行的 (移除可能的換行符，雖然 dumps 預設通常不換行除非有 indent)
                    new_value_str = new_value_str.replace('\n', '').replace('\r', '')
                    
                    dprint(f"[Config] 📦 結構化更新 (單行模式): {key_path}")
                else:
                    # 基礎類型
                    new_value_str = json.dumps(new_value)
                
                # 構建新內容
                new_content = content[:value_start] + new_value_str + content[value_end:]
                
                with open(self.path, 'w') as f:
                    f.write(new_content)
                # 🔧 sync 落盤：無損更新後若緊接著 machine.reset()（例如 WDT re-arm），
                #    未 sync 的寫入會被丟掉 → enable=1 沒存進 → 下次開機又 enable=0
                #    → 60s 沉默又 re-arm → 無限重啟循環。
                if hasattr(os, 'sync'):
                    os.sync()
                dprint(f"[Config] ✓ 無損更新成功: {key_path}")
                return True
            else:
                dprint(f"[Config] ⚠ 無損更新失敗：找不到路徑 {key_path}")
                return False

        except Exception as e:
            dprint(f"[Config] ⚠ 無損更新異常: {e}")
            return False

    def save_from_bus(self, update_key=None):
        """
        持久化：自動隱藏密碼，並徹底忽略 _obj 對象。
        如果指定了 update_key (例如 "Network.wifi.ssid")，嘗試進行無損更新。
        """
        # 1. 如果有指定 update_key，且不是複雜結構，嘗試無損更新
        if update_key:
            # 從 bus.shared 獲取新值
            try:
                keys = update_key.split('.')
                val = self.bus.shared
                for k in keys:
                    val = val[k]
                
                # 嘗試無損寫入
                if self._update_value_preserve_format(update_key, val):
                    # 同步 BTree (確保密碼等也被更新，雖然無損更新通常不是更密碼)
                    self._update_btree_only(self.bus.shared)
                    self._db.flush()
                    return
            except Exception as e:
                dprint(f"[Config] 獲取新值失敗，回退全面保存: {e}")
        
        # 2. 如果無損更新失敗或未指定 key，執行標準保存（會重置縮排但保留順序）
        #    寫入 tmp 再 rename，避免中途失敗把 config.json 寫壞（只保留 config 段，
        #    runtime 全域如 _vbtn/_hw_inputs 等非 config key 本來就不該進 config 檔）。
        tmp_path = self.path + ".tmp"
        try:
            config_only = {k: v for k, v in self.bus.shared.items()
                           if isinstance(k, str) and not k.startswith("_")}
            with open(tmp_path, 'w') as f:
                self._pretty_dump(config_only, f)
            # ⚠️ MicroPython 的 os 模組**沒有 replace()**（只有 rename）。
            #    原本直接呼叫 os.replace 會讓整個「標準保存」失敗，而且只印一行
            #    「✗ 保存出錯」—— 無損更新（update_key）以外的存檔全部無效。
            #    （router_board_test.py §5 在板上抓到；自動註冊寫回 config 就靠這條路。）
            _rep = getattr(os, "replace", None)
            if _rep is not None:
                _rep(tmp_path, self.path)
            else:
                os.rename(tmp_path, self.path)
            # 🔧 sync 落盤：標準保存也一樣，避免 rename 後立刻 reset 丟寫入
            if hasattr(os, 'sync'):
                os.sync()
            self._update_btree_only(self.bus.shared)
            self._db.flush()
            dprint(f"[Config] ✓ 配置已同步 (已自動忽略 _obj 對象)")
        except Exception as e:
            try:
                if os.stat(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            dprint(f"[Config] ✗ 保存出錯: {e}")

    def _update_btree_only(self, node, prefix=""):
        """單純提取密碼到 BTree，不處理 JSON"""
        if isinstance(node, dict):
            for k, v in node.items():
                if not isinstance(k, str):
                    continue  # bus.shared 是開放全域，task 可能塞非字串 key，跳過避免崩潰
                db_key = f"{prefix}{k}"
                if k.endswith('_pw'):
                    self._db[db_key.encode()] = json.dumps(v).encode()
                elif isinstance(v, (dict, list)):
                    self._update_btree_only(v, prefix=db_key + ".")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                self._update_btree_only(item, prefix=f"{prefix}{i}.")

    # ══════════════════════════════════════════════════════════════════
    # 半永久 KV（btree 門面）
    #
    #   用途：放「不屬於 config、但斷電要留著」的東西 —— 節點表、身份/角色/
    #         目標、配對關係……這些「一堆列表的半永久資料」。
    #
    #   命名空間：`@` 開頭 = 系統保留。
    #     既有兩個使用者都不會撞：
    #       config 路徑樣式 → 大寫開頭（"Network.wifi.ssid_pw"）
    #       network_manager → "wifi_credentials"
    #     所以 `@node.role` / `@peer.<SID>` 一眼看出不是 config 來的。
    #
    #   格式：key → JSON bytes（與既有兩個使用者一致）
    #
    #   ★ 為什麼不另開檔案：btree 的價值在**逐 key 更新**（改一個節點不用
    #     重寫整個檔）＋ 內容損壞有 rollback 保護。`/peers.json` 那種
    #     「整個檔重寫」的做法在節點變多時會痛；而它唯一的優點（人可讀）
    #     可以用 kv_keys() + snapshot 印出來補。
    #
    #   ⚠️ 寫入後**不會自動落盤** —— 呼叫端自行 kv_flush()，或等 housekeep。
    # ══════════════════════════════════════════════════════════════════

    _KV_NS = "@"        # 系統保留前綴（不屬於 config 的 key）

    def _kv_key(self, key):
        """key → 實際 btree key（bytes，自動補命名空間）。"""
        k = key if isinstance(key, bytes) else str(key).encode()
        return k if k.startswith(self._KV_NS.encode()) else self._KV_NS.encode() + k

    def kv_set(self, key, obj):
        """寫一筆半永久資料（obj 會被 JSON 編碼）。回 True/False（不 raise）。"""
        if self._db is None:
            return False
        try:
            self._db[self._kv_key(key)] = json.dumps(obj).encode()
            return True
        except Exception as e:
            dprint(f"[Config] kv_set({key}) 失敗: {e}")
            return False

    def kv_get(self, key, default=None):
        """讀一筆並 JSON 解碼。不存在或壞掉 → 回 default（不 raise）。"""
        if self._db is None:
            return default
        try:
            raw = self._db.get(self._kv_key(key))
            if raw is None:
                return default
            return json.loads(raw.decode())
        except Exception as e:
            dprint(f"[Config] kv_get({key}) 失敗: {e}")
            return default

    def kv_del(self, key):
        """刪一筆。回 True=真的刪了，False=本來就沒有或失敗。"""
        if self._db is None:
            return False
        try:
            k = self._kv_key(key)
            if self._db.get(k) is None:
                return False
            del self._db[k]
            return True
        except Exception as e:
            dprint(f"[Config] kv_del({key}) 失敗: {e}")
            return False

    def kv_keys(self, prefix=""):
        """列出命名空間下（可再篩前綴）的所有 key，回傳**不含 '@'** 的字串清單。

        例：kv_keys("peer.") → ['peer.A0B1C2D3E4F5', ...]
        btree 的 key 是排序的，所以同前綴的鍵是連續的；DB 很小，直接走訪即可。
        """
        out = []
        if self._db is None:
            return out
        try:
            want = self._kv_key(prefix)
            ks = self._db.keys() if hasattr(self._db, "keys") else list(self._db)
            ns = self._KV_NS
            for k in ks:
                if k.startswith(want):
                    out.append(k[len(ns):].decode())
        except Exception as e:
            dprint(f"[Config] kv_keys({prefix}) 失敗: {e}")
        return sorted(out)

    def kv_flush(self):
        """把累積的 KV 變更落盤。回 True/False。"""
        if self._db is None:
            return False
        try:
            self._db.flush()
            return True
        except Exception as e:
            dprint(f"[Config] kv_flush 失敗: {e}")
            return False

    # ══════════════════════════════════════════════════════════════════
    # config 路徑存取（執行期「讀 / 寫 / 逐 key 存」的統一入口）
    #
    #   為什麼要這一層：三段路本來只有兩段有統一入口 ——
    #     讀   載入時整棵樹灌進 bus.shared              ✅ 有（load_setup）
    #     寫   值 → bus.shared                          ❌ 沒有，散在 30+ 處各寫各的
    #     存   bus.shared → config.json（逐 key 無損）  ✅ 有（save_from_bus）
    #   這一層補的就是中間那段，並把三段收斂成同一組 key（點分路徑）。
    #
    #   ★ key 一律是**點分路徑**，與 save_from_bus(update_key=) 同一種寫法：
    #       "Network.ESP_now.enable"        → 葉節點
    #       "System.watchdog"               → 整個子樹
    #
    #   ⚠️ 這一層只碰 config（bus.shared），**不碰 btree KV** ——
    #      KV 有自己的門面（kv_get/kv_set，`@` 命名空間），用途不同。
    # ══════════════════════════════════════════════════════════════════

    # 遠端改壞會很難查的紅線（改得到，但會回警告）。
    #   判準：這幾條的值錯了，症狀會離「有人改了設定」很遠 ——
    #     看門狗 → 裝置自己重啟；螢幕方向 → 畫面 double-rotate。
    _REDLINE = {
        "System.watchdog.auto_rearm_ms":
            "非 0 會讓裝置在沉默 N ms 後自動開 WDT + 重啟",
        "TFT.rotation":
            "非 0 會 double-rotate（lvgl_init 自己送 MADCTL）",
    }

    def _path_parts(self, key):
        """key → 路徑片段清單。空 key / 非字串 → 空清單。"""
        if not key or not isinstance(key, str):
            return []
        return [p for p in key.split(".") if p != ""]

    def get_by_path(self, key, default=None):
        """用點分路徑讀 bus.shared（config 的執行期快取）。讀不到回 default。

        與 save_from_bus(update_key=) 對稱：那邊是「讀快取 → 寫檔」，
        這邊是「讀快取 → 給呼叫端」。
        """
        parts = self._path_parts(key)
        if not parts:
            return default
        cur = self.bus.shared
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur

    def set_by_path(self, key, value):
        """用點分路徑寫 bus.shared。回 (ok, message)。

        ★ 只寫**快取**（立即生效），不落盤 —— 落盤是 save_keys() 的事。
          這是刻意的分工：「改值」與「存檔」分開，因為
            ① 落盤是 flash 寫入，逐次存會把調參數變成大量抹寫
            ② 先改快取測效果、確定了再存，是正常的使用流程
          （改值 vs 落盤本來就是兩件事，不該綁在一起）

        中間層不存在時自動建 dict。中途遇到非 dict（路徑撞到葉節點）→ 拒絕，
        不覆蓋既有資料。
        """
        parts = self._path_parts(key)
        if not parts:
            return False, "空路徑"
        cur = self.bus.shared
        for p in parts[:-1]:
            nxt = cur.get(p)
            if nxt is None:
                nxt = {}
                cur[p] = nxt
            elif not isinstance(nxt, dict):
                return False, "路徑衝突：{} 已經是值，不是子樹".format(p)
            cur = nxt
        cur[parts[-1]] = value
        return True, ""

    # 落盤結果代碼（給 0x1104 STATUS_ACK.message 逐 key 回報用）
    SAVE_OK = "ok"            # 無損更新（逐字元就地替換，排版保留）
    SAVE_REWRITTEN = "rewrite"  # 無損更新失敗 → 退回整檔重寫（排版會重排）
    SAVE_SKIPPED = "skip"     # 快取裡沒這個路徑 → 不存

    def save_keys(self, keys):
        """逐 key 落盤。回 [(key, 結果, 說明)]，**不 raise**。

        ★ 逐 key 呼叫 save_from_bus(update_key=)，而不是整檔存一次：
          `_update_value_preserve_format` 是「在原始檔文字裡只換掉那一段」，
          所以 N 個 key = N 次獨立的小改動，**排版每次都保留**。
          反之不帶 update_key 的整檔存會 `_pretty_dump` 重排全部。

        ⚠️ 無損更新失敗時 save_from_bus 會**退回整檔重寫**（排版被重排）。
          那個副作用無法從 save_from_bus 的回傳值看出來，所以這裡先自己
          檢查一次路徑存不存在 —— 路徑不存在就跳過，不要讓它去觸發整檔重寫。
          回報裡用 SAVE_REWRITTEN 標記「這次可能重排了」。
        """
        out = []
        if not keys:
            return out
        for k in keys:
            if not self._path_parts(k):
                out.append((k, self.SAVE_SKIPPED, "空路徑"))
                continue
            if self.get_by_path(k, _MISSING) is _MISSING:
                out.append((k, self.SAVE_SKIPPED, "快取裡沒有這個路徑"))
                continue
            before = None
            try:
                # 記下檔案內容，用「有沒有整檔級別的變動」判斷是否走了重寫路徑
                with open(self.path, 'r') as f:
                    before = f.read()
            except Exception:
                pass
            try:
                self.save_from_bus(update_key=k)
            except Exception as e:
                out.append((k, self.SAVE_SKIPPED, "例外: {}".format(e)))
                continue
            if before is not None:
                try:
                    with open(self.path, 'r') as f:
                        after = f.read()
                    # 無損更新只動一個值 → 行數不變；整檔重寫會重排縮排
                    if after.count("\n") != before.count("\n"):
                        out.append((k, self.SAVE_REWRITTEN,
                                    "無損更新失敗，已整檔重寫（排版重排）"))
                        continue
                except Exception:
                    pass
            out.append((k, self.SAVE_OK, ""))
        return out

    def config_snapshot(self, root=None, max_depth=99):
        """回一份可安全公開的 config 快照（給 0x1101 STATUS_GET 用）。

        ★ 必須剝離兩種東西，否則等於把密碼送上網路：
            `_` 開頭   —— 執行期全域（_vbtn / _hw_inputs / _core_buf…），不是 config
            `_pw` 結尾 —— 密碼。存檔時 `_clean_passwords_preserve_format` 會把它
                          從 config.json 移除、改存 btree；但**開機時 sync_node 又把它
                          還原進 bus.shared**，所以記憶體裡是有值的。
        這兩條與 save_from_bus() 的過濾規則一致（同一套判準，不重複定義）。

        root 給了就只回那個子樹（找不到回 None）。
        """
        src = self.bus.shared if root is None else self.get_by_path(root, None)
        if src is None and root is not None:
            return None

        def _clean(node, depth):
            if isinstance(node, dict):
                out = {}
                for k, v in node.items():
                    if not isinstance(k, str):
                        continue
                    if k.startswith("_") or k.endswith("_pw"):
                        continue
                    if depth >= max_depth:
                        continue
                    out[k] = _clean(v, depth + 1)
                return out
            if isinstance(node, list):
                return [_clean(v, depth + 1) for v in node]
            return node

        return _clean(src, 0)

    def config_keys(self, root=None):
        """可公開的 config key 路徑清單（給 0x1101 的 `?` 目錄查詢用）。"""
        snap = self.config_snapshot(root)
        out = []

        def _walk(node, prefix):
            if not isinstance(node, dict):
                return
            for k in sorted(node.keys()):
                path = k if not prefix else prefix + "." + k
                out.append(path)
                if isinstance(node[k], dict):
                    _walk(node[k], path)

        _walk(snap or {}, "")
        return out

    def close(self):
        if self._db: self._db.close()
        if self._f: self._f.close()
        
cfg_manager = ConfigManager(bus)
cfg_manager.load_setup()


