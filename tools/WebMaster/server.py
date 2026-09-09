"""WebMaster 服務入口 (FastAPI + WebSocket)。

端點:
  GET  /                    → 靜態 SPA (static/index.html)
  WS   /ws/{slave_id}       → slave 連入 (binary NC 幀)
  WS   /ws/ui               → 瀏覽器連入 (JSON 控制 + 訂閱設備狀態)
  GET  /api/devices         → 設備清單
  GET  /api/mp3             → MP3 清單
  GET  /media/{name}        → MP3 檔案 (供 <audio> 播放)
  POST /api/upload/{slave_id} → 上傳檔案到指定設備 (multipart)

啟動: uvicorn server:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from protocol import protocol
from device_manager import manager, Device
import transfer, stream, audio, firmware
import poe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("webmaster")

app = FastAPI(title="NetBus WebMaster")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ═══════════════════════ ViperIDE（分頁: USB 燒錄 bin / 上傳項目檔案）═══
# source: viper-ide/（vendored 在 WebMaster 底下，可魔改）；build 產物: viper-ide/build。
# build 用 VIPER_IDE_BASE_URL=. 產出相對路徑版，才能掛在 /viper/ 子路徑
# （同源 iframe → WebSerial 可用）。缺 build 時 server 照常啟動，前端會提示。
VIPER_SRC_DIR = os.path.join(os.path.dirname(__file__), "viper-ide")
VIPER_BUILD_DIR = os.path.join(VIPER_SRC_DIR, "build")
VIPER_INDEX = os.path.join(VIPER_BUILD_DIR, "index.html")


def _viper_version():
    try:
        with open(os.path.join(VIPER_SRC_DIR, "package.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


VIPER_BUILT = os.path.isfile(VIPER_INDEX)
if VIPER_BUILT:
    app.mount("/viper", StaticFiles(directory=VIPER_BUILD_DIR, html=True), name="viper")
    log.info("ViperIDE build 就緒 → /viper/（version=%s）", _viper_version())
else:
    log.warning("ViperIDE 未建置（缺 %s）— /viper/ 不掛載", VIPER_INDEX)


# ═══════════════════════ mp_web_ide 燒錄頁（/flash/）═══════════════
# source: mp_web_ide/（白室重建第一步：esptool-js 燒錄 .bin UI）；
# build: cd mp_web_ide && npm run build → dist/（vite base='./'，可掛子路徑）
MP_IDE_DIR = os.path.join(os.path.dirname(__file__), "mp_web_ide")
MP_DIST_DIR = os.path.join(MP_IDE_DIR, "dist")
MP_INDEX = os.path.join(MP_DIST_DIR, "index.html")

if os.path.isfile(MP_INDEX):
    app.mount("/flash", StaticFiles(directory=MP_DIST_DIR, html=True), name="mp-flash")
    log.info("mp_web_ide 就緒 → /flash/（同源 → WebSerial 可用）")
else:
    log.warning("mp_web_ide 未建置（缺 %s）— /flash/ 不掛載", MP_INDEX)


# ═══════════════════════ 靜態 / 頁面 ═══════════════════════
@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>WebMaster</h1><p>static/index.html 缺失</p>")


# ═══════════════════════ REST API ═══════════════════════
@app.get("/api/viper")
async def api_viper():
    """ViperIDE 分頁狀態：build 是否就緒 + vendored 版本（供前端 lazy 載入/提示）。"""
    return JSONResponse({
        "ok": True,
        "built": os.path.isfile(VIPER_INDEX),
        "version": _viper_version(),
        "base": "/viper/",
    })


@app.get("/api/devices")
async def api_devices():
    return JSONResponse({"ok": True, "data": manager.list_devices()})


@app.get("/api/mp3")
async def api_mp3():
    return JSONResponse({"ok": True, "data": audio.list_mp3()})


@app.get("/api/commands")
async def api_commands():
    """列出所有 NC4 命令 (cmd_id + 名稱 + 參數欄位)，供 console 下拉。"""
    cmds = []
    for cid, cdef in sorted(protocol.store.cmd_map.items()):
        cmds.append({
            "cmd": f"0x{cid:04X}",
            "name": cdef.get("name", ""),
            "fields": [f.get("name", "") for f in cdef.get("payload", [])],
        })
    return JSONResponse({"ok": True, "data": cmds})


@app.get("/media/{name}")
async def media_mp3(name: str):
    path = audio.resolve_mp3(name)
    if path is None:
        return JSONResponse({"ok": False, "err": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/upload/{slave_id}")
async def api_upload(slave_id: str, request: Request):
    """上傳 raw 檔案 body 到指定設備 (remote_path/chunk_size 用 query 參數)。

    用 raw body 而非 multipart, 避免額外依賴 python-multipart。
    """
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    remote_path = request.query_params.get("remote_path", "/sd/upload.bin")
    try:
        chunk_size = int(request.query_params.get("chunk_size", 4096))
    except ValueError:
        chunk_size = 4096
    data = await request.body()
    try:
        sha = await transfer.upload(dev, data, remote_path, chunk_size=chunk_size)
        return JSONResponse({"ok": True, "size": len(data), "sha": sha.hex()})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.get("/api/files/{slave_id}")
async def api_files(slave_id: str):
    """列出設備 manifest 檔案（含 pending 狀態）。

    回傳 {path: {s: size, h: sha, pending: bool}}。
    """
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        files = await transfer.list_files(dev)
        if files is None:
            return JSONResponse({"ok": False, "err": "manifest 讀取失敗"}, status_code=500)
        return JSONResponse({"ok": True, "data": files})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.get("/api/download/{slave_id}")
async def api_download(slave_id: str, path: str):
    """下載設備檔案 → 回傳原始 bytes。"""
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        data = await transfer.download(dev, path)
        if data is None:
            return JSONResponse({"ok": False, "err": "檔案不存在"}, status_code=404)
        fname = os.path.basename(path) or "download.bin"
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.post("/api/delete/{slave_id}")
async def api_delete(slave_id: str, path: str):
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        ok = await transfer.delete(dev, path)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.post("/api/confirm/{slave_id}")
async def api_confirm(slave_id: str, path: str):
    """確認覆蓋 → 刪 .bak + 清 pending（正式生效）。"""
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        ok = await transfer.confirm(dev, path)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.post("/api/undo/{slave_id}")
async def api_undo(slave_id: str, path: str):
    """復原 → 刪新檔 + .bak 蓋回舊版。"""
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        ok = await transfer.undo(dev, path)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.post("/api/promote/{slave_id}")
async def api_promote(slave_id: str, path: str):
    """/sd 暫存 → root 正式上線。"""
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        ok = await transfer.promote(dev, path)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


def _get_local_ip(_connect_to=("10.255.255.255", 9000)):
    """找本機對外 LAN IP（供 DISCOVER 的 ws_url 使用）。"""
    import socket as _s
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            s.connect(_connect_to)
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return _s.gethostbyname(_s.gethostname())
        except Exception:
            return "127.0.0.1"


@app.post("/api/knock")
async def api_knock(request: Request):
    """DISCOVER (0x1001) 敲門：讓設備連回本 master。

    query 參數:
      ip          = 定向 unicast 到該 IP:9000（多個逗號分隔）
      broadcast   = 1 廣播到 255.255.255.255 + 子網廣播
      port        = 本 master 的 WS port（預設 8000，用於 ws_url）
    設備 UDP 收到 0x1001 後會依 ws_url 主動連回 /ws/{slave_id}。
    """
    ip = request.query_params.get("ip", "")
    broadcast = request.query_params.get("broadcast", "0") in ("1", "true", "yes")
    try:
        port = int(request.query_params.get("port", "8000") or 8000)
    except ValueError:
        port = 8000
    local_ip = _get_local_ip()
    # 帶上 /ws 前綴：設備 on_discover 會拼成 ws://ip:port/ws/<slave_id>，
    # 對上 WebMaster 的 /ws/{slave_id} 路由（否則連到 /<slave_id> 會 404）。
    ws_url = f"ws://{local_ip}:{port}/ws"
    pkt = bytes(protocol.pack(0x1001, {"server_ip": local_ip, "ws_url": ws_url}))
    import socket as _s
    udp = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    udp.settimeout(1.0)
    targets = []
    if broadcast:
        sub_bc = local_ip.rsplit(".", 1)[0] + ".255"
        targets = ["255.255.255.255", sub_bc]
    elif ip:
        targets = [t.strip() for t in ip.replace("，", ",").split(",") if t.strip()]
    sent = 0
    try:
        for t in targets:
            for _ in range(3):
                try:
                    udp.sendto(pkt, (t, 9000))
                    sent += 1
                except Exception:
                    pass
    finally:
        udp.close()
    log.info("knock → targets=%s (ws_url=%s)", targets, ws_url)
    return JSONResponse({"ok": True, "sent": sent, "targets": targets, "ws_url": ws_url})


@app.post("/api/poe")
async def api_poe(action: str = "restart", dry_run: str = "1", switches: str = "", ports: str = ""):
    """交換器 PoE 電源控制 (重用 tools/PC/poe_restart.py)。

    query 參數:
      action    = restart / off / on
      dry_run   = 1 預覽指令唔真做 (預設 1)
      switches  = 逗號分隔交換器名 (空 = 兩台都要)
      ports     = "3,5,10-15" (空 = 全部 1-45)
    回傳 {ok, output}。
    """
    dry = dry_run in ("1", "true", "yes")
    sw = [s.strip() for s in switches.split(",") if s.strip()] if switches else []
    try:
        out, ok = poe.run_poe(action, sw, ports, dry)
        return JSONResponse({"ok": ok, "output": out})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


@app.post("/api/firmware/{slave_id}")
async def api_firmware(slave_id: str, dry_run: str = "1", confirm: str = "1", reboot: str = "0"):
    """固件全量更新（delta）：比對本地 slave/ 與設備 manifest，只上傳差異檔。

    查詢參數:
      dry_run=1  只比對列出差異，不上傳 (預設 1)
      confirm=1  上傳後確認（清 pending，正式生效）(預設 1)
      reboot=1   上傳完軟重啟 (預設 0)
    回傳 {total, changed, matched, uploaded, dry_run}
    """
    dev = manager.get(slave_id)
    if dev is None:
        return JSONResponse({"ok": False, "err": "slave 離線"}, status_code=404)
    try:
        result = await firmware.firmware_update(
            dev,
            dry_run=dry_run in ("1", "true", "yes"),
            confirm=confirm in ("1", "true", "yes"),
            reboot=reboot in ("1", "true", "yes"),
        )
        return JSONResponse({"ok": True, "data": result})
    except Exception as e:
        return JSONResponse({"ok": False, "err": str(e)}, status_code=500)


# ═══════════════════════ WebSocket: UI ═══════════════════════
# 注意：/ws/ui 必須宣告在 /ws/{slave_id} 之前，否則 UI 連線會被 {slave_id} 攔截
# （FastAPI 依註冊順序匹配），被當成 slave ("ui") 而用 receive_bytes 收文字 → 'bytes' 錯誤。
@app.websocket("/ws/ui")
async def ws_ui(websocket: WebSocket):
    await websocket.accept()
    manager.ui_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "device_list", "data": manager.list_devices()}, ensure_ascii=False))
        while True:
            raw = await websocket.receive_text()
            await handle_ui_message(websocket, raw)
    except WebSocketDisconnect:
        pass
    finally:
        manager.ui_clients.discard(websocket)


# ═══════════════════════ WebSocket: slave ═══════════════════════
@app.websocket("/ws/{slave_id}")
async def ws_slave(websocket: WebSocket, slave_id: str):
    await websocket.accept()
    dev = manager.register(slave_id, websocket, addr=websocket.client.host if websocket.client else None)
    if dev is None:
        await websocket.close()
        return
    await manager.broadcast_ui({"type": "device_list", "data": manager.list_devices()})
    # 連上立刻送 0x1101：讓設備一連上就刷新 _last_rx，避免設備端
    # ws_stale_ms 判逾時 → 自我斷線重連（flapping）。
    try:
        await dev.send(0x1101, {"query_type": 0})
    except Exception:
        pass
    try:
        while True:
            data = await websocket.receive_bytes()
            await dev.feed_bytes(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("slave %s 連線異常: %s", slave_id, e)
    finally:
        # 只移除「這條連線」註冊的設備，避免同 id 重連時被舊連線踢掉
        manager.unregister(slave_id, dev)
        await manager.broadcast_ui({"type": "device_list", "data": manager.list_devices()})


async def handle_ui_message(ws: WebSocket, raw: str):
    """處理瀏覽器送來的 JSON 控制指令。"""
    try:
        msg = json.loads(raw)
    except Exception:
        await ws.send_text(json.dumps({"type": "error", "err": "invalid json"}))
        return

    action = msg.get("action")
    slave_id = msg.get("slave_id")
    dev = manager.get(slave_id) if slave_id else None

    if action == "ping":
        await ws.send_text(json.dumps({"type": "pong"}))

    elif action == "device_list":
        await ws.send_text(json.dumps({"type": "device_list", "data": manager.list_devices()}, ensure_ascii=False))

    elif action == "stream_prepare" and dev:
        await stream.prepare(dev, msg.get("file_name", "data.bin"),
                             int(msg.get("block_id", 0)), int(msg.get("play_mode", 0)))
        await ws.send_text(json.dumps({"type": "ok", "action": action}))

    elif action == "stream_play" and dev:
        await stream.play(dev, int(msg.get("start_frame", 0)))
        await ws.send_text(json.dumps({"type": "ok", "action": action}))

    elif action == "stream_pause" and dev:
        await stream.pause(dev, bool(msg.get("paused", True)))
        await ws.send_text(json.dumps({"type": "ok", "action": action}))

    elif action == "stream_stop" and dev:
        await stream.stop(dev)
        await ws.send_text(json.dumps({"type": "ok", "action": action}))

    elif action == "stream_seek" and dev:
        await stream.seek(dev, int(msg.get("frame", 0)))
        await ws.send_text(json.dumps({"type": "ok", "action": action}))

    elif action == "stream_fps" and dev:
        await stream.set_fps(dev, int(msg.get("fps", 40)))
        await ws.send_text(json.dumps({"type": "ok", "action": action}))

    elif action == "ram_upload" and dev:
        # 上傳資料到 RAM 緩衝區 (實時播放)
        import base64
        b64 = msg.get("data_b64", "")
        remote = msg.get("remote_path", "/ram/live.bin")
        try:
            data = base64.b64decode(b64)
            sha = await transfer.upload(dev, data, remote, chunk_size=int(msg.get("chunk_size", 4096)))
            await ws.send_text(json.dumps({"type": "ok", "action": action, "sha": sha.hex(), "size": len(data)}))
        except Exception as e:
            await ws.send_text(json.dumps({"type": "error", "err": str(e)}))

    elif action in ("file_list", "file_download", "file_delete", "file_confirm",
                    "file_undo", "file_promote") and dev:
        # 檔案操作經 WS 統一入口 (叫 REST 相同邏輯)
        await handle_file_ws(ws, dev, action, msg)

    elif action == "cmd" and dev:
        # 通用命令 console: 送任意 NC4 命令 + 等回應
        await handle_cmd_ws(ws, dev, msg)

    else:
        await ws.send_text(json.dumps({"type": "error", "err": f"unknown action: {action}"}))


async def handle_file_ws(ws: WebSocket, dev, action: str, msg: dict):
    """檔案操作統一入口（WS）。msg 含 path；list 不含 path。"""
    path = msg.get("path", "")
    try:
        if action == "file_list":
            files = await transfer.list_files(dev)
            await ws.send_text(json.dumps(
                {"type": "ok", "action": action, "data": files}, ensure_ascii=False))

        elif action == "file_download":
            import base64
            data = await transfer.download(dev, path)
            await ws.send_text(json.dumps({
                "type": "ok", "action": action, "path": path,
                "size": len(data) if data else 0,
                "data_b64": base64.b64encode(data or b"").decode()}))

        elif action == "file_delete":
            ok = await transfer.delete(dev, path)
            await ws.send_text(json.dumps({"type": "ok", "action": action, "path": path, "ok": ok}))

        elif action == "file_confirm":
            ok = await transfer.confirm(dev, path)
            await ws.send_text(json.dumps({"type": "ok", "action": action, "path": path, "ok": ok}))

        elif action == "file_undo":
            ok = await transfer.undo(dev, path)
            await ws.send_text(json.dumps({"type": "ok", "action": action, "path": path, "ok": ok}))

        elif action == "file_promote":
            ok = await transfer.promote(dev, path)
            await ws.send_text(json.dumps({"type": "ok", "action": action, "path": path, "ok": ok}))

        else:
            await ws.send_text(json.dumps({"type": "error", "err": f"unknown file action: {action}"}))
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "err": str(e)}))


def _cvid(v):
    """'0x1101' 或 4353 → int。"""
    if isinstance(v, str) and v.lower().startswith("0x"):
        return int(v, 16)
    return int(v)


async def handle_cmd_ws(ws: WebSocket, dev, msg: dict):
    """通用命令 console：送任意 NC4 命令並（若指定 expect）等回應。

    msg: {cmd_id, args: {...}, expect?, timeout?}
    """
    try:
        cmd_id = _cvid(msg["cmd_id"])
        args = msg.get("args", {})
        expect = msg.get("expect")
        timeout = float(msg.get("timeout", 5.0))
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "err": f"bad cmd: {e}"}))
        return

    try:
        if expect is None:
            await dev.send(cmd_id, args)   # 單向（廣播/串流控制，不等回應）
            await ws.send_text(json.dumps({"type": "ok", "action": "cmd", "sent": True}))
        else:
            r = await dev.request(cmd_id, args, expect=_cvid(expect), timeout=timeout)
            if r is None:
                await ws.send_text(json.dumps({"type": "error", "err": "timeout"}))
            else:
                got_cmd, got_args = r
                await ws.send_text(json.dumps({
                    "type": "ok", "action": "cmd",
                    "resp": {"cmd": got_cmd, "args": got_args}}, ensure_ascii=False))
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "err": str(e)}))


# ═══════════════════════ 啟動 (背景心跳) ═══════════════════════
@app.on_event("startup")
async def startup():
    asyncio.create_task(manager.heartbeat_loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
