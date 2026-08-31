import os
import time
import ujson
import ubinascii
import hashlib
from lib.sys.dispatch import dprint

# ── 分批驗證的讓步鉤子 ─────────────────────────────────────────
# 全檔 SHA 讀取迴圈 (_finalize_atomic_write / calc_sha256) 每 ~256KB 讓出
# 控制權一次，避免大檔重讀在 core0 上同步阻塞超過看門狗 timeout。
# 鉤子預設 no-op；由啟動方 (TaskManager.runner_loop core0) 依看門狗策略注入
# 回呼 —— 模組本身不知道 WDT 存在，不會與看門狗耦合。
_yield_cb = None


def set_yield_cb(cb):
    """注入讓步回呼 (None = 恢復 no-op)。回呼執行頻率 ≈ 每 256KB 一次。"""
    global _yield_cb
    _yield_cb = cb


def yield_point():
    """全檔讀取迴圈的讓步點: 呼叫注入的回呼 (若有的話)。"""
    global _yield_cb
    cb = _yield_cb
    if cb is not None:
        try:
            cb()
        except Exception:
            pass

MANIFEST_FILE = "/manifest.json"          # 本地 flash 的 manifest
MANIFEST_FILE_SD = "/sd/.manifest.json"   # SD 的 manifest (與本地分開存放)
DELTA_FILE = "/sd/.delta.json"            # 兩段式 commit + 斷點續傳 journal (存 SD)

