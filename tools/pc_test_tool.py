import socket
import time
import threading
import os,sys
import hashlib
import struct
import json


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.chdir(SCRIPT_DIR)
print(PROJECT_ROOT)





# 引用 NL3 協議模型
from slave.lib.proto import Proto, StreamParser
from slave.lib.schema_loader import SchemaStore
from slave.lib.schema_codec import SchemaCodec

# ==================== 全局配置 ====================
DEBUG_MODE = True  # 開啟以監控二進制封包交換

class PCTestTool:
    def __init__(self):
        # 1. 載入 Schema
        self.store = SchemaStore(dir_path=f"{PROJECT_ROOT}/slave/schema")
        self.slaves = {} # { slave_id: {data} }
        self.running = True
        self.local_ip = self.get_local_ip()

    def get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        except: return '127.0.0.1'
        finally: s.close()

    # ==================== 通訊核心 (Server) ====================
    def start_ws_server(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', 8000))
        s.listen(10)
        while self.running:
            try:
                conn, addr = s.accept()
                threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True).start()
            except: break

    def handle_client(self, conn, addr):
        curr_id = f"PENDING_{addr[1]}"
        try:
            # WebSocket Handshake
            raw_req = conn.recv(1024).decode()
            if "Upgrade: websocket" not in raw_req:
                conn.close(); return

            resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
            conn.send(resp.encode())
            
            self.slaves[curr_id] = {
                "conn": conn, "addr": addr, "ack_event": threading.Event(),
                "parser": StreamParser(), "last_seen": time.time(),
                "mem_free": 0, "uptime_ms": 0, "is_identified": False
            }
            p = self.slaves[curr_id]["parser"]
            print(f"\n✨ [Conn] New Slave Connected: {addr[0]}")

            while self.running:
                raw = conn.recv(4096)
                if not raw: break
                
                # WS Binary 解包 ( NL3 封裝在 WS 載荷內 )
                if raw[0] == 0x82:
                    pl_len = raw[1] & 0x7F
                    offset = 2
                    if pl_len == 126: offset = 4
                    elif pl_len == 127: offset = 10
                    payload_data = raw[offset:]
                else: payload_data = raw

                p.feed(payload_data)
                for ver, addr_pkt, cmd, payload in p.pop():
                    curr_id = self.dispatch(curr_id, cmd, payload)
        except: pass
        finally:
            if curr_id in self.slaves: del self.slaves[curr_id]
            conn.close()

    def dispatch(self, cid, cmd, payload):
        """核心分發器：解析來自 MCU 的回報"""
        try:
            c_def = self.store.get(cmd)
            if not c_def: return cid
            args = SchemaCodec.decode(c_def, payload)
            
            # --- ID 識別與更名 ---
            real_id = args.get("slave_id") if cmd == 0x1201 else None
            if cmd == 0x1102: # STATUS_RSP
                try: 
                    real_id = json.loads(args["status_json"]).get("id")
                    data = json.loads(args["status_json"])
                    # 🚀 格式化打印診斷數據
                    print(f"\n--- 🩺 Health Check [{cid}] ---")
                    print(f"  Mem Free: {data.get('mem_free', 0)//1024} KB")
                    print(f"  Render FPS: {data.get('render_fps', 0)}")
                    print(f"  Net In FPS: {data.get('net_in_fps', 0)}")
                    print(f"  Status: {'Active' if data.get('render_fps',0)>0 else 'IDLE'}")
                    print("-" * 35)

                except: 
                    print(f"📥 [Status] {cid}: {args['status_json']}")
                    pass

            if real_id and real_id != cid:
                if cid in self.slaves:
                    self.slaves[real_id] = self.slaves.pop(cid)
                    self.slaves[real_id]["is_identified"] = True
                    print(f"\n🆔 [Identified] {cid} -> {real_id}")
                    cid = real_id

            # --- 數據處理 ---
            if cmd == 0x1201: # HEARTBEAT
                self.slaves[cid].update({"mem_free": args["mem_free"], "uptime_ms": args["uptime_ms"], "last_seen": time.time()})
            elif cmd == 0x3008: # STREAM_READY_ACK
                print(f"\n✅ [MCU Ready] Block {args['block_id']} loaded on {cid}")
            elif cmd == 0x2004: # FILE_ACK
                self.slaves[cid]["ack_event"].set()
            elif cmd == 0x1102:
                print(f"\n📊 [Status] {cid}: {args['status_json']}")
            
            return cid
        except: return cid

    # ==================== 指令發送 ====================
    def send_to_targets(self, targets, cmd_id, args):
        c_def = self.store.get(cmd_id)
        if not c_def: return
        data_pkt = Proto.pack(cmd_id, SchemaCodec.encode(c_def, args))
        
        # 封裝 WS Binary Header
        l = len(data_pkt)
        ws_hdr = bytearray([0x82])
        if l <= 125: ws_hdr.append(l)
        elif l <= 65535: ws_hdr.append(126); ws_hdr.extend(struct.pack(">H", l))
        else: ws_hdr.append(127); ws_hdr.extend(struct.pack(">Q", l))
        
        full_pkt = ws_hdr + data_pkt
        for tid in targets:
            if tid in self.slaves:
                try: self.slaves[tid]["conn"].sendall(full_pkt)
                except: pass

    # ==================== 播放控制介面 ====================
    def stream_menu(self):
        targets = self.select_targets()
        if not targets: return
        
        print("\n--- 🎬 Stream Controller ---")
        print("1. Set Task (Local: data.bin)")
        print("2. Set Task (Semi: data_0/1.bin)")
        print("3. START PLAY (Sync)")
        print("4. PAUSE / RESUME")
        print("5. STOP / BLACK")
        print("6. SEEK (Target Frame)")
        
        c = input("\n👉 Choice: ")
        
        if c == '1': # Set Data.bin
            self.send_to_targets(targets, 0x3009, {"file_name": "data.bin", "block_id": 0, "play_mode": 1})
            print("📡 Task Sent: data.bin (Loop)")
        elif c == '2': # Set Semi
            bid = int(input("Block ID: "))
            self.send_to_targets(targets, 0x3009, {"file_name": f"data_{bid}.bin", "block_id": bid, "play_mode": 0})
        elif c == '3': # Play
            self.send_to_targets(targets, 0x300A, {})
            print("🚀 GLOBAL PLAY SENT")
        elif c == '4': # Pause
            p = int(input("Pause(1) or Resume(0): "))
            self.send_to_targets(targets, 0x3005, {"pause": p})
        elif c == '5': # Stop
            self.send_to_targets(targets, 0x3002, {})
        elif c == '6': # Seek
            f_idx = int(input("Frame index: "))
            self.send_to_targets(targets, 0x3004, {"target_block": 0, "target_frame": f_idx})

    # ==================== 選單 ====================
    def select_targets(self):
        ids = list(self.slaves.keys())
        if not ids: print("❌ Offline"); return []
        for i, sid in enumerate(ids): print(f"{i+1}. {sid} ({self.slaves[sid]['addr'][0]})")
        res = input("👉 Select (num or 'a'): ")
        return ids if res == 'a' else [ids[int(res)-1]]
    
    def upload_file_task(self):
        targets = self.select_targets()
        if not targets: return
        
        files = [f for f in os.listdir('.') if f.endswith(('.bin', '.py', '.json', '.pxld'))]
        if not files: return
        
        for i, f in enumerate(files): print(f"{i+1}. {f} ({os.path.getsize(f)} bytes)")
        try: 
            local_name = files[int(input("📂 Choose file: "))-1]
        except: return
        
        remote_path = input(f"💾 Remote Path [/{local_name}]: ") or f"/{local_name}"
        
        with open(local_name, "rb") as f:
            data = f.read()
        
        sha = hashlib.sha256(data).digest()
        f_id = 100
        chunk_size = 1024
        
        print(f"\n🚀 Uploading to {targets}...")
        self.send_to_targets(targets, 0x2001, {
            "file_id": f_id, "total_size": len(data), 
            "chunk_size": chunk_size, "sha256": sha, "path": remote_path
        })
        
        for off in range(0, len(data), chunk_size):
            chunk = data[off : off + chunk_size]
            for tid in targets:
                if tid not in self.slaves: continue
                # 停等機制
                retry = 0
                while retry < 5:
                    self.slaves[tid]["ack_event"].clear()
                    self.send_to_targets([tid], 0x2002, {"file_id": f_id, "offset": off, "data": chunk})
                    if self.slaves[tid]["ack_event"].wait(timeout=1.0):
                        break
                    retry += 1
                    print(f"⚠️ [{tid}] Retry {retry}/5 for offset {off}")
            
            print(f"  ﹂ 📤 Progress: {min(off+chunk_size, len(data))}/{len(data)} bytes", end='\r')
            
        self.send_to_targets(targets, 0x2003, {"file_id": f_id})
        print("\n✅ Upload Complete.")

    def run(self):
        threading.Thread(target=self.start_ws_server, daemon=True).start()
        while True:
            print(f"\n🚀 NetBus PC Console ({self.local_ip})")
            print("1. Disc | 2. Dash | 3. Query | 4. Upload | 5. Stream | q. Exit")
            c = input("\n👉 Choice: ").lower()
            if c == '1':
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                p_data = SchemaCodec.encode(self.store.get(0x1001), {"server_ip": self.local_ip, "ws_url": f"ws://{self.local_ip}:8000/ws"})
                s.sendto(Proto.pack(0x1001, p_data), ('255.255.255.255', 9000)); s.close()
            elif c == '2':
                for sid, info in self.slaves.items():
                    print(f"{sid} | {info['mem_free']} | {info['uptime_ms']//1000}s")
            elif c == '3':
                ts = self.select_targets()
                if ts: self.send_to_targets(ts, 0x1101, {"query_type": 1})
            elif c == '4':
                self.upload_file_task()
            elif c == '5':
                self.stream_menu()
            elif c == 'q':
                self.running = False; break

if __name__ == "__main__":
    PCTestTool().run()