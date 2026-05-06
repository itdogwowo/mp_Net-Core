import os
import time
import ujson
import ubinascii
import hashlib
from lib.dispatch import dprint

MANIFEST_FILE = "/manifest.json"

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
        self.manifest = {}
        self.scanning = False
        self._scan_files = []
        self._scan_manifest = {}
        self._scan_idx = 0
        
        # Session State for File Upload
        self.session = {
            "active": False,
            "path": None,
            "temp_path": None,
            "fp": None,
            "file_id": 0,
            "written": 0,
            "sha_expect_hex": None,
            "last_error": None,
            "last_sha_hex": ""
        }
        
        self.load_manifest()

    def load_manifest(self):
        try:
            with open(MANIFEST_FILE, "r") as f:
                self.manifest = ujson.load(f)
            print(f"📦 [FS] Manifest loaded: {len(self.manifest)} files")
        except:
            print("⚠️ [FS] Manifest missing or corrupt, starting scan...")
            self.manifest = {}
            # Start background scan if manifest is missing
            self.scan_all()

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

    def save_manifest(self):
        try:
            with open(MANIFEST_FILE, "w") as f:
                # Custom Pretty Dump for Manifest
                f.write("{\n")
                # Sort keys for consistent order
                keys = sorted(self.manifest.keys())
                for i, k in enumerate(keys):
                    entry = self.manifest[k]
                    # Use json.dumps for key to handle escaping
                    key_str = ujson.dumps(k)
                    # Entry is small, keep it one line: {"s": 123, "h": "..."}
                    entry_str = ujson.dumps(entry)
                    
                    f.write(f'    {key_str}: {entry_str}')
                    
                    if i < len(keys) - 1:
                        f.write(",\n")
                    else:
                        f.write("\n")
                f.write("}")
        except Exception as e:
            print(f"❌ [FS] Save manifest failed: {e}")

    def update_manifest_entry(self, path, size, sha_hex):
        self.manifest[path] = {
            "s": size,
            "h": sha_hex
        }
        self.save_manifest()

    def remove_manifest_entry(self, path):
        if path in self.manifest:
            del self.manifest[path]
            self.save_manifest()

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

    def begin_write(self, args: dict) -> bool:
        """FILE_BEGIN (0x2001)"""
        self._close_session()
        
        # Reset Session
        self.session.update({
            "active": False,
            "path": args.get("path"),
            "file_id": int(args.get("file_id", 0)),
            "written": 0,
            "last_error": None
        })
        
        sha_bytes = args.get("sha256")
        self.session["sha_expect_hex"] = ubinascii.hexlify(sha_bytes).decode() if sha_bytes else None
        
        if not self.session["path"]:
            self.session["last_error"] = "MISSING_PATH"
            return False

        try:
            # Create Temp Path
            self.session["temp_path"] = self.session["path"] + ".tmp"
            
            # Ensure Directory
            parent = "/".join(self.session["temp_path"].split("/")[:-1])
            if parent:
                parts = parent.split("/")
                curr = ""
                for p in parts:
                    if not p: continue
                    curr += "/" + p
                    try:
                        os.stat(curr)
                    except:
                        try:
                            os.mkdir(curr)
                        except:
                            pass
            
            # Open Temp File
            self.session["fp"] = open(self.session["temp_path"], "wb")
            self.session["active"] = True
            return True
            
        except Exception as e:
            self.session["last_error"] = f"OPEN_FAIL: {e}"
            return False

    def write_chunk(self, args: dict) -> bool:
        """FILE_CHUNK (0x2002)"""
        if not self.session["active"] or not self.session["fp"]:
            self.session["last_error"] = "NO_ACTIVE_SESSION"
            return False
            
        req_id = int(args.get("file_id", 0))
        if req_id != self.session["file_id"]:
            self.session["last_error"] = f"ID_MISMATCH {req_id}!={self.session['file_id']}"
            return False
            
        off = int(args.get("offset", 0))
        data = args.get("data", b"")
        
        try:
            if off != self.session["written"]:
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
            
        self._close_session()
        
        try:
            ok, result = self._finalize_atomic_write(
                self.session["path"], 
                self.session["temp_path"], 
                self.session["sha_expect_hex"]
            )
            
            if ok:
                self.session["last_sha_hex"] = result
                self.session["active"] = False
                return True
            else:
                self.session["last_error"] = f"FINALIZE_ERR: {result}"
                self.session["last_sha_hex"] = "00"*32
                self.session["active"] = False
                return False
                
        except Exception as e:
            self.session["last_error"] = f"VERIFY_ERR: {e}"
            self.session["active"] = False
            return False

    def _finalize_atomic_write(self, path, temp_path, expected_sha):
        """Internal finalize logic"""
        try:
            # 1. Calc SHA
            h = hashlib.sha256()
            buf = bytearray(2048)
            size = 0
            with open(temp_path, "rb") as f:
                while True:
                    n = f.readinto(buf)
                    if n == 0: break
                    h.update(memoryview(buf)[:n])
                    size += n
            
            got_sha = ubinascii.hexlify(h.digest()).decode()
            
            # 2. Verify
            if expected_sha and got_sha != expected_sha:
                print(f"❌ [FS] SHA Mismatch! Got: {got_sha}, Exp: {expected_sha}")
                os.remove(temp_path)
                return False, "SHA_MISMATCH"
            
            # 3. Rename (Atomic Replace)
            try:
                os.stat(path)
                os.remove(path)
            except:
                pass
                
            os.rename(temp_path, path)
            
            # 4. Update Manifest
            self.update_manifest_entry(path, size, got_sha)
            print(f"✅ [FS] Written: {path} (Size: {size})")
            return True, got_sha
            
        except Exception as e:
            print(f"❌ [FS] Finalize failed: {e}")
            try: os.remove(temp_path)
            except: pass
            return False, str(e)

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
            buf = bytearray(2048)
            with open(path, "rb") as f:
                while True:
                    n = f.readinto(buf)
                    if n == 0: break
                    h.update(memoryview(buf)[:n])
            return ubinascii.hexlify(h.digest()).decode()
        except:
            return None

    def scan_all(self):
        """
        Request background scan (set flag for Core 1)
        """
        if self.scanning: return
        from lib.sys_bus import bus
        print("🔄 [FS] Scan requested (Queued for Core 1)")
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
                    if self._is_ignored(full_path, ignore_prefixes):
                        continue
                    if type_ == 0x4000:
                        _collect(full_path)
                    else:
                        file_list.append(full_path)
            except Exception as e:
                print(f"  ⚠️ Scan collect error {dir_path}: {e}")
        _collect("/")

        self.scanning = True
        self._scan_files = file_list
        self._scan_manifest = {}
        self._scan_idx = 0

        from lib.sys_bus import bus
        bus.shared["fs_scan_total"] = len(file_list)
        bus.shared["fs_scan_progress"] = 0
        print(f"🔍 [FS] Scan phase 1: {len(file_list)} files to hash (Core 1)")

    def scan_step(self):
        """
        Phase 1: hash the next file. Returns True when all done.
        Hashes the entire file in one call (no chunking needed during boot).
        Yields every 256KB to keep interrupts responsive.
        """
        from lib.sys_bus import bus

        if not self.scanning or self._scan_idx >= len(self._scan_files):
            bus.shared["fs_scan_result"] = self._scan_manifest
            bus.shared["fs_scan_done"] = True
            self.scanning = False
            total = len(self._scan_files)
            print(f"✅ [FS] Scan complete (Core 1). Found {total} files. Handing over to Core 0...")
            return True

        path = self._scan_files[self._scan_idx]
        self._scan_idx += 1
        bus.shared["fs_scan_current"] = path

        # Fast-path abort check between files
        if not bus.shared.get("engine_run", True):
            self.scanning = False
            print("⏹ [FS] Scan aborted by Core 0")
            return False

        try:
            h = hashlib.sha256()
            buf = bytearray(2048)
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
                            print("⏹ [FS] Scan aborted by Core 0")
                            return False
            sha = ubinascii.hexlify(h.digest()).decode()
            self._scan_manifest[path] = {"s": size, "h": sha}
        except Exception as e:
            print(f"  ⚠️ Scan error {path}: {e}")

        bus.shared["fs_scan_progress"] = self._scan_idx
        return False

    def finalize_scan(self):
        """Called by Core 0 to save the manifest"""
        from lib.sys_bus import bus
        if not bus.shared.get("fs_scan_done"): return

        new_manifest = bus.shared.get("fs_scan_result")
        if not new_manifest:
            bus.shared["fs_scan_done"] = False
            bus.shared["fs_scan_result"] = None
            return

        self.manifest = new_manifest
        self.save_manifest()

        bus.shared["fs_scan_done"] = False
        bus.shared["fs_scan_result"] = None
        print(f"💾 [FS] Manifest saved by Core 0 ({len(self.manifest)} entries).")

# Singleton Instance
fs = FileSystemManager()