class FileSystemManager:
    """
    Unified File System Manager
    Responsibilities:
    1. Atomic File Write (write to .tmp -> verify -> rename)
    2. Manifest Management (load, save, update)
    3. Background Scanning (Core 1)
    4. File Reception Logic (replacing FileRx)
    """
    def __init__(self):
        self.manifest_local = {}   # 本地 flash 檔案 (存 /manifest.json)
        self.manifest_sd = {}      # SD 檔案 (存 /sd/.manifest.json)
        self.delta = {"partial": {}, "pending": {}}  # 兩段式 commit + 斷點續傳
        self.scanning = False
        self._scan_files = []
        self._scan_manifest = {}
        self._scan_idx = 0

        # === 統一資料層 (RAM / SD-raw / FAT) ===
        # RAM cache：路徑前綴 /ram 的暫存區 (斷電消失)
        self._ram = {}

        # 初始化時檢查 alloc.json 決定讀寫模式
        #   alloc.json 存在 → raw 高速模式 (fast_io.Storage)
        #   alloc.json 不存在 → FAT 模式 (os.open/readinto)
        self._raw_mode = False
        self._raw = None
        try:
            os.stat("/sd/alloc.json")
            from lib.sys.sys_bus import bus
            if bus.get_service("sd_raw") is not None:
                from lib.sys.fast_io import Storage
                self._raw = Storage()
                self._raw_mode = True
                print("✅ [FS] SD-raw backend ready (alloc.json found)")
        except Exception:
            pass
        if not self._raw_mode:
            print("📂 [FS] FAT mode (alloc.json not found)")

        # Session State for File Upload
        self.session = {
            "active": False,
            "path": None,
            "temp_path": None,
            "fp": None,
            "file_id": 0,
            "written": 0,
            "total_size": 0,
            "sha_expect_hex": None,
            "last_error": None,
            "last_sha_hex": "",
            "last_pending": 0,
            "ram_buf": None   # /ram 分塊上傳: 拼 chunk 用的 bytearray (非 ram 時為 None)
        }

        # 串流讀取狀態
        self._str_kind = None

        self.load_manifest()

        # 開機最高優先：pending 備份 boots+1，滿 3 次未確認 → 自動還原 .bak
        self._boot_recovery_check()

    def load_manifest(self):
        try:
            with open(MANIFEST_FILE, "r") as f:
                self.manifest_local = ujson.load(f)
            print(f"📦 [FS] Local manifest loaded: {len(self.manifest_local)} files")
        except:
            print("⚠️ [FS] Local manifest missing or corrupt, starting scan...")
            self.manifest_local = {}
            # Start background scan if manifest is missing
            self.scan_all()

        try:
            with open(MANIFEST_FILE_SD, "r") as f:
                self.manifest_sd = ujson.load(f)
            print(f"📦 [FS] SD manifest loaded: {len(self.manifest_sd)} files")
        except:
            print("⚠️ [FS] SD manifest missing or corrupt (filled on demand)")
            self.manifest_sd = {}

        self._load_delta()

    def _load_scan_ignore(self):
        prefixes = []
        try:
            with open("/config.json", "r") as f:
                cfg = ujson.load(f)
            raw = cfg.get("scan_ignore")
            if raw is None:
                raw = cfg.get("fs", {}).get("scan_ignore")
            if isinstance(raw, list):
                for p in raw:
                    p = str(p).rstrip("/")
                    if p:
                        prefixes.append(p)
        except Exception:
            pass
        return prefixes

    def _is_ignored(self, path, prefixes):
        for p in prefixes:
            if path == p or path.startswith(p + "/"):
                return True
        return False

    def _manifest_target(self, path):
        """依路徑回傳 (manifest_dict, full_path, manifest_file)。"""
        kind, full, _ = self.resolve(path)
        if kind == "sd":
            return self.manifest_sd, full, MANIFEST_FILE_SD
        return self.manifest_local, full, MANIFEST_FILE

    def manifest_lookup(self, path):
        """依路徑查對應的 manifest 條目。回傳 (entry_or_None, full_path)。"""
        d, full, _ = self._manifest_target(path)
        return d.get(full), full

    def manifest_lookup_abs(self, path):
        """直接用「絕對路徑」查 manifest，不做 resolve 映射。

        供 FILE_PROMOTE（落根目錄 /xxx）與其查詢/還原使用：根目錄檔的 manifest
        鍵就是 /xxx 本身，走 resolve 會被誤映射成 /sd/xxx。"""
        p = "/" + str(path).lstrip("/")
        if p == "/sd" or p.startswith("/sd/"):
            return self.manifest_sd.get(p), p
        return self.manifest_local.get(p), p

    def _manifest_target_abs(self, path):
        """直接用「絕對路徑」判斷 manifest 落點（不 resolve）。

        根目錄檔（/xxx）→ manifest_local（/manifest.json）；
        /sd 檔（/sd/xxx）→ manifest_sd（/sd/.manifest.json）。
        與 _manifest_target 的差別：不把 /xxx 映射成 /sd/xxx。"""
        p = "/" + str(path).lstrip("/")
        if p == "/sd" or p.startswith("/sd/"):
            return self.manifest_sd, p, MANIFEST_FILE_SD
        return self.manifest_local, p, MANIFEST_FILE

    def remove_abs(self, path):
        """依「絕對路徑」刪除（不 resolve），並更新對應 manifest。

        供 FILE_DELETE 刪除根目錄檔（/xxx）使用：resolve() 會把 /xxx 誤映射成
        /sd/xxx，導致刪錯檔 + 更新錯 manifest。"""
        p = "/" + str(path).lstrip("/")
        ok = False
        if self._raw_mode and self._raw is not None and not p.startswith("/sd/"):
            raw_name = p.lstrip("/")
            try:
                if self._raw._alloc.find(raw_name) is not None:
                    self._raw.remove(raw_name)
                    ok = True
            except Exception:
                pass
        if self.delete_file(p):
            ok = True
        return ok

    def _write_manifest(self, mfile, d):
        try:
            with open(mfile, "w") as f:
                # Custom Pretty Dump for Manifest
                f.write("{\n")
                # Sort keys for consistent order
                keys = sorted(d.keys())
                for i, k in enumerate(keys):
                    entry = d[k]
                    key_str = ujson.dumps(k)
                    entry_str = ujson.dumps(entry)

                    f.write(f'    {key_str}: {entry_str}')

                    if i < len(keys) - 1:
                        f.write(",\n")
                    else:
                        f.write("\n")
                f.write("}")
            # 🔧 落盤後立即 sync：否則軟重啟 (machine.reset) 可能丟掉剛寫的 manifest，
            #    下次「下載 manifest 比對」就會拿到過期哈希表 → 一直顯示需要更新。
            if hasattr(os, 'sync'):
                os.sync()
        except Exception as e:
            print(f"❌ [FS] Save manifest failed: {e}")

    def save_manifest(self):
        """兩份 manifest 各自落盤。"""
        self._write_manifest(MANIFEST_FILE, self.manifest_local)
        self._write_manifest(MANIFEST_FILE_SD, self.manifest_sd)

    def update_manifest_entry(self, path, size, sha_hex):
        d, full, mfile = self._manifest_target_abs(path)
        d[full] = {
            "s": size,
            "h": sha_hex
        }
        self._write_manifest(mfile, d)

    def remove_manifest_entry(self, path):
        d, full, mfile = self._manifest_target_abs(path)
        if full in d:
            del d[full]
            self._write_manifest(mfile, d)

    # ==================== Delta Journal (SD) ====================
    # 存 /sd/.delta.json，兩段:
    #   partial — 傳輸中 (斷點續傳用): {path: {tmp, total_size, sha256}}
    #   pending — 已覆蓋待確認:        {path: {bak, old_sha, old_size, new_sha}}

    def _load_delta(self):
        try:
            with open(DELTA_FILE, "r") as f:
                d = ujson.load(f)
            self.delta = {
                "partial": d.get("partial", {}),
                "pending": d.get("pending", {})
            }
            print(f"🧾 [FS] Delta loaded: {len(self.delta['partial'])} partial, "
                  f"{len(self.delta['pending'])} pending")
        except Exception:
            self.delta = {"partial": {}, "pending": {}}

    def _save_delta(self):
        try:
            self._ensure_parent(DELTA_FILE)
            with open(DELTA_FILE, "w") as f:
                f.write(ujson.dumps(self.delta))
            # 🔧 sync 落盤：pending/partial 是回滾與斷點續傳的權威紀錄，丟了等於失去保護
            if hasattr(os, 'sync'):
                os.sync()
        except Exception as e:
            print(f"❌ [FS] Save delta failed: {e}")

    # ==================== File Reception Logic ====================
    
    def _close_session(self):
        if self.session["fp"]:
            try:
                self.session["fp"].flush()
                if hasattr(os, 'sync'): os.sync()
                self.session["fp"].close()
            except:
                pass
        self.session["fp"] = None

    def _resume_match(self, temp_path, path, total_size, sha_hex):
        """判斷現有 .tmp 是否可續傳: .tmp 存在且 delta.partial 身分一致。"""
        try:
            os.stat(temp_path)
        except Exception:
            return False
        rec = self.delta.get("partial", {}).get(path)
        if not rec:
            return False
        return (rec.get("total_size") == total_size
                and rec.get("sha256") == sha_hex)

    def begin_write(self, args: dict) -> bool:
        """FILE_BEGIN (0x2001)"""
        self._close_session()

        path = args.get("path")
        file_id = int(args.get("file_id", 0))
        total_size = int(args.get("total_size", 0))

        sha_bytes = args.get("sha256")
        sha_expect_hex = ubinascii.hexlify(sha_bytes).decode() if sha_bytes else None

        # Reset Session
        self.session.update({
            "active": False,
            "path": path,
            "file_id": file_id,
            "written": 0,
            "total_size": total_size,
            "sha_expect_hex": sha_expect_hex,
            "last_error": None,
            "last_pending": 0,
            "ram_buf": None
        })

        if not path:
            self.session["last_error"] = "MISSING_PATH"
            return False

        # ── RAM 緩衝區上傳 (實時播放用, 斷電消失, 不落盤) ──
        #   resolve() 對 /ram/... 回 kind="ram"; 走整塊 bytearray 拼 chunk,
        #   收尾校驗 sha 後直接存 self._ram, 不做 .tmp/.bak/pending (RAM 不需回滾保護)。
        kind, full, _raw = self.resolve(path)
        if kind == "ram":
            self.session["path"] = full
            self.session["ram_buf"] = bytearray(total_size) if total_size > 0 else bytearray()
            self.session["active"] = True
            return True

        try:
            temp_path = path + ".tmp"
            self.session["temp_path"] = temp_path

            # Ensure Directory
            self._ensure_parent(temp_path)

            # 斷點續傳: .tmp 存在且 delta.partial 身分一致 (path+size+sha)
            resumed = False
            written = 0
            if self._resume_match(temp_path, path, total_size, sha_expect_hex):
                try:
                    st = os.stat(temp_path)
                    written = st[6]
                    self.session["fp"] = open(temp_path, "r+b")
                    self.session["fp"].seek(written)
                    resumed = True
                    print(f"♻️ [FS] Resume: {path} @ {written} bytes")
                except Exception:
                    written = 0

            if not resumed:
                self.session["fp"] = open(temp_path, "wb")
                written = 0

            self.session["written"] = written
            self.session["active"] = True

            # 記錄 partial 身分供斷線後續傳; written 由 os.stat 導出, 不每包落盤
            self.delta["partial"][path] = {
                "tmp": temp_path,
                "total_size": total_size,
                "sha256": sha_expect_hex or ""
            }
            self._save_delta()
            return True

        except Exception as e:
            self.session["last_error"] = f"OPEN_FAIL: {e}"
            return False

    def write_chunk(self, args: dict) -> bool:
        """FILE_CHUNK (0x2002)"""
        # ── RAM 緩衝區: 直接依 offset 寫入 bytearray ──
        if self.session.get("ram_buf") is not None:
            if not self.session["active"]:
                self.session["last_error"] = "NO_ACTIVE_SESSION"
                return False
            req_id = int(args.get("file_id", 0))
            if req_id != self.session["file_id"]:
                self.session["last_error"] = f"ID_MISMATCH {req_id}!={self.session['file_id']}"
                return False
            off = int(args.get("offset", 0))
            data = args.get("data", b"")
            if not isinstance(data, (bytes, bytearray, memoryview)):
                data = bytes(data)
            buf = self.session["ram_buf"]
            if off + len(data) > len(buf):
                # 允許分塊長度與 total_size 一致時直接擴容 (容錯)
                new = bytearray(off + len(data))
                new[:len(buf)] = buf
                self.session["ram_buf"] = new
                buf = new
            buf[off:off + len(data)] = data
            self.session["written"] = off + len(data)
            return True

        if not self.session["active"] or not self.session["fp"]:
            self.session["last_error"] = "NO_ACTIVE_SESSION"
            return False
            
        req_id = int(args.get("file_id", 0))
        if req_id != self.session["file_id"]:
            self.session["last_error"] = f"ID_MISMATCH {req_id}!={self.session['file_id']}"
            return False
            
        off = int(args.get("offset", 0))
        data = args.get("data", b"")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            data = bytes(data)

        # 中途容量安全網 (前置 QUERY.free 才是主力)
        # ⚠️ statvfs 在 FAT/flash 模式可能返回負數(overflow)，那時 free_bytes 不可靠，
        # 不能當作「沒空間」；只有拿到「明確的正數且不足」才擋。
        _free = self.free_bytes(self.session["path"])
        if _free > 0 and _free < len(data) + 4096:
            self.session["last_error"] = "NO_SPACE"
            return False

        try:
            self.session["fp"].seek(off)
            self.session["fp"].write(data)
            self.session["written"] = off + len(data)
            return True
        except Exception as e:
            self.session["last_error"] = f"WRITE_FAIL: {e}"
            self.session["active"] = False
            return False

    def end_write(self, args: dict) -> bool:
        """FILE_END (0x2003) -> Finalize"""
        if not self.session["active"]:
            return False

        # ── RAM 緩衝區收尾: 校驗 sha → 存 self._ram (不落盤/不回滾) ──
        if self.session.get("ram_buf") is not None:
            buf = self.session["ram_buf"]
            try:
                got_sha = ubinascii.hexlify(hashlib.sha256(buf).digest()).decode()
                expect = self.session.get("sha_expect_hex")
                if expect and got_sha != expect:
                    self.session["last_error"] = "SHA_MISMATCH"
                    self.session["last_sha_hex"] = "00" * 32
                    self.session["last_pending"] = 0
                    self.session["active"] = False
                    self.session["ram_buf"] = None
                    return False
                path = self.session["path"]
                self._ram[path] = bytes(buf)
                self.session["last_sha_hex"] = got_sha
                self.session["last_pending"] = 0
                self.session["active"] = False
                self.session["ram_buf"] = None
                print(f"✅ [FS] RAM buffer ready: {path} ({len(buf)} bytes)")
                return True
            except Exception as e:
                self.session["last_error"] = f"RAM_FINALIZE_ERR: {e}"
                self.session["last_pending"] = 0
                self.session["active"] = False
                self.session["ram_buf"] = None
                return False

        self._close_session()
        
        try:
            ok, result, pending = self._finalize_atomic_write(
                self.session["path"], 
                self.session["temp_path"], 
                self.session["sha_expect_hex"]
            )
            
            if ok:
                self.session["last_sha_hex"] = result
                self.session["last_pending"] = 1 if pending else 0
                self.session["active"] = False
                return True
            else:
                self.session["last_error"] = f"FINALIZE_ERR: {result}"
                self.session["last_sha_hex"] = "00"*32
                self.session["last_pending"] = 0
                self.session["active"] = False
                return False
                
        except Exception as e:
            self.session["last_error"] = f"VERIFY_ERR: {e}"
            self.session["last_pending"] = 0
            self.session["active"] = False
            return False

    def _finalize_atomic_write(self, path, temp_path, expected_sha):
        """Internal finalize logic. 回傳 (ok, sha_hex_or_err, pending_flag)。

        同名覆蓋走兩段式 commit:
          1. 寫 pending delta
          2. 舊檔 path → path.bak
          3. 新檔 .tmp → path
          4. 更新 manifest
        全新檔案 (無舊檔) 單段式: .tmp → path。
        """
        try:
            # 1. Calc SHA
            h = hashlib.sha256()
            buf = bytearray(4096)
            size = 0
            chunk_since_yield = 0
            with open(temp_path, "rb") as f:
                while True:
                    n = f.readinto(buf)
                    if n == 0: break
                    h.update(memoryview(buf)[:n])
                    size += n
                    # 🔧 分批驗證: 每 ~256KB 讓出控制權 (鉤子由啟動方依 WDT 策略
                    #    注入, 模組不知道 WDT 存在)。避免大檔全檔重讀卡死 core0。
                    chunk_since_yield += n
                    if chunk_since_yield >= 262144:
                        yield_point()
                        chunk_since_yield = 0
            
            got_sha = ubinascii.hexlify(h.digest()).decode()
            
            # 2. Verify
            if expected_sha and got_sha != expected_sha:
                print(f"❌ [FS] SHA Mismatch! Got: {got_sha}, Exp: {expected_sha}")
                os.remove(temp_path)
                self.delta["partial"].pop(path, None)
                self._save_delta()
                return False, "SHA_MISMATCH", 0

            # 3. 判斷是否有舊檔 (同名覆蓋 → 兩段式)
            old_exists = False
            try:
                os.stat(path)
                old_exists = True
            except Exception:
                pass

            if old_exists:
                old_sha = ""
                old_size = 0
                d, p_abs, _ = self._manifest_target_abs(path)
                entry = d.get(p_abs)
                if entry and entry.get("h"):
                    old_sha = entry["h"]
                    old_size = entry.get("s", 0)
                else:
                    old_sha = self.calc_sha256(path) or ""
                    try:
                        old_size = os.stat(path)[6]
                    except Exception:
                        old_size = 0

                bak = path + ".bak"
                self.delta["pending"][path] = {
                    "bak": bak,
                    "old_sha": old_sha,
                    "old_size": old_size,
                    "new_sha": got_sha,
                    "boots": 0
                }

            # 4. 清 partial + (若有) 寫 pending, 一次落盤於 rename 之前
            self.delta["partial"].pop(path, None)
            self._save_delta()

            # 5. rename: 舊檔 → .bak (舊檔絕不直接刪), 新檔上位
            if old_exists:
                bak = path + ".bak"
                try:
                    os.stat(bak)
                    os.remove(bak)
                except Exception:
                    pass
                os.rename(path, bak)

            os.rename(temp_path, path)
            
            # 6. Update Manifest
            self.update_manifest_entry(path, size, got_sha)
            print(f"✅ [FS] Written: {path} (Size: {size})")
            return True, got_sha, 1 if old_exists else 0
            
        except Exception as e:
            print(f"❌ [FS] Finalize failed: {e}")
            try: os.remove(temp_path)
            except: pass
            return False, str(e), 0

    # ==================== Commit / Undo / Partial / Move ====================

    def _find_pending(self, path):
        """找 pending 記錄：FILE_PROMOTE 用「真實根路徑」當 key（如 /_app_test.py），
        FILE 覆寫用 resolve 後的 /sd key（如 /sd/_night.bin）。回 (key, rec)；
        找不到回 (None, None)。"""
        if not path:
            return None, None
        raw = "/" + str(path).lstrip("/")
        rec = self.delta.get("pending", {}).get(raw)
        if rec is not None:
            return raw, rec
        _, full, _ = self._manifest_target(path)
        rec = self.delta.get("pending", {}).get(full)
        if rec is not None:
            return full, rec
        return None, None

    def _pending_manifest_target(self, key):
        """依 pending key 決定 manifest 落點：根目錄檔 → local，/sd → SD manifest。"""
        if key == "/sd" or key.startswith("/sd/"):
            return self.manifest_sd, key, MANIFEST_FILE_SD
        return self.manifest_local, key, MANIFEST_FILE

    def _restore_pending(self, key, rec):
        """把 pending 記錄還原：刪新檔 + .bak 改回 key，並回填 manifest。回 True/False。"""
        bak = rec.get("bak")
        try:
            os.stat(key)
            os.remove(key)
        except Exception:
            pass
        try:
            if bak:
                os.stat(bak)
                os.rename(bak, key)
        except Exception as e:
            print("❌ [FS] Boot recovery restore failed {}: {}".format(key, e))
            return False
        old_sha = rec.get("old_sha")
        old_size = rec.get("old_size", 0)
        md, mkey, mfile = self._pending_manifest_target(key)
        if old_sha:
            md[mkey] = {"s": old_size, "h": old_sha}
        else:
            md.pop(mkey, None)
        self._write_manifest(mfile, md)
        print("🛡️ [FS] Boot recovery: restored {} from {}".format(key, bak or "(delete)"))
        return True

    def _boot_recovery_check(self):
        """開機最高優先檢查：pending 備份記錄 boots+1；滿 3 次仍未確認 → 自動還原。

        對應「上傳→備份→確認」的保護帶：新檔 promote 後若一直沒 confirm
        （例如新固件起唔到、無法上線確認），連續 3 次開機就自動回滾舊檔，
        避免壞固件令板子卡死。每次開機都 _save_delta() 落盤更新 boots 計數。
        """
        pending = self.delta.get("pending")
        if not pending:
            return
        restored = []
        for key in list(pending.keys()):
            rec = pending.get(key)
            if not isinstance(rec, dict):
                continue
            boots = int(rec.get("boots", 0) or 0) + 1
            rec["boots"] = boots
            if boots >= 3:
                if self._restore_pending(key, rec):
                    restored.append(key)
        for key in restored:
            pending.pop(key, None)
        self._save_delta()
        remain = len(pending)
        if restored:
            print("🛡️ [FS] Boot recovery: auto-restored {} file(s)".format(len(restored)))
        if remain:
            print("🛡️ [FS] Boot recovery: {} pending backup(s) awaiting confirm".format(remain))

    def confirm_commit(self, path):
        """FILE_CONFIRM (0x2008): 確認覆蓋 → 刪 .bak + 清 pending。回 True/False。"""
        key, rec = self._find_pending(path)
        if not rec:
            return False
        bak = rec.get("bak")
        try:
            if bak:
                os.stat(bak)
                os.remove(bak)
        except Exception:
            pass
        self.delta["pending"].pop(key, None)
        self._save_delta()
        print(f"✅ [FS] Confirmed: {key} (backup removed)")
        return True

    def undo_commit(self, path):
        """FILE_UNDO (0x200A): 復原 → 刪新檔 + .bak 改回 path + 清 pending。回 True/False。"""
        key, rec = self._find_pending(path)
        if not rec:
            return False
        bak = rec.get("bak")
        try:
            os.stat(key)
            os.remove(key)
        except Exception:
            pass
        try:
            if bak:
                os.stat(bak)
                os.rename(bak, key)
        except Exception as e:
            print(f"❌ [FS] Undo rename-back failed: {e}")
            return False
        self.delta["pending"].pop(key, None)
        self._save_delta()

        # 復原後 manifest 回填舊檔資訊 (若有)
        old_sha = rec.get("old_sha")
        old_size = rec.get("old_size", 0)
        md, mkey, mfile = self._pending_manifest_target(key)
        if old_sha:
            md[mkey] = {"s": old_size, "h": old_sha}
        else:
            md.pop(mkey, None)
        self._write_manifest(mfile, md)
        print(f"♻️ [FS] Undone: {key} (old restored)")
        return True

    def partial_query(self, path):
        """FILE_PARTIAL_QUERY (0x200E): 回傳 (partial, written, total_size, sha_hex, full)。"""
        _, full, _ = self._manifest_target_abs(path)
        rec = self.delta.get("partial", {}).get(full)
        if not rec:
            return 0, 0, 0, "", full
        # 活躍 session 的 written 是權威值 (檔案尚未 flush, os.stat 可能回 0)
        if self.session.get("active") and self.session.get("path") == full:
            written = int(self.session.get("written", 0) or 0)
        else:
            tmp = rec.get("tmp")
            written = 0
            try:
                st = os.stat(tmp)
                written = st[6]
            except Exception:
                pass
        return 1, written, rec.get("total_size", 0), rec.get("sha256", ""), full

    def move_file(self, src, dst):
        """FILE_MOVE (0x200D): 通用改名/移動。走 manifest, 不碰 delta。回 True/False。

        只支援同一卷 (sd→sd 或 local→local), 避免跨卷 rename 的隱性複製成本。
        """
        if not src or not dst:
            return False
        s_kind, s_full, _ = self.resolve(src)
        d_kind, d_full, _ = self.resolve(dst)
        if s_kind != d_kind:
            print("❌ [FS] MOVE cross-volume not supported")
            return False
        try:
            self._ensure_parent(d_full)
            os.rename(s_full, d_full)
        except Exception as e:
            print(f"❌ [FS] Move failed: {e}")
            return False

        # manifest: 舊條目搬到新鍵
        d, _, mfile = self._manifest_target(s_full)
        entry = d.pop(s_full, None)
        if entry is not None:
            d[d_full] = entry
            self._write_manifest(mfile, d)
        print(f"📦 [FS] Moved: {s_full} -> {d_full}")
        return True

    def promote_file(self, src, dst):
        """FILE_PROMOTE (0x2011): 把 src 的內容「交換」到 dst（跨卷，SD→根目錄）。

        流程（讀+寫三步法，對真 SD 卡這類「獨立檔案系統」也安全，不靠 rename）：
          1. 把 dst 的舊內容備份成 dst.bak（若舊 bak 存在先刪）
          2. 把 src 串流複製到 dst.tmp
          3. dst.tmp rename 成 dst（正式上線）
          4. 刪 src（清除暫存）
        任一步失敗會嘗試還原 bak；回 True/False。
        """
        try:
            # 正規化 dst 為「根目錄絕對路徑」（/xxx），src 保持 /sd/xxx
            d = str(dst)
            if not d.startswith("/"):
                d = "/" + d
            d = d.rstrip("/")
            if not d:
                return False
            s = str(src)
            if not s.startswith("/"):
                s = "/" + s

            # 安全檢查：dst 不允許指到 /sd 底下（那是「假 SD」資料區，非根固件）
            if d == "/sd" or d.startswith("/sd/"):
                print("❌ [FS] promote dst must be root path, not /sd: {}".format(d))
                return False

            # 0. 確認 src 存在
            if not self.exists(src):
                print("❌ [FS] promote src not found: {}".format(s))
                return False

            d_tmp = d + ".tmp"
            d_bak = d + ".bak"

            # 1. 串流複製 src -> d.tmp
            total = self.begin_read(src)
            if total <= 0:
                print("❌ [FS] promote read src failed")
                return False
            buf = bytearray(4096)
            try:
                with open(d_tmp, "wb") as out:
                    while True:
                        n = self.read_into(buf)
                        if n <= 0:
                            break
                        out.write(buf[:n])
            except Exception as e:
                print("❌ [FS] promote write tmp failed: {}".format(e))
                self._end_read()
                try:
                    os.remove(d_tmp)
                except Exception:
                    pass
                return False
            self._end_read()

            # 2. 舊 dst → d.bak（若舊 bak 先刪）
            had_old = False
            try:
                if self._os_exists(d):
                    had_old = True
                    try:
                        os.remove(d_bak)
                    except Exception:
                        pass
                    os.rename(d, d_bak)
            except Exception as e:
                print("❌ [FS] promote backup failed: {}".format(e))
                try:
                    os.remove(d_tmp)
                except Exception:
                    pass
                return False

            # 3. d.tmp → d（正式上線）
            try:
                os.rename(d_tmp, d)
            except Exception as e:
                print("❌ [FS] promote rename tmp->dst failed: {}".format(e))
                # 失敗：還原 bak
                if had_old:
                    try:
                        os.rename(d_bak, d)
                    except Exception:
                        pass
                try:
                    os.remove(d_tmp)
                except Exception:
                    pass
                return False

            # 3.5 記錄 pending（備份恢復用）+ 更新根目錄 manifest。
            #     FILE_PROMOTE 直接落根目錄（非 /sd），key 用真實根路徑 d，讓
            #     FILE_CONFIRM / FILE_UNDO 能透過 _find_pending 找到並還原。
            new_sha = self.calc_sha256(d) or ""
            try:
                new_size = os.stat(d)[6]
            except Exception:
                new_size = 0
            old_sha = ""
            old_size = 0
            if had_old:
                old_sha = self.calc_sha256(d_bak) or ""
                try:
                    old_size = os.stat(d_bak)[6]
                except Exception:
                    old_size = 0
            self.delta["pending"][d] = {
                "bak": d_bak if had_old else "",
                "old_sha": old_sha,
                "old_size": old_size,
                "new_sha": new_sha,
                "boots": 0
            }
            self._save_delta()
            self.manifest_local[d] = {"s": new_size, "h": new_sha}
            self._write_manifest(MANIFEST_FILE, self.manifest_local)

            # 4. 刪 src（SD 暫存清除）
            try:
                self.delete_file(src)
            except Exception as e:
                print("⚠️ [FS] promote delete src failed: {}".format(e))

            print("✅ [FS] promoted: {} -> {} (bak={})".format(s, d, "yes" if had_old else "no"))
            return True
        except Exception as e:
            print("❌ [FS] promote exception: {}".format(e))
            return False

    def _os_exists(self, path):
        try:
            os.stat(path)
            return True
        except Exception:
            return False

    # ==================== Unified Data Layer ====================
    # 路徑前綴決定目的地：
    #   /ram/...  -> RAM cache (暫存，最快)
    #   /sd/...   -> SD 永久儲存 (依 _raw_mode 決定走 raw 或 FAT)
    #   其他/相對路徑 -> 預設補 /sd
    #
    # raw 模式 (alloc.json 存在)：讀寫直接走 fast_io.Storage
    # FAT 模式 (alloc.json 不存在)：讀寫走 os.open/readinto

    def resolve(self, path):
        """正規化路徑前綴。回傳 (kind, full_path, raw_name)
        kind: 'ram' | 'sd'
        full_path: FAT 用的完整路徑 (/ram/... 或 /sd/...)
        raw_name : SD-raw alloc 用的鍵名 (去掉 /sd 前綴)
        """
        p = str(path)
        if not p.startswith("/"):
            p = "/" + p
        if p == "/ram" or p.startswith("/ram/"):
            return ("ram", p, p[len("/ram"):].lstrip("/"))
        if p == "/sd" or p.startswith("/sd/"):
            return ("sd", p, p[len("/sd"):].lstrip("/"))
        return ("sd", "/sd" + p, p.lstrip("/"))

    def _ensure_parent(self, full_path):
        parent = "/".join(full_path.split("/")[:-1])
        if not parent:
            return
        curr = ""
        for part in parent.split("/"):
            if not part:
                continue
            curr += "/" + part
            try:
                os.stat(curr)
            except Exception:
                try:
                    os.mkdir(curr)
                except Exception:
                    pass

    def write(self, path, data):
        """統一寫入：依路由 + 模式寫入，回傳 True/False。"""
        kind, full, raw_name = self.resolve(path)

        if kind == "ram":
            self._ram[full] = bytes(data)
            return True

        if self._raw_mode and self._raw is not None and raw_name:
            try:
                self._raw.write_file(raw_name, data)
                return True
            except Exception as e:
                print("⚠️ [FS] raw write failed:", e)

        # FAT 落地
        try:
            self._ensure_parent(full)
            tmp = full + ".tmp"
            h = hashlib.sha256()
            mv = memoryview(data)
            with open(tmp, "wb") as f:
                f.write(mv)
            size = len(mv)
            h.update(mv)
            try:
                os.stat(full)
                os.remove(full)
            except Exception:
                pass
            os.rename(tmp, full)
            self.update_manifest_entry(full, size, ubinascii.hexlify(h.digest()).decode())
            return True
        except Exception as e:
            print("❌ [FS] FAT write failed:", e)
            try:
                os.remove(full + ".tmp")
            except Exception:
                pass
            return False

    def read(self, path):
        """統一讀取整個檔案。回傳 bytes 或 None。"""
        kind, full, raw_name = self.resolve(path)

        if kind == "ram":
            return self._ram.get(full)

        if self._raw_mode and self._raw is not None and raw_name:
            try:
                return bytes(self._raw.read_all(raw_name))
            except Exception:
                pass
        try:
            with open(full, "rb") as f:
                return f.read()
        except Exception:
            return None

    # ════════════════════════════════════════════════════════
    #  串流讀取 API (begin_read / read_into / seek / tell / end_read)
    #  行為類似 file object，內部根據路徑和模式自動路由。
    # ════════════════════════════════════════════════════════

    def begin_read(self, path):
        """開始串流讀取。回傳總位元組數，0 表示失敗。"""
        self._end_read()
        kind, full, raw_name = self.resolve(path)

        if kind == "ram":
            data = self._ram.get(full)
            if data is None:
                return 0
            self._str_kind = "ram"
            self._str_data = data
            self._str_pos = 0
            return len(data)

        if self._raw_mode and self._raw is not None and raw_name:
            try:
                size = self._raw.read_begin(raw_name)
                self._str_kind = "raw"
                return size
            except Exception:
                pass

        try:
            f = open(full, "rb")
            self._str_kind = "fat"
            self._str_fp = f
            try:
                st = os.stat(full)
                return st[6]
            except Exception:
                return 0
        except Exception:
            return 0

    def read_into(self, buf):
        """讀取下一塊資料到 buf。回傳位元組數，0 表示結束。"""
        k = self._str_kind
        if k == "ram":
            data = self._str_data
            pos = self._str_pos
            if pos >= len(data):
                return 0
            n = min(len(buf), len(data) - pos)
            buf[:n] = data[pos:pos + n]
            self._str_pos = pos + n
            return n
        if k == "raw":
            return self._raw.read_into(buf)
        if k == "fat":
            return self._str_fp.readinto(buf)
        return 0

    def seek(self, offset):
        """設定讀取位置 (類似 file.seek)。"""
        k = self._str_kind
        if k == "ram":
            if offset < 0:
                offset = 0
            if offset > len(self._str_data):
                offset = len(self._str_data)
            self._str_pos = offset
        elif k == "raw":
            self._raw.seek(offset)
        elif k == "fat":
            self._str_fp.seek(offset)

    def tell(self):
        """回傳目前讀取位置 (類似 file.tell)。"""
        k = self._str_kind
        if k == "ram":
            return self._str_pos
        if k == "raw":
            return self._raw.tell()
        if k == "fat":
            return self._str_fp.tell()
        return 0

    def end_read(self):
        """結束串流讀取，釋放資源。"""
        self._end_read()

    def _end_read(self):
        k = self._str_kind
        if k == "raw":
            try:
                self._raw.read_end()
            except Exception:
                pass
        elif k == "fat" and hasattr(self, "_str_fp"):
            try:
                self._str_fp.close()
            except Exception:
                pass
        self._str_kind = None

    def open_read(self, path):
        """回傳可讀的 file-like 物件供串流讀取；RAM 則回 BytesIO。
        大檔串流建議使用 begin_read / read_into / seek / tell / end_read。

        🔧 根目錄真檔 (如 /manifest.json 本地 manifest、FILE_PROMOTE 落根的固件)
        直接用絕對路徑開 — resolve() 會把這類路徑映射到 /sd 導致讀不到
        (與 file_actions 的 _root_file_exists 同一套判斷)。
        """
        p = str(path)
        if p.startswith("/") and not p.startswith(("/ram", "/sd")):
            try:
                st = os.stat(p)
                if (st[0] & 0x4000) == 0:  # 是檔案不是目錄
                    return open(p, "rb")
            except Exception:
                pass
        kind, full, raw_name = self.resolve(path)
        if kind == "ram":
            data = self._ram.get(full)
            if data is None:
                return None
            import io
            return io.BytesIO(data)
        try:
            return open(full, "rb")
        except Exception:
            return None

    def exists(self, path):
        kind, full, raw_name = self.resolve(path)
        if kind == "ram":
            return full in self._ram
        if self._raw_mode and self._raw is not None and raw_name:
            try:
                if self._raw._alloc.find(raw_name) is not None:
                    return True
            except Exception:
                pass
        try:
            os.stat(full)
            return True
        except Exception:
            return False

    def list(self, folder="/sd"):
        kind, full, raw_name = self.resolve(folder)
        if kind == "ram":
            prefix = full.rstrip("/") + "/"
            return [k for k in self._ram if k.startswith(prefix) or k == full]
        try:
            return [full.rstrip("/") + "/" + n for n in os.listdir(full)]
        except Exception:
            return []

    def remove(self, path):
        """統一刪除：RAM / SD-raw / FAT 各自更新 table。"""
        kind, full, raw_name = self.resolve(path)
        if kind == "ram":
            self._ram.pop(full, None)
            return True
        ok = False
        if self._raw_mode and self._raw is not None and raw_name:
            try:
                if self._raw._alloc.find(raw_name) is not None:
                    self._raw.remove(raw_name)
                    ok = True
            except Exception:
                pass
        if self.delete_file(full):
            ok = True
        return ok

    # ==================== Other Operations ====================

    def delete_file(self, path):
        try:
            st = os.stat(path)
            mode = st[0]
            if (mode & 0o170000) == 0o040000: # Directory
                os.rmdir(path)
                self.remove_manifest_entry(path)
                print(f"🗑️ [FS] Dir removed: {path}")
            else: # File
                os.remove(path)
                self.remove_manifest_entry(path)
                print(f"🗑️ [FS] File removed: {path}")
            return True
        except Exception as e:
            print(f"⚠️ [FS] Delete failed: {e}")
            return False
            
    def calc_sha256(self, path):
        """Helper for external use"""
        try:
            h = hashlib.sha256()
            buf = bytearray(4096)
            chunk_since_yield = 0
            with open(path, "rb") as f:
                while True:
                    n = f.readinto(buf)
                    if n == 0: break
                    h.update(memoryview(buf)[:n])
                    # 🔧 分批驗證: 與 _finalize_atomic_write 一致, 每 ~256KB 讓出控制權
                    chunk_since_yield += n
                    if chunk_since_yield >= 262144:
                        yield_point()
                        chunk_since_yield = 0
            return ubinascii.hexlify(h.digest()).decode()
        except:
            return None

    def free_bytes(self, path):
        """回傳 path 所在卷的可用位元組數; 失敗回 0。

        依「絕對路徑前綴」判斷卷（不 resolve——resolve 會把 /boot.py 映射成 /sd
        而查錯卷）：/sd 前綴查 SD，其餘（根目錄韌體）查 root flash。
        """
        try:
            p = "/" + str(path).lstrip("/")
            if p == "/sd" or p.startswith("/sd/"):
                st = os.statvfs("/sd")
            else:
                st = os.statvfs("/")
            # ⚠️ 用 st[2] (f_bfree) 而非 st[3] (f_bavail)：MicroPython FAT/flash 的
            # f_bavail 會回負數(root quota overflow)，導致空間誤判成 0/負數。
            # f_bfree 才是真實可用 block 數。
            return st[0] * st[2]  # block_size * free_blocks
        except Exception:
            return 0

    def scan_sd(self):
        """FILE_SCAN(target=1): 同步掃描 /sd, 重算 sha256, 更新 SD manifest。"""
        ignore = {".manifest.json", ".delta.json"}
        found = {}

        def _walk(d):
            try:
                for name, ftype, *_ in os.ilistdir(d):
                    full = (d.rstrip("/") + "/" + name) if d != "/" else "/" + name
                    if name in ignore:
                        continue
                    if ftype == 0x4000:  # directory
                        _walk(full)
                    else:
                        sha = self.calc_sha256(full)
                        if sha is None:
                            continue
                        try:
                            size = os.stat(full)[6]
                        except Exception:
                            size = 0
                        found[full] = {"s": size, "h": sha}
            except Exception as e:
                print(f"⚠️ [FS] SD scan walk error {d}: {e}")

        _walk("/sd")
        self.manifest_sd = found
        self._write_manifest(MANIFEST_FILE_SD, self.manifest_sd)
        print(f"📦 [FS] SD scan done: {len(found)} files")
        return len(found)

    def scan_all(self):
        """
        Request background scan (set flag for Core 1)
        """
        if self.scanning: return
        from lib.sys.sys_bus import bus
        from lib.sys.log_service import get_log
        get_log().info("FS Scan requested (Queued for Core 1)")
        bus.shared["fs_scan_requested"] = True

    def scan_init(self):
        """
        Phase 0: collect all file paths (ilistdir only, no hashing).
        Called by Core 1 FsScanTask.
        Respects config.json scan_ignore paths.
        """
        ignore_prefixes = self._load_scan_ignore()
        file_list = []
        def _collect(dir_path):
            try:
                for entry in os.ilistdir(dir_path):
                    name = entry[0]
                    type_ = entry[1]
                    full_path = f"{dir_path}/{name}" if dir_path != "/" else f"/{name}"
                    if name == "manifest.json": continue
                    if name.endswith(".tmp"): continue
                    if name.endswith(".db"): continue
                    if name.endswith(".bak"): continue
                    if self._is_ignored(full_path, ignore_prefixes):
                        continue
                    if type_ == 0x4000:
                        _collect(full_path)
                    else:
                        file_list.append(full_path)
            except Exception as e:
                get_log().error("Scan collect error {}: {}".format(dir_path, e))
        _collect("/")

        self.scanning = True
        self._scan_files = file_list
        self._scan_manifest = {}
        self._scan_idx = 0

        from lib.sys.sys_bus import bus
        from lib.sys.log_service import get_log
        bus.shared["fs_scan_total"] = len(file_list)
        bus.shared["fs_scan_progress"] = 0
        get_log().set_metric("fs_scan_total", len(file_list))
        get_log().set_metric("fs_scan_progress", 0)
        get_log().info("FS Scan phase 1: {} files to hash (Core 1)".format(len(file_list)))

    def scan_step(self):
        """
        Phase 1: hash the next file. Returns True when all done.
        Hashes the entire file in one call (no chunking needed during boot).
        Yields every 256KB to keep interrupts responsive.
        """
        from lib.sys.sys_bus import bus
        from lib.sys.log_service import get_log

        if not self.scanning or self._scan_idx >= len(self._scan_files):
            bus.shared["fs_scan_result"] = self._scan_manifest
            bus.shared["fs_scan_done"] = True
            get_log().set_metric("fs_scan_done", 1)
            self.scanning = False
            total = len(self._scan_files)
            get_log().info("FS Scan complete (Core 1). Found {} files. Handing over to Core 0...".format(total))
            return True

        path = self._scan_files[self._scan_idx]
        self._scan_idx += 1
        bus.shared["fs_scan_current"] = path

        # Fast-path abort check between files
        if not bus.shared.get("engine_run", True):
            self.scanning = False
            get_log().warn("FS Scan aborted by Core 0")
            return False

        try:
            h = hashlib.sha256()
            buf = bytearray(4096)
            size = 0
            chunk_since_yield = 0
            with open(path, "rb") as f:
                while True:
                    n = f.readinto(buf)
                    if n == 0:
                        break
                    h.update(memoryview(buf)[:n])
                    size += n
                    chunk_since_yield += n
                    if chunk_since_yield >= 262144:
                        time.sleep_ms(0)
                        chunk_since_yield = 0
                        if not bus.shared.get("engine_run", True):
                            self.scanning = False
                            get_log().warn("FS Scan aborted by Core 0")
                            return False
            sha = ubinascii.hexlify(h.digest()).decode()
            self._scan_manifest[path] = {"s": size, "h": sha}
        except Exception as e:
            get_log().error("Scan error {}: {}".format(path, e))

        bus.shared["fs_scan_progress"] = self._scan_idx
        get_log().set_metric("fs_scan_progress", self._scan_idx)
        return False

    def finalize_scan(self):
        """Called by Core 0 to save the manifest"""
        from lib.sys.sys_bus import bus
        from lib.sys.log_service import get_log
        if not bus.shared.get("fs_scan_done"): return

        new_manifest = bus.shared.get("fs_scan_result")
        if not new_manifest:
            bus.shared["fs_scan_done"] = False
            bus.shared["fs_scan_result"] = None
            get_log().set_metric("fs_scan_done", 0)
            return

        self.manifest_local = new_manifest
        self._write_manifest(MANIFEST_FILE, self.manifest_local)

        bus.shared["fs_scan_done"] = False
        bus.shared["fs_scan_result"] = None
        get_log().set_metric("fs_scan_done", 0)
        get_log().info("FS Manifest saved by Core 0 ({} entries).".format(len(self.manifest_local)))

# Singleton Instance
fs = FileSystemManager()