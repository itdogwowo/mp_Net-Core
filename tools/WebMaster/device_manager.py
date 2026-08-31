"""WebMaster 設備連線管理。

每個 slave 透過 WS 連入 /ws/{slave_id}, 本模組持有該連線並提供:
  - send(cmd, args): 單向發送 (廣播/串流控制, 不等回應)
  - request(cmd, args, expect, timeout): 發送 + 等待指定回應命令 (含超時)
  - 心跳餵狗 / 離線偵測
  - 0x1102 狀態快取 + 0x1101 主動查詢

回應匹配採 per-device 的 asyncio.Future dict, 依「期待的回應命令 id」路由。
注意: NC4 的 Proto.pack 回傳共享 memoryview, 本模組一律立刻 bytes() 拷貝再 await 送出。
"""
import asyncio
import json
import os
import time
import logging

from protocol import protocol, StreamParser

# 保留路徑：這些不是設備，別當成 slave 註冊/列入 known
RESERVED = {"ui", "static", "media", "api"}

log = logging.getLogger("webmaster.device")


class Device:
    def __init__(self, slave_id, ws, addr=None):
        self.slave_id = slave_id
        self.ws = ws
        self.addr = addr
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.parser = StreamParser()
        self.status = {}           # 最近一次 0x1102 狀態 dict
        self._pending = {}         # expect_cmd_id -> asyncio.Future
        self._lock = asyncio.Lock()

    # ── 發送 ──────────────────────────────────────────────
    async def send(self, cmd_id, args):
        """單向發送 (不回傳回應)。立即 bytes() 拷貝, 避免共享 buffer 被下一次 pack 覆蓋。"""
        data = bytes(protocol.pack(cmd_id, args))
        await self.ws.send_bytes(data)

    async def request(self, cmd_id, args, expect, timeout=5.0):
        """發送 cmd_id, 等待 expect 回應命令; 回傳 (expect_cmd_id, args_dict) 或 None (超時)。"""
        fut = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending[expect] = fut
            await self.send(cmd_id, args)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(expect, None)

    # ── 收到一幀 ──────────────────────────────────────────
    async def feed_bytes(self, data: bytes):
        self.parser.feed(data)
        while True:
            r = self.parser.pop_frame()
            if r is None:
                break
            ver, addr, cmd, payload = r
            self.last_seen = time.time()
            await self._dispatch(cmd, bytes(payload))

    async def _dispatch(self, cmd, payload):
        args = protocol.unpack(cmd, payload)
        if args is None:
            return
        name = protocol.name(cmd)
        # 1. 狀態心跳 0x1102 → 快取
        if cmd == 0x1102:
            import json
            try:
                self.status = json.loads(args.get("status_json", "{}"))
            except Exception:
                self.status = args
        # 2. 滿足等待中的 future
        fut = self._pending.get(cmd)
        if fut is not None and not fut.done():
            fut.set_result((cmd, args))
        log.debug("[%s] rx 0x%04X %s", self.slave_id, cmd, name)

    # ── 狀態查詢 ──────────────────────────────────────────
    async def query_status(self, timeout=2.0):
        r = await self.request(0x1101, {"query_type": 0}, expect=0x1102, timeout=timeout)
        return self.status if r is not None else None


