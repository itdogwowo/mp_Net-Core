from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
import ubinascii
from lib.sys.sys_bus import bus
from lib.sys.fs_manager import fs
from lib.sys.log_service import get_log
import _thread
import os
import machine


def _hex_to_bytes(hexstr, fallback=b'\x00' * 32):
    try:
        return ubinascii.unhexlify(hexstr)
    except Exception:
        return fallback


def _send(ctx, cmd, fields):
    """統一送出回應幀 (無 addr = 廣播, 沿用既有行為)。"""
    app = ctx["app"]
    if "send" not in ctx:
        return
    try:
        cmd_def = app.store.get(cmd)
        if not cmd_def:
            return
        data = SchemaCodec.encode(cmd_def, fields)
        ctx["send"](Proto.pack(cmd, data))
    except Exception as e:
        print(f"⚠️ [File] send 0x{cmd:04X} failed: {e}")


def _send_error(ctx, error_key):
    """依 fs.session['last_error'] 對應出 FILE_ERROR_RSP 的錯誤 bool。"""
    err = fs.session.get("last_error") or ""
    fields = {
        "err_no_space": 0,
        "err_write_fail": 0,
        "err_offset_mismatch": 0,
        "err_id_mismatch": 0,
        "err_sha_mismatch": 0,
        "err_not_active": 0,
        "err_busy": 0,
        "failed_offset": 0,
        "written_up_to": int(fs.session.get("written", 0) or 0),
        "path": fs.session.get("path") or "",
    }
    if error_key:
        fields[error_key] = 1
    else:
        if "SHA_MISMATCH" in err:
            fields["err_sha_mismatch"] = 1
        elif "ID_MISMATCH" in err:
            fields["err_id_mismatch"] = 1
        elif "NO_SPACE" in err:
            fields["err_no_space"] = 1
        elif "NO_ACTIVE_SESSION" in err:
            fields["err_not_active"] = 1
        else:
            fields["err_write_fail"] = 1
    _send(ctx, 0x2010, fields)


def on_file_begin(ctx, args):
    path = args.get("path", "")
    if path:
        # 路徑路由：/sd、/ram 前綴維持原卷；其餘視為「根目錄韌體」(/boot.py、
        # /lib/... 等)，保持絕對路徑直接寫 root flash，不再 resolve 到 /sd。
        p = "/" + str(path).lstrip("/")
        if p == "/sd" or p.startswith("/sd/") or p == "/ram" or p.startswith("/ram/"):
            args['path'] = fs.resolve(p)[1]
        else:
            args['path'] = p

    ok = fs.begin_write(args)
    if ok:
        print(f"📂 [File] Start -> {fs.session['path']} (Atomic)")
    else:
        _send_error(ctx, None)


def on_file_chunk(ctx, args):
    if fs.write_chunk(args):
        # 🚀 關鍵：每收到一包就回傳 ACK
        # 讓 PC 知道可以發下一包了
        _send(ctx, 0x2004, {
            "file_id": args["file_id"],
            "offset": args["offset"]
        })
    else:
        # ⚠️ 寫入失敗：回錯誤碼 (容量不足 / offset 不符 / id 不符 / 寫入失敗)
        print(f"⚠️ [File] Chunk Failed: Off={args.get('offset')} Err={fs.session['last_error']}")
        _send_error(ctx, None)


