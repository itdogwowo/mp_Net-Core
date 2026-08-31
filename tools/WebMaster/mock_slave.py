"""Mock slave: 模擬一台 mp_Net-Core 設備連入 WebMaster, 走通端到端。

用 NC4 協議實作最小回應邏輯:
  - 0x2001 FILE_BEGIN   → 記錄 session (不回應)
  - 0x2005 FILE_QUERY   → 回 0x2006 (exists/sha/size)
  - 0x2002 FILE_CHUNK   → 累積資料並回 0x2004 FILE_ACK
  - 0x2003 FILE_END     → 回 0x2006 最終 sha
  - 0x1101 STATUS_GET   → 回 0x1102 狀態
  - 0x30xx 串流         → 印 log, 不回 (fire-and-forget)

用法: python3 -B mock_slave.py <ws_url> <slave_id>
"""
import asyncio
import sys
import os
import hashlib
import json

_DIR = os.path.dirname(os.path.abspath(__file__))
_SLAVE = os.path.abspath(os.path.join(_DIR, "..", "..", "slave"))
if _SLAVE not in sys.path:
    sys.path.insert(0, _SLAVE)

from lib.sys.proto import Proto, StreamParser  # noqa: E402
from lib.sys.schema_loader import SchemaStore   # noqa: E402
from lib.sys.schema_codec import SchemaCodec    # noqa: E402

try:
    import websockets
    _HAS_WS = True
except ImportError:
    _HAS_WS = False


class MockSlave:
    def __init__(self):
        self.store = SchemaStore(dir_path=os.path.join(_SLAVE, "schema"))
        self.store.finalize()
        self.parser = StreamParser()
        # 檔案區：存「上傳的資料」+ 一份預設 /config.json 供下載
        self.files = {"/config.json": b'{"System":{"master_port":8000,"debug_level":1}}'}
        self.buf = bytearray()
        self.total = 0
        self.sha_expect = b""

    def pack(self, cmd, args):
        d = self.store.get(cmd)
        return bytes(Proto.pack(cmd, SchemaCodec.encode(d, args)))

    def unpack(self, cmd, payload):
        d = self.store.get(cmd)
        return SchemaCodec.decode(d, payload, store=self.store)

    async def handle(self, data, send):
        self.parser.feed(data)
        while True:
            r = self.parser.pop_frame()
            if r is None:
                break
            ver, addr, cmd, payload = r
            args = self.unpack(cmd, bytes(payload))
            name = self.store.get(cmd).get("name")
            print(f"    ← 0x{cmd:04X} {name} {args}")

            if cmd == 0x2001:
                self.buf = bytearray()
                self.total = int(args.get("total_size", 0))
                self.sha_expect = args.get("sha256", b"")
            elif cmd == 0x2005:
                p = args.get("path", "")
                data = self.files.get(p, bytes(self.buf)) if p in self.files else bytes(self.buf)
                await send(self.pack(0x2006, {
                    "exists": 1, "sha256": hashlib.sha256(data).digest(),
                    "size": len(data), "path": p, "free": 1 << 20, "pending": 0,
                }))
            elif cmd == 0x2002:
                off = int(args.get("offset", 0))
                data = bytes(args.get("data", b""))
                need = off + len(data)
                if len(self.buf) < need:
                    self.buf += bytearray(need - len(self.buf))
                self.buf[off:off + len(data)] = data
                await send(self.pack(0x2004, {"file_id": args.get("file_id", 0), "offset": off}))
            elif cmd == 0x2003:
                sha = hashlib.sha256(bytes(self.buf)).digest()
                await send(self.pack(0x2006, {
                    "exists": 1, "sha256": sha, "size": len(self.buf),
                    "path": "/mock", "free": 1 << 20, "pending": 0,
                }))
                print(f"    → FILE_END sha={sha.hex()[:8]} size={len(self.buf)}")
            elif cmd == 0x2007:
                # FILE_READ: 回檔案分塊 (0x2002 FILE_CHUNK)
                off = int(args.get("offset", 0))
                length = int(args.get("length", 2048))
                p = args.get("path", "")
                data = self.files.get(p, bytes(self.buf))
                chunk = data[off:off + length]
                await send(self.pack(0x2002, {"file_id": 0, "offset": off, "data": chunk}))

            elif cmd == 0x1101:
                await send(self.pack(0x1102, {
                    "status_json": json.dumps({
                        "stream_active": True, "stream_pos_frame": 0,
                        "played_frames": 0, "mem_free": 100000, "slave_id": "MOCK",
                    }),
                }))
            elif cmd in (0x3001, 0x3002, 0x3004, 0x3005, 0x3009, 0x300A):
                pass  # 串流 fire-and-forget


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000/ws/MOCK"
    slave_id = sys.argv[2] if len(sys.argv) > 2 else "MOCK"

    if not _HAS_WS:
        print("需要 websockets 套件: pip install websockets")
        sys.exit(1)

    import websockets
    mock = MockSlave()
    print(f"mock slave 連線: {url}")
    async with websockets.connect(url) as ws:
        print("已連線 (slave id:", slave_id, ")")
        async def _send(pkt):
            await ws.send(pkt)
        while True:
            try:
                data = await ws.recv()
            except websockets.ConnectionClosed:
                print("連線關閉")
                break
            if isinstance(data, (bytes, bytearray)):
                await mock.handle(bytes(data), _send)


if __name__ == "__main__":
    asyncio.run(main())