class DeviceManager:
    def __init__(self, known_path=None):
        self.devices = {}          # slave_id -> Device (目前已連線)
        self.ui_clients = set()    # 瀏覽器 WS (WebSocket 物件)
        self.known = {}            # slave_id -> {addr, last_seen, play_id} (所有已知設備)
        self.known_path = known_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "devices.json")
        self._load_known()

    # ── 已知設備註冊 (跨連線累積, 供「所有 slave 概況」) ────
    def _load_known(self):
        # 1. WebMaster 自己的 devices.json
        try:
            if os.path.isfile(self.known_path):
                with open(self.known_path, "r", encoding="utf-8") as f:
                    self.known = json.loads(f.read())
        except Exception:
            self.known = {}
        # 清掉保留路徑殘留（舊 bug 把 /ws/ui 誤註冊成 "ui" 設備）
        for k in list(self.known.keys()):
            if k in RESERVED:
                del self.known[k]
        # 2. 種子: NetBusMaster 的 slave_map.json (若有)
        try:
            smap = os.path.abspath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "PC", "slave_map.json"))
            if os.path.isfile(smap):
                with open(smap, "r", encoding="utf-8") as f:
                    cfg = json.loads(f.read())
                for sid, info in (cfg.get("mapping") or {}).items():
                    self.known.setdefault(sid, {})
                    if info.get("ip"):
                        self.known[sid]["addr"] = info["ip"]
                    if "play_id" in info:
                        self.known[sid]["play_id"] = info["play_id"]
        except Exception:
            pass

    def _save_known(self):
        try:
            with open(self.known_path, "w", encoding="utf-8") as f:
                json.dump(self.known, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── 註冊/移除 ─────────────────────────────────────────
    def register(self, slave_id, ws, addr=None):
        # 保留路徑（如 /ws/ui 被誤導到這裡）不是設備，直接拒掉
        if slave_id in RESERVED:
            log.warning("拒絕保留路徑連線: %s", slave_id)
            return None
        dev = Device(slave_id, ws, addr)
        self.devices[slave_id] = dev
        # 更新已知設備清單
        self.known.setdefault(slave_id, {})
        if addr:
            self.known[slave_id]["addr"] = addr
        self.known[slave_id]["last_seen"] = time.time()
        self._save_known()
        log.info("slave 上線: %s", slave_id)
        return dev

    def unregister(self, slave_id, dev=None):
        """移除設備。若傳入 dev，只移除「當下這條連線」註冊的那個（避免舊連線的 finally
        把「同 id 新連線」踢掉造成假離線）。"""
        cur = self.devices.get(slave_id)
        if cur is not None and (dev is None or cur is dev):
            self.devices.pop(slave_id, None)
            log.info("slave 離線: %s", slave_id)

    def get(self, slave_id):
        return self.devices.get(slave_id)

    def list_devices(self):
        """回傳「所有已知設備」概況，依 slave_id 排序，標 online 狀態。

        offline 的設備 (只進過已知清單、目前未連線) 也會列出，方便看全貌。
        """
        now = time.time()
        result = []
        for sid, rec in sorted(self.known.items()):
            d = self.devices.get(sid)
            if d is not None:
                result.append({
                    "slave_id": sid,
                    "addr": d.addr,
                    "online": True,
                    "uptime_s": int(now - d.connected_at),
                    "status": d.status,
                    "play_id": rec.get("play_id"),
                })
            else:
                result.append({
                    "slave_id": sid,
                    "addr": rec.get("addr"),
                    "online": False,
                    "uptime_s": 0,
                    "status": {},
                    "play_id": rec.get("play_id"),
                })
        return result

    # ── 瀏覽器 UI 廣播 ────────────────────────────────────
    async def broadcast_ui(self, message: dict):
        import json
        dead = []
        for ws in list(self.ui_clients):
            try:
                await ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ui_clients.discard(ws)

    # ── 心跳 / 離線偵測 (背景迴圈) ────────────────────────
    async def heartbeat_loop(self, interval=2.0, offline_after=30.0):
        # 心跳 2s 一次：讓設備 _last_rx 頻繁刷新，避免設備端 ws_stale_ms(30s) 判逾時
        """定期：
        1. 向在線設備送 0x1101 STATUS_GET（保活兩端：設備收到→抬 idle_ms，
           設備回 0x1102→更新 last_seen 與 status，避免設備「30s 無流量→斷線重連」）。
        2. 偵測心跳逾時標離線。
        3. 廣播 device_list 給 UI。
        """
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            for sid, d in list(self.devices.items()):
                # 保活心跳（fire-and-forget，不佔用 request 的 pending）
                try:
                    await d.send(0x1101, {"query_type": 0})
                except Exception:
                    pass
                if now - d.last_seen > offline_after:
                    log.warning("slave %s 心跳逾時, 標記離線", sid)
                    self.unregister(sid)
            await self.broadcast_ui({"type": "device_list", "data": self.list_devices()})


manager = DeviceManager()