def on_file_end(ctx, args):
    # 執行校驗 (內部會調用 fs._finalize_atomic_write 兩段式 commit)
    ok = fs.end_write(args)

    path = fs.session["path"]
    sha = fs.session["last_sha_hex"]
    pending = fs.session.get("last_pending", 0)

    if ok:
        print("-" * 40)
        print(f"🏁 [File] End Success: {path}")
        print(f"🔒 [SHA256] {sha}")
        print("-" * 40)

        # 回覆最終狀態 (0x2006)，附 free + pending
        size = 0
        sha_bytes = b'\x00' * 32
        # ── RAM 緩衝區: 不落盤, manifest/os.stat 都查不到, 直接回 session 的真實 sha ──
        if fs.resolve(path)[0] == "ram":
            data = fs.read(path) or b""
            size = len(data)
            sha_bytes = _hex_to_bytes(sha) if sha else b'\x00' * 32
        else:
            entry, _ = fs.manifest_lookup_abs(path)
            if entry:
                size = entry.get("s", 0)
                sha_bytes = _hex_to_bytes(entry.get("h", ""))
            else:
                try:
                    import os
                    st = os.stat(path)
                    size = st[6]
                    sha_bytes = _hex_to_bytes(sha) if sha else b'\x00' * 32
                except Exception:
                    size = 0
                    sha_bytes = b'\x00' * 32

        _send(ctx, 0x2006, {
            "exists": 1,
            "sha256": sha_bytes,
            "size": size,
            "path": path,
            "free": fs.free_bytes(path),
            "pending": 1 if pending else 0
        })
    else:
        err = fs.session['last_error']
        print(f"❌ [File] End Failed: {err}")
        _send_error(ctx, None)


def _root_file_exists(path):
    """判斷 path 是否為「根目錄的普通檔案」（FILE_PROMOTE 落地的固件）。"""
    import os
    try:
        st = os.stat(path)
        return (st[0] & 0x4000) == 0
    except Exception:
        return False


def on_file_query(ctx, args):
    path = args.get("path")
    if path:
        # FILE_PROMOTE 落根目錄的檔用真實根路徑（不 resolve → /sd）；
        # 其餘依「非 /ram//sd → /sd」慣例解析。
        raw = "/" + str(path).lstrip("/")
        if _root_file_exists(raw):
            path = raw

    exists = 0
    sha = b'\x00' * 32
    size = 0
    pending = 0

    if path and _root_file_exists(path):
        # 根目錄檔：用絕對路徑查 manifest（跳過 resolve 映射）
        entry, full = fs.manifest_lookup_abs(path)
        if entry:
            exists = 1
            size = entry.get("s", 0)
            sha = _hex_to_bytes(entry.get("h", ""))
            print(f"🔍 [Query] Root Cache Hit: {full} (Size:{size})")
        else:
            try:
                import os
                st = os.stat(full)
                exists = 1
                size = st[6]
                sha_hex = fs.calc_sha256(full)
                if sha_hex:
                    sha = ubinascii.unhexlify(sha_hex)
                print(f"🔍 [Query] Root Realtime: {full} (Size:{size})")
            except Exception:
                print(f"🔍 [Query] {full} not found.")
        if full in fs.delta.get("pending", {}):
            pending = 1
        _send(ctx, 0x2006, {
            "exists": exists,
            "sha256": sha,
            "size": size,
            "path": full,
            "free": fs.free_bytes("/"),
            "pending": pending
        })
        return

    if path:
        path = fs.resolve(path)[1]

    # 優先查對應卷的 Manifest
    entry, full = fs.manifest_lookup(path)
    if entry:
        exists = 1
        size = entry.get("s", 0)
        sha = _hex_to_bytes(entry.get("h", ""))
        print(f"🔍 [Query] Cache Hit: {full} (Size:{size})")
    else:
        # Cache Miss: 實時檢查
        try:
            import os
            st = os.stat(full)
            exists = 1
            size = st[6]
            sha_hex = fs.calc_sha256(full)
            if sha_hex:
                sha = ubinascii.unhexlify(sha_hex)
            print(f"🔍 [Query] Realtime: {full} (Size:{size})")
        except Exception:
            print(f"🔍 [Query] {full} not found.")

    # 是否有待確認覆蓋 (pending delta)
    if full in fs.delta.get("pending", {}):
        pending = 1

    _send(ctx, 0x2006, {
        "exists": exists,
        "sha256": sha,
        "size": size,
        "path": full,
        "free": fs.free_bytes(full),
        "pending": pending
    })


