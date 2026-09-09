# -*- coding: utf-8 -*-
"""net_test_upload.py — 純網絡腳本：以 master 身分敲門並上傳/覆寫/驗證單一檔案。

用途：跳過 NetBusMaster GUI，直接對一台 slave（預設 10.161.185.73）做
  DISCOVER 敲門 → 等連線 → 上傳 fs_manager.py → 驗 sha → CONFIRM →
  下載 manifest 驗證 → (可選) 軟重啟後再驗 manifest 是否存活(測 os.sync 修正)。

用法：
    python -B tools/PC/net_test_upload.py [device_ip] [local_file] [remote_path]
預設：
    device_ip  = 10.161.185.73
    local_file = slave/lib/sys/fs_manager.py
    remote_path= /lib/sys/fs_manager.py
"""
import os
import sys
import time
import struct
import socket
import threading
import hashlib
import queue

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from slave.lib.sys.proto import Proto, StreamParser
from slave.lib.sys.schema_loader import SchemaStore
from slave.lib.sys.schema_codec import SchemaCodec

WS_PORT = 8000
UDP_PORT = 9000


class WSFrameAssembler:
    """WebSocket 二元幀重組 (與 NetBusMaster 一致)。"""

    def __init__(self):
        self._hdr = bytearray(14)
        self._hdr_len = 0
        self._need = 0
        self._pay = bytearray()

    def feed(self, data):
        mv = memoryview(data)
        n = len(mv)
        i = 0
        while i < n:
            if self._need <= 0:
                if self._hdr_len < 2:
                    take = min(2 - self._hdr_len, n - i)
                    self._hdr[self._hdr_len:self._hdr_len + take] = mv[i:i + take]
                    self._hdr_len += take
                    i += take
                    if self._hdr_len < 2:
                        return
                b0 = self._hdr[0]
                b1 = self._hdr[1]
                if b0 != 0x82:
                    self._hdr[0] = self._hdr[1]
                    self._hdr_len = 1
                    continue
                plen7 = b1 & 0x7F
                ext_len = 2 if plen7 == 126 else (8 if plen7 == 127 else 0)
                need_hdr = 2 + ext_len
                if self._hdr_len < need_hdr:
                    take = min(need_hdr - self._hdr_len, n - i)
                    self._hdr[self._hdr_len:self._hdr_len + take] = mv[i:i + take]
                    self._hdr_len += take
                    i += take
                    if self._hdr_len < need_hdr:
                        return
                if plen7 == 126:
                    pay_len = (self._hdr[2] << 8) | self._hdr[3]
                elif plen7 == 127:
                    pay_len = 0
                    for k in range(8):
                        pay_len = (pay_len << 8) | self._hdr[4 + k]
                else:
                    pay_len = plen7
                self._hdr_len = 0
                self._need = pay_len + (4 if (b1 & 0x80) else 0)
                self._pay = bytearray()
            take = self._need
            avail = n - i
            if take > avail:
                take = avail
            if take <= 0:
                break
            self._pay.extend(mv[i:i + take])
            i += take
            self._need -= take
            if self._need <= 0:
                yield bytes(self._pay)
                self._pay = bytearray()


