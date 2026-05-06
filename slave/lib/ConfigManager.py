import btree
import json
import os
from lib.dispatch import dprint
from lib.sys_bus import bus

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
            
            # 過濾掉所有以 _obj 結尾的鍵
            current_keys = [k for k in obj.keys() if not k.endswith('_obj')]
            
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
        try:
            with open(self.path, 'w') as f:
                self._update_btree_only(self.bus.shared)
                self._pretty_dump(self.bus.shared, f)
            self._db.flush()
            dprint(f"[Config] ✓ 配置已同步 (已自動忽略 _obj 對象)")
        except Exception as e:
            dprint(f"[Config] ✗ 保存出錯: {e}")

    def _update_btree_only(self, node, prefix=""):
        """單純提取密碼到 BTree，不處理 JSON"""
        if isinstance(node, dict):
            for k, v in node.items():
                db_key = f"{prefix}{k}"
                if k.endswith('_pw'):
                    self._db[db_key.encode()] = json.dumps(v).encode()
                elif isinstance(v, (dict, list)):
                    self._update_btree_only(v, prefix=db_key + ".")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                self._update_btree_only(item, prefix=f"{prefix}{i}.")

    def close(self):
        if self._db: self._db.close()
        if self._f: self._f.close()
        
cfg_manager = ConfigManager(bus)
cfg_manager.load_setup()