def on_file_read(ctx, args):
    path = args.get("path")
    offset = args.get("offset", 0)
    length = args.get("length", 1024)
    full_path = path

    if path:
        # 🔧 與 on_file_query 一致: 根目錄真檔 (FILE_PROMOTE 落地/本地 manifest)
        # 用絕對路徑讀, 不 resolve → /sd (否則 /manifest.json 查得到卻讀不到)
        raw = "/" + str(path).lstrip("/")
        if _root_file_exists(raw):
            full_path = raw
        else:
            full_path = fs.resolve(path)[1]

    try:
        f = fs.open_read(full_path)
        if f is None:
            raise OSError("not found: " + str(full_path))
        try:
            f.seek(offset)
            data = f.read(length)
        finally:
            f.close()

        _send(ctx, 0x2002, {
            "file_id": 0,
            "offset": offset,
            "data": data
        })
        print(f"📤 [File] Read Chunk: {full_path} Off:{offset} Len:{len(data)}")
    except Exception as e:
        print(f"❌ [File] Read Failed: {full_path} - {e}")
        # Send empty data to indicate error/eof if needed, or just silence
        _send(ctx, 0x2002, {
            "file_id": 0,
            "offset": offset,
            "data": b""
        })


def on_file_delete(ctx, args):
    path = args.get("path")

    if not path:
        return

    # 🔧 重建索引專用 (master 選單「4. 重建文件索引」): 剷 /manifest.json →
    #    唔回覆、直接 self-reset —— 開機 detect 到 manifest 缺失會自動背景
    #    重掃 (core1), 上線時索引已重建。master 見到 WS 斷線 = 已執行,
    #    唔使加新指令、唔使 0x2006 回覆 (重用 0x2009 + 通道斷線信號)。
    if str(path).lstrip("/").rstrip("/") == "manifest.json":
        try:
            os.remove("/manifest.json")
            get_log().immediate("[FileScan] /manifest.json 已剷除 → 重啟 (boot 重建索引)")
        except Exception as e:
            get_log().error("[FileScan] 剷 manifest 失敗 (照重啟): {}".format(e))
        machine.reset()
        return

    # 根目錄檔用「絕對路徑」刪（resolve 會把 /xxx 誤映射成 /sd/xxx，刪錯 + 更新錯 manifest）
    raw = "/" + str(path).lstrip("/")
    if _root_file_exists(raw):
        fs.remove_abs(raw)
        on_file_query(ctx, {"path": raw})
        return

    # 統一刪除：依前綴路由，同步更新 alloc / manifest table
    fs.remove(path)

    # 操作後查詢狀態並回傳
    on_file_query(ctx, {"path": fs.resolve(path)[1]})


def on_file_scan(ctx, args):
    """
    FILE_SCAN: 依 target 選擇掃描範圍
      0 = 本地 flash (背景掃描, 跳過 /sd)
      1 = SD (同步掃描)

    註: 0x200B 冇定義回覆; master 選單「4. 重建文件索引」而家改用
    「0x2009 剷 /manifest.json → 設備 self-reset → 開機重掃」嘅流程
    (WS 斷線 = 已執行)。0x200B 保留畀 console 手動用, 進度可經
    STATUS_GET 嘅 fs_scan_busy 查。
    """
    target = int(args.get("target", 0) or 0)
    if target == 1:
        get_log().info("[FileScan] 0x200B target=1 (SD 同步掃描) 收到, 已排隊")
        _thread.start_new_thread(fs.scan_sd, ())
    else:
        get_log().info("[FileScan] 0x200B target=0 (local 背景掃描) 收到, 已排隊 (core1)")
        # 🔧 同步先標記「掃描中」再開背景執行——否則 master 在 thread 尚未跑到
        #    scan_all() 前就輪詢, 會誤判「掃描已完成」而太早下載 manifest。
        bus.shared["fs_scan_requested"] = True
        _thread.start_new_thread(fs.scan_all, ())