def get_local_ip():
    """找出 10.161.x 的介面 IP，找不到就回 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.161.185.73", 9000))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


class NetMaster:
    def __init__(self, ws_port=WS_PORT):
        self.store = SchemaStore(dir_path=os.path.join(PROJECT_ROOT, "slave", "schema"))
        self.store.finalize()
        self.ws_port = ws_port
        self.local_ip = get_local_ip()
        self.conn = None
        self.slave_id = None
        self.parser = StreamParser()
        self.ws_asm = WSFrameAssembler()
        self.resp_queue = queue.Queue()
        self.connected_event = threading.Event()
        self._srv = None
        self._lock = threading.Lock()

    # ---------- WS server ----------
    def _handle(self, conn, addr):
        try:
            conn.settimeout(5.0)
            header = b""
            try:
                while b"\r\n\r\n" not in header and len(header) < 8192:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    header += chunk
            except socket.timeout:
                pass
            conn.settimeout(None)
            if not header or b"Upgrade: websocket" not in header:
                conn.close()
                return
            header_text = header.decode(errors="ignore")
            first_line = header_text.split("\r\n")[0]
            parts = first_line.split(" ")
            cid = "unknown"
            if len(parts) >= 2:
                path = parts[1].strip("/")
                if path and path != "ws":
                    cid = path.split("/")[-1]
            resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
            conn.send(resp.encode())
            with self._lock:
                self.conn = conn
                self.slave_id = cid
            print(f"👋 已連線: {cid} ({addr[0]})")
            self.connected_event.set()

            # 收資料迴圈
            while True:
                raw = conn.recv(4096)
                if not raw:
                    break
                for frame in self.ws_asm.feed(raw):
                    self.parser.feed(frame)
                    while True:
                        r = self.parser.pop_frame()
                        if r is None:
                            break
                        ver, pkt_addr, cmd, payload_mv = r
                        pb = bytes(payload_mv)
                        c_def = self.store.get(cmd)
                        try:
                            args = SchemaCodec.decode(c_def, pb, store=self.store) if c_def else {}
                        except Exception as e:
                            args = {"_raw": pb}
                        self.resp_queue.put((cmd, args))
        except Exception as e:
            print(f"[recv] 連線結束: {e}")
        finally:
            with self._lock:
                if self.conn is conn:
                    self.conn = None
            try:
                conn.close()
            except Exception:
                pass

    def start_server(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.ws_port))
        s.listen(10)
        self._srv = s
        print(f"🖥  WS server 監聽 0.0.0.0:{self.ws_port} (local_ip={self.local_ip})")
        t = threading.Thread(target=self._accept_loop, args=(s,), daemon=True)
        t.start()

    def _accept_loop(self, s):
        while True:
            try:
                conn, addr = s.accept()
                print(f"  [accept] TCP 連線進入: {addr[0]}:{addr[1]}")
                threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
            except Exception as e:
                print(f"  [accept] 結束: {e}")
                break

    # ---------- knock / discover ----------
    def knock(self, device_ip, udp_port=UDP_PORT):
        c_def = self.store.get(0x1001)
        payload = SchemaCodec.encode(c_def, {
            "server_ip": self.local_ip,
            "ws_url": f"ws://{self.local_ip}:{self.ws_port}",
        })
        frame = bytes(Proto.pack(0x1001, payload))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in range(3):
                s.sendto(frame, (device_ip, udp_port))
                time.sleep(0.3)
            print(f"📡 DISCOVER 已發送 → {device_ip}:{udp_port} (ws_url=ws://{self.local_ip}:{self.ws_port})")
        finally:
            s.close()

    # ---------- 發送 ----------
    def send_cmd(self, cmd, args):
        if self.conn is None:
            raise RuntimeError("尚未連線")
        c_def = self.store.get(cmd)
        payload = SchemaCodec.encode(c_def, args)
        nc4 = bytes(Proto.pack(cmd, payload))
        l = len(nc4)
        hdr = bytearray([0x82])
        if l <= 125:
            hdr.append(l)
        elif l <= 65535:
            hdr.append(126)
            hdr.extend(struct.pack(">H", l))
        else:
            hdr.append(127)
            hdr.extend(struct.pack(">Q", l))
        with self._lock:
            self.conn.sendall(bytes(hdr) + nc4)

    def wait_for(self, cmd, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            remain = deadline - time.time()
            try:
                got, args = self.resp_queue.get(timeout=remain)
            except queue.Empty:
                return None
            if got == cmd:
                return args
            # 非目標回應: 存回前端? 為求簡單直接丟棄並印出
            print(f"  (忽略回應 0x{got:04X})")
        return None

    # ---------- FILE 操作 ----------
    def query(self, remote_path, timeout=8.0):
        self.send_cmd(0x2005, {"path": remote_path})
        return self.wait_for(0x2006, timeout=timeout)

    def confirm(self, remote_path, timeout=8.0):
        self.send_cmd(0x2008, {"path": remote_path})
        return self.wait_for(0x2006, timeout=timeout)

    def upload(self, local_path, remote_path, chunk_size=4096):
        data = open(local_path, "rb").read()
        total = len(data)
        sha = hashlib.sha256(data).digest()

        # BEGIN
        self.send_cmd(0x2001, {
            "file_id": 1,
            "total_size": total,
            "chunk_size": chunk_size,
            "sha256": sha,
            "path": remote_path,
        })
        # 握手 QUERY (與 NetBusMaster._upload_bytes 一致)
        self.send_cmd(0x2005, {"path": remote_path})
        rsp = self.wait_for(0x2006, timeout=8.0)
        if rsp is None:
            raise RuntimeError("FILE_BEGIN handshake timeout")

        # CHUNK loop
        off = 0
        while off < total:
            chunk = data[off:off + chunk_size]
            self.send_cmd(0x2002, {"file_id": 1, "offset": off, "data": chunk})
            ack = self.wait_for(0x2004, timeout=8.0)
            if ack is None:
                raise RuntimeError(f"ACK timeout @ {off}")
            off += len(chunk)

        # END
        self.send_cmd(0x2003, {"file_id": 1})
        rsp = self.wait_for(0x2006, timeout=30.0)
        if rsp is None:
            raise RuntimeError("FILE_END timeout")
        remote_sha = rsp.get("sha256")
        if remote_sha != sha:
            raise RuntimeError(f"SHA mismatch: {remote_sha.hex() if remote_sha else None} != {sha.hex()}")
        print(f"  ✅ 上傳完成: {remote_path} ({total} bytes) sha={sha.hex()[:16]}... pending={rsp.get('pending')}")
        return sha, rsp.get("pending", 0)

    def download_manifest(self):
        """下載 /manifest.json → {path: sha_hex}。"""
        q = self.query("/manifest.json")
        if q is None or q.get("exists") != 1:
            raise RuntimeError("manifest 查詢失敗")
        size = q.get("size", 0)
        if size <= 0:
            return {}
        buf = bytearray()
        off = 0
        chunk = 2048
        while off < size:
            req = min(chunk, size - off)
            self.send_cmd(0x2007, {"path": "/manifest.json", "offset": off, "length": req})
            # FILE_READ 回 0x2002
            rsp = self.wait_for(0x2002, timeout=8.0)
            if rsp is None:
                raise RuntimeError(f"manifest read timeout @ {off}")
            d = bytes(rsp.get("data") or b"")
            if not d:
                break
            buf.extend(d)
            off += len(d)
        import json
        obj = json.loads(bytes(buf).decode("utf-8"))
        result = {}
        for p, info in obj.items():
            if isinstance(info, dict) and "h" in info:
                result[p] = info["h"]
        return result


def main():
    device_ip = sys.argv[1] if len(sys.argv) > 1 else "10.161.185.73"
    local_file = sys.argv[2] if len(sys.argv) > 2 else os.path.join(PROJECT_ROOT, "slave", "lib", "sys", "fs_manager.py")
    remote_path = sys.argv[3] if len(sys.argv) > 3 else "/lib/sys/fs_manager.py"
    do_reboot = "--reboot" in sys.argv
    # --port=N 覆寫 WS 監聽 port (預設 8000)
    ws_port = WS_PORT
    for a in sys.argv:
        if a.startswith("--port="):
            try:
                ws_port = int(a.split("=", 1)[1])
            except Exception:
                pass

    if not os.path.isfile(local_file):
        print(f"❌ 找不到本地檔案: {local_file}")
        return 1

    m = NetMaster(ws_port=ws_port)
    m.start_server()
    m.knock(device_ip)

    wait_sec = 180
    print(f"⏳ 等待設備連線 (最多 {wait_sec}s, 每 4s 重新敲門; Ctrl+C 中斷)...")
    deadline = time.time() + wait_sec
    while time.time() < deadline and not m.connected_event.is_set():
        if m.connected_event.wait(timeout=4.0):
            break
        try:
            m.knock(device_ip)
        except Exception as e:
            print(f"  knock err: {e}")
    if not m.connected_event.is_set():
        print("❌ 設備未連線。可能: IP 不同網段/防火牆/設備已在別台 master 上/韌體 schema 不符。")
        return 1
    print(f"✅ 設備 {m.slave_id} 已連線")

    # 1. 上傳前查詢
    before = m.query(remote_path)
    if before:
        print(f"  [上傳前] exists={before.get('exists')} size={before.get('size')} pending={before.get('pending')}")

    # 2. 上傳
    local_sha = hashlib.sha256(open(local_file, "rb").read()).digest()
    sha, pending = m.upload(local_file, remote_path)
    print(f"  [上傳後] pending={pending} (1=已備份待確認)")

    # 3. CONFIRM
    rsp = m.confirm(remote_path)
    if rsp is None:
        print("  ❌ CONFIRM 無回應")
        return 1
    pend = rsp.get("pending", -1)
    print(f"  [CONFIRM] pending={pend} {'✅ 已清' if pend == 0 else '⚠️ 未清'}")

    # 4. 下載 manifest 驗證
    man = m.download_manifest()
    man_sha = man.get(remote_path)
    if man_sha == local_sha.hex():
        print(f"  [manifest] ✅ {remote_path} 哈希一致: {man_sha[:16]}...")
    else:
        print(f"  [manifest] ❌ 不一致: manifest={man_sha} local={local_sha.hex()}")

    # 5. 選用: 軟重啟後再驗 manifest 是否存活 (測 os.sync 修正)
    if do_reboot:
        print("\n🔁 軟重啟 (0x100F)...")
        m.send_cmd(0x100F, {"delay_ms": 500})
        time.sleep(3)
        m.connected_event.clear()
        m.knock(device_ip)
        print("⏳ 等重啟後回連 (最多 25s)...")
        if not m.connected_event.wait(timeout=25.0):
            print("⚠️ 重啟後未回連 (可能需手動重啟設備)")
            return 1
        print(f"✅ 重啟後已回連: {m.slave_id}")
        man2 = m.download_manifest()
        man_sha2 = man2.get(remote_path)
        if man_sha2 == local_sha.hex():
            print(f"  [reboot-manifest] ✅ 哈希存活一致: {man_sha2[:16]}... (sync 修正生效)")
        else:
            print(f"  [reboot-manifest] ❌ manifest 不一致: {man_sha2} (sync 修正未生效?)")

    print("\n✅ 測試完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
