from lib.proto import Proto
from lib.schema_codec import SchemaCodec
import ubinascii
from lib.sys_bus import bus
from lib.fs_manager import fs
import _thread
import struct

def on_file_begin(ctx, args):
    ok = fs.begin_write(args)
    if ok:
        print(f"📂 [File] Start -> {fs.session['path']} (Atomic)")
    else:
        try:
            print(f"❌ [File] Begin Failed: {fs.session.get('last_error')}")
        except Exception:
            pass
    if "send" in ctx:
        try:
            fid = int(args.get("file_id", 0) or 0) & 0xFFFF
            aoff = 0xFFFFFFFE if ok else 0xFFFFFFFF
            ack_data = struct.pack("<HI", fid, int(aoff) & 0xFFFFFFFF)
            ctx["send"](Proto.pack(0x2004, ack_data))
        except Exception:
            pass

def on_file_chunk(ctx, args):
    app = ctx["app"]
    if fs.write_chunk(args):
        # 🚀 關鍵：每收到一包就回傳 ACK
        # 讓 PC 知道可以發下一包了
        if "send" in ctx:
            try:
                fid = int(args.get("file_id", 0) or 0) & 0xFFFF
                off = int(args.get("offset", 0) or 0) & 0xFFFFFFFF
                ack_data = struct.pack("<HI", fid, off)
                ctx["send"](Proto.pack(0x2004, ack_data))
            except Exception:
                pass
    else:
        # ⚠️ 如果寫入失敗，打印原因方便調試
        print(f"⚠️ [File] Chunk Failed: Off={args.get('offset')} Err={fs.session['last_error']}")
        if "send" in ctx:
            try:
                off = int(args.get("offset", 0) or 0) & 0xFFFFFFFF
                nack_off = off | 0x80000000
                fid = int(args.get("file_id", 0) or 0) & 0xFFFF
                ack_data = struct.pack("<HI", fid, nack_off & 0xFFFFFFFF)
                ctx["send"](Proto.pack(0x2004, ack_data))
            except Exception:
                pass

def on_file_end(ctx, args):
    app = ctx["app"]
    # 執行校驗 (內部會調用 fs.atomic_write_finalize)
    ok = fs.end_write(args)
    
    path = fs.session["path"]
    sha = fs.session["last_sha_hex"]
    
    if ok:
        print("-" * 40)
        print(f"🏁 [File] End Success: {path}")
        print(f"🔒 [SHA256] {sha}")
        print("-" * 40)

        # 回覆最終狀態 (0x2006)
        # 優先從 manifest 取值
        if path in fs.manifest:
            entry = fs.manifest[path]
            size = entry["s"]
            try:
                sha_bytes = ubinascii.unhexlify(entry["h"])
            except:
                sha_bytes = b'\x00'*32
        else:
            # Fallback
            import os
            try:
                st = os.stat(path)
                size = st[6]
                sha_bytes = ubinascii.unhexlify(sha) if sha else b'\x00'*32
            except:
                size = 0
                sha_bytes = b'\x00'*32

        if "send" in ctx:
            try:
                rsp_def = app.store.get(0x2006)
                rsp_data = SchemaCodec.encode(rsp_def, {
                    "exists": 1,
                    "sha256": sha_bytes,
                    "size": size,
                    "path": path
                })
                ctx["send"](Proto.pack(0x2006, rsp_data))
                print(f"📤 [File] Sent Final SHA256 (0x2006)")
            except Exception as e:
                print(f"⚠️ [File] Failed to send final SHA: {e}")
    else:
        err = fs.session['last_error']
        print(f"❌ [File] End Failed: {err}")

def on_file_query(ctx, args):
    app = ctx["app"]
    path = args.get("path")
    
    exists = 0
    sha = b'\x00' * 32
    size = 0
    
    # 優先查 Manifest
    if path in fs.manifest:
        entry = fs.manifest[path]
        exists = 1
        size = entry["s"]
        try:
            sha = ubinascii.unhexlify(entry["h"])
            print(f"🔍 [Query] Cache Hit: {path} (Size:{size})")
        except:
            pass 
    else:
        # Cache Miss: 實時檢查
        try:
            import os
            st = os.stat(path)
            exists = 1
            size = st[6]
            # 實時計算
            sha_hex = fs.calc_sha256(path)
            if sha_hex:
                sha = ubinascii.unhexlify(sha_hex)
            print(f"🔍 [Query] Realtime: {path} (Size:{size})")
        except:
            print(f"🔍 [Query] {path} not found.")

    # 回傳結果
    if "send" in ctx:
        rsp_def = app.store.get(0x2006)
        rsp_data = SchemaCodec.encode(rsp_def, {
            "exists": exists,
            "sha256": sha,
            "size": size,
            "path": path
        })
        ctx["send"](Proto.pack(0x2006, rsp_data))

def on_file_read(ctx, args):
    app = ctx["app"]
    path = args.get("path")
    offset = args.get("offset", 0)
    length = args.get("length", 1024)
    full_path = path

    try:
        with open(full_path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
            
        if "send" in ctx:
            rsp_def = app.store.get(0x2002)
            rsp_data = SchemaCodec.encode(rsp_def, {
                "file_id": 0,
                "offset": offset,
                "data": data
            })
            ctx["send"](Proto.pack(0x2002, rsp_data))
            print(f"📤 [File] Read Chunk: {full_path} Off:{offset} Len:{len(data)}")
    except Exception as e:
        print(f"❌ [File] Read Failed: {full_path} - {e}")
        # Send empty data to indicate error/eof if needed, or just silence
        if "send" in ctx:
             rsp_def = app.store.get(0x2002)
             rsp_data = SchemaCodec.encode(rsp_def, {
                "file_id": 0,
                "offset": offset,
                "data": b""
            })
             ctx["send"](Proto.pack(0x2002, rsp_data))

def on_file_delete(ctx, args):
    app = ctx["app"]
    path = args.get("path")
    
    if not path: return

    fs.delete_file(path)
    
    # 操作後查詢狀態並回傳
    on_file_query(ctx, {"path": path})

def on_file_scan(ctx, args):
    """
    手動觸發全盤掃描
    """
    print("🔄 [File] Manual Scan Requested")
    _thread.start_new_thread(fs.scan_all, ())

def register(app):
    app.disp.on(0x2001, on_file_begin)
    app.disp.on(0x2002, on_file_chunk)
    app.disp.on(0x2003, on_file_end)
    app.disp.on(0x2005, on_file_query)
    app.disp.on(0x2007, on_file_read)
    app.disp.on(0x2009, on_file_delete)
    app.disp.on(0x200B, on_file_scan)