def on_file_confirm(ctx, args):
    """FILE_CONFIRM (0x2008): 確認覆蓋 → 刪 .bak + 清 pending。"""
    path = args.get("path")
    if not path:
        return
    fs.confirm_commit(path)
    # 確認後回傳新檔現況 (pending 已清 = 0)
    on_file_query(ctx, {"path": path})


def on_file_undo(ctx, args):
    """FILE_UNDO (0x200A): 復原 → 刪新檔 + .bak 改回 path + 清 pending。"""
    path = args.get("path")
    if not path:
        return
    ok = fs.undo_commit(path)
    # 復原後回傳舊檔現況 (查 manifest)
    on_file_query(ctx, {"path": path})


def on_file_promote(ctx, args):
    """FILE_PROMOTE (0x2011): SD 暫存 → 根目錄正式上線（自動 .bak 備份）。"""
    src = args.get("src")
    dst = args.get("dst")
    if not src or not dst:
        _send(ctx, 0x2010, {
            "err_no_space": 0, "err_write_fail": 1, "err_offset_mismatch": 0,
            "err_id_mismatch": 0, "err_sha_mismatch": 0, "err_not_active": 0,
            "err_busy": 0, "failed_offset": 0, "written_up_to": 0, "path": str(dst),
        })
        return
    ok = fs.promote_file(src, dst)
    if not ok:
        _send(ctx, 0x2010, {
            "err_no_space": 0, "err_write_fail": 1, "err_offset_mismatch": 0,
            "err_id_mismatch": 0, "err_sha_mismatch": 0, "err_not_active": 0,
            "err_busy": 0, "failed_offset": 0, "written_up_to": 0, "path": str(dst),
        })
        return
    # 成功：回 FILE_QUERY_RSP（path = 真正的根目錄 dst，不經 resolve 映射）
    import os
    exists = 0
    size = 0
    try:
        st = os.stat(dst)
        exists = 1
        size = st[6]
    except Exception:
        pass
    _send(ctx, 0x2006, {
        "exists": exists, "sha256": b'\x00' * 32, "size": size,
        "path": dst, "free": fs.free_bytes("/sd"), "pending": 0,
    })


def on_file_move(ctx, args):
    """FILE_MOVE (0x200D): 通用改名/移動 (走 manifest, 不碰 delta)。"""
    src = args.get("src")
    dst = args.get("dst")
    if not src or not dst:
        return
    ok = fs.move_file(src, dst)
    if not ok:
        _send(ctx, 0x2010, {
            "err_no_space": 0,
            "err_write_fail": 1,
            "err_offset_mismatch": 0,
            "err_id_mismatch": 0,
            "err_sha_mismatch": 0,
            "err_not_active": 0,
            "err_busy": 0,
            "failed_offset": 0,
            "written_up_to": 0,
            "path": fs.resolve(src)[1]
        })


def on_file_partial_query(ctx, args):
    """FILE_PARTIAL_QUERY (0x200E): 回傳斷點續傳進度。"""
    path = args.get("path")
    if not path:
        return
    partial, written, total_size, sha_hex, full = fs.partial_query(path)
    _send(ctx, 0x200F, {
        "partial": partial,
        "written": written,
        "total_size": total_size,
        "sha256": _hex_to_bytes(sha_hex) if sha_hex else b'\x00' * 32,
        "path": full
    })


def register(app):
    app.disp.on(0x2001, on_file_begin)
    app.disp.on(0x2002, on_file_chunk)
    app.disp.on(0x2003, on_file_end)
    app.disp.on(0x2005, on_file_query)
    app.disp.on(0x2007, on_file_read)
    app.disp.on(0x2008, on_file_confirm)
    app.disp.on(0x2009, on_file_delete)
    app.disp.on(0x200A, on_file_undo)
    app.disp.on(0x200B, on_file_scan)
    app.disp.on(0x200D, on_file_move)
    app.disp.on(0x200E, on_file_partial_query)
    app.disp.on(0x2011, on_file_promote)
