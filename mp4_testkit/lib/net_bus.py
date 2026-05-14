import socket
import struct
import time
from lib.sys_bus import bus
from lib.buffer_hub import AtomicStreamHub

class NetBus:
    """
    NetBus: 純傳輸層 (TCP/WS/UDP)
    只負責接收/發送與 WS 拆幀，接收端輸出寫入 AtomicStreamHub
    """
    TYPE_TCP = 0
    TYPE_WS  = 1
    TYPE_UDP = 2

    def __init__(self, bus_type=TYPE_WS, label="Bus", rx_hub=None):
        self.type = bus_type
        self.label = label
        self.sock = None
        self.connected = False
        self.target_addr = None # UDP 發送對象
        self._peer = None
        self._decode_ctx = {}
        
        # 統一使用 Buffer.size 作為接收緩衝區大小
        buf_cfg = bus.shared.get('Buffer', {}) or {}
        buf_size = buf_cfg.get('size', 4096)
        self._buf = bytearray(buf_size)
        self.rx_hub = rx_hub
        self._drop_buf = bytearray(min(2048, buf_size))
        self._hub_off = 2
        if self.rx_hub is None:
            rx_buffers = int(buf_cfg.get("rx_hub_buffers", 0) or 0)
            if rx_buffers > 0:
                self.rx_hub = AtomicStreamHub(buf_size + self._hub_off, num_buffers=rx_buffers)
        self._drop_on_full = int(buf_cfg.get("drop_on_full", 0) or 0)
        self._drain_reads = int(buf_cfg.get("drain_reads", 1) or 0)
        if self._drain_reads <= 0:
            self._drain_reads = 1
        self._ws_need = 0
        self._ws_masked = 0
        self._ws_mask = bytearray(4)
        self._ws_mask_i = 0
        self._ws_hdr = bytearray(14)
        self._ws_hdr_len = 0
        self._send_retry = int(buf_cfg.get("send_retry", 64) or 0)
        if self._send_retry <= 0:
            self._send_retry = 64

    def connect(self, host, port, path="/ws"):
        """初始化連接 (TCP/WS) 或 綁定 (UDP)"""
        try:
            if self.type == self.TYPE_UDP:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.bind(('0.0.0.0', port))
                self.connected = True
            else:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5)
                self.sock.connect((host, port))
                
                if self.type == self.TYPE_WS:
                    # WebSocket 握手邏輯
                    handshake = (
                        f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n"
                    )
                    self._send_all(handshake.encode())
                    if b"101 Switching Protocols" not in self.sock.recv(1024):
                        raise Exception("WS Handshake Failed")
                
                self.connected = True
            
            self.sock.settimeout(0)  # 統一進入非阻塞模式
            if self.type == self.TYPE_UDP:
                self._peer = ("0.0.0.0", port, "")
            else:
                self._peer = (host, port, path)
            print(f"✅ [{self.label}] Initialized")
            return True
        except Exception as e:
            print(f"❌ [{self.label}] Init Failed: {e}")
            return False
        
    def disconnect(self):
        """全面清除現有的網路連接資源"""
        if not self.sock:
            self.connected = False
            return
            
        try:
            # 針對不同類型的協議做優雅收尾
            if self.type == self.TYPE_WS and self.connected:
                # 嘗試發送 WS 關閉幀 (Opcode 0x8)
                try: self._send_all(b'\x88\x00')
                except: pass
            
            # 關閉 Socket (TCP/UDP/WS 均適用)
            self.sock.close()
        except OSError:
            pass
        finally:
            self.sock = None
            self.connected = False
            self.target_addr = None
            self._peer = None
            print(f"🔌 [{self.label}] Connection Closed.")

    def poll(self, **extra_ctx):
        """
        核心輪詢：
        1. 從網路吸取數據
        2. WS: 拆幀/unmask
        3. 將接收數據寫入 rx_hub (每槽位前 2 bytes 為長度)
        """
        if not self.connected or not self.sock: return
        
        try:
            if extra_ctx:
                self._decode_ctx = extra_ctx
            else:
                self._decode_ctx = {}
            if self.rx_hub is None:
                return
            buf_cfg = bus.shared.get('Buffer', {}) or {}
            dr = int(buf_cfg.get("drain_reads", self._drain_reads) or 0)
            if dr <= 0:
                dr = 1
            self._drain_reads = dr

            recv_size = len(self._buf)
            if self.type == self.TYPE_WS:
                for _ in range(dr):
                    try:
                        n = 0
                        if hasattr(self.sock, "recv_into"):
                            n = self.sock.recv_into(self._buf)
                        elif hasattr(self.sock, "readinto"):
                            n = self.sock.readinto(self._buf)
                        else:
                            raw_bytes = self.sock.recv(recv_size)
                            n = len(raw_bytes)
                            if n:
                                self._buf[:n] = raw_bytes
                    except OSError:
                        break
                    if n is None or n <= 0:
                        if n == 0:
                            self.connected = False
                        break

                    raw = memoryview(self._buf)[:n]

                    mv = raw
                    ln_mv = len(mv)
                    i = 0
                    while i < ln_mv:
                        if self._ws_need <= 0:
                            need_hdr = 2
                            if self._ws_hdr_len and self._ws_hdr_len < need_hdr:
                                take = need_hdr - self._ws_hdr_len
                                if take > (ln_mv - i):
                                    take = ln_mv - i
                                if take > 0:
                                    self._ws_hdr[self._ws_hdr_len:self._ws_hdr_len + take] = mv[i:i + take]
                                    self._ws_hdr_len += take
                                    i += take
                                if self._ws_hdr_len < need_hdr:
                                    break

                            if self._ws_hdr_len >= 2:
                                b0 = self._ws_hdr[0]
                                b1 = self._ws_hdr[1]
                                hdr_src = self._ws_hdr
                                hdr_off = 2
                            else:
                                if (ln_mv - i) < 2:
                                    break
                                b0 = int(mv[i])
                                b1 = int(mv[i + 1])
                                hdr_src = mv
                                hdr_off = i + 2
                                i += 2

                            plen7 = b1 & 0x7F
                            masked = 1 if (b1 & 0x80) else 0
                            ext_len = 0
                            if plen7 == 126:
                                ext_len = 2
                            elif plen7 == 127:
                                ext_len = 8
                            need = 2 + ext_len + (4 if masked else 0)

                            if hdr_src is mv:
                                if (ln_mv - (hdr_off)) < (need - 2):
                                    self._ws_hdr[0] = b0
                                    self._ws_hdr[1] = b1
                                    take = ln_mv - i
                                    if take > (need - 2):
                                        take = need - 2
                                    if take > 0:
                                        self._ws_hdr[2:2 + take] = mv[i:i + take]
                                        self._ws_hdr_len = 2 + take
                                        i += take
                                    break
                                if ext_len == 0:
                                    pay_len = plen7
                                elif ext_len == 2:
                                    pay_len = (int(mv[hdr_off]) << 8) | int(mv[hdr_off + 1])
                                else:
                                    pay_len = 0
                                    for k in range(8):
                                        pay_len = (pay_len << 8) | int(mv[hdr_off + k])
                                if masked:
                                    moff = hdr_off + ext_len
                                    self._ws_mask[0] = int(mv[moff])
                                    self._ws_mask[1] = int(mv[moff + 1])
                                    self._ws_mask[2] = int(mv[moff + 2])
                                    self._ws_mask[3] = int(mv[moff + 3])
                                i = hdr_off + ext_len + (4 if masked else 0)
                            else:
                                if self._ws_hdr_len < need:
                                    take = need - self._ws_hdr_len
                                    if take > (ln_mv - i):
                                        take = ln_mv - i
                                    if take > 0:
                                        self._ws_hdr[self._ws_hdr_len:self._ws_hdr_len + take] = mv[i:i + take]
                                        self._ws_hdr_len += take
                                        i += take
                                    if self._ws_hdr_len < need:
                                        break
                                if ext_len == 0:
                                    pay_len = plen7
                                elif ext_len == 2:
                                    pay_len = (int(self._ws_hdr[2]) << 8) | int(self._ws_hdr[3])
                                else:
                                    pay_len = 0
                                    for k in range(8):
                                        pay_len = (pay_len << 8) | int(self._ws_hdr[2 + k])
                                if masked:
                                    moff = 2 + ext_len
                                    self._ws_mask[0] = int(self._ws_hdr[moff])
                                    self._ws_mask[1] = int(self._ws_hdr[moff + 1])
                                    self._ws_mask[2] = int(self._ws_hdr[moff + 2])
                                    self._ws_mask[3] = int(self._ws_hdr[moff + 3])
                                self._ws_hdr_len = 0

                            self._ws_need = pay_len
                            self._ws_masked = masked
                            self._ws_mask_i = 0
                            if self._ws_need <= 0:
                                continue

                        take = self._ws_need
                        avail = ln_mv - i
                        if take > avail:
                            take = avail
                        if take <= 0:
                            break
                        chunk = mv[i:i + take]
                        if self._ws_masked:
                            mi = self._ws_mask_i
                            m0 = self._ws_mask[0]
                            m1 = self._ws_mask[1]
                            m2 = self._ws_mask[2]
                            m3 = self._ws_mask[3]
                            for j in range(take):
                                b = int(chunk[j])
                                if mi == 0:
                                    chunk[j] = b ^ m0
                                elif mi == 1:
                                    chunk[j] = b ^ m1
                                elif mi == 2:
                                    chunk[j] = b ^ m2
                                else:
                                    chunk[j] = b ^ m3
                                mi = (mi + 1) & 3
                            self._ws_mask_i = mi

                        view = self.rx_hub.get_write_view()
                        if view is None:
                            return
                        pv = memoryview(view)[self._hub_off:]
                        if take > len(pv):
                            take = len(pv)
                            chunk = mv[i:i + take]
                        struct.pack_into("<H", view, 0, take)
                        pv[:take] = chunk
                        self.rx_hub.commit()

                        i += take
                        self._ws_need -= take
                return

            for _ in range(dr):
                view = self.rx_hub.get_write_view()
                if view is None:
                    if not self._drop_on_full or self.type != self.TYPE_UDP:
                        break
                    try:
                        if self.type == self.TYPE_UDP:
                            if hasattr(self.sock, "recvfrom_into"):
                                self.sock.recvfrom_into(self._drop_buf)
                            else:
                                self.sock.recvfrom(len(self._drop_buf))
                        else:
                            if hasattr(self.sock, "recv_into"):
                                self.sock.recv_into(self._drop_buf)
                            elif hasattr(self.sock, "readinto"):
                                self.sock.readinto(self._drop_buf)
                            else:
                                self.sock.recv(len(self._drop_buf))
                    except OSError:
                        break
                    continue

                pv = memoryview(view)[self._hub_off:]
                try:
                    if self.type == self.TYPE_UDP:
                        if hasattr(self.sock, "recvfrom_into"):
                            n, addr = self.sock.recvfrom_into(pv)
                            self.target_addr = addr
                        else:
                            raw_bytes, addr = self.sock.recvfrom(len(pv))
                            self.target_addr = addr
                            n = len(raw_bytes)
                            if n:
                                pv[:n] = raw_bytes
                    else:
                        if hasattr(self.sock, "recv_into"):
                            n = self.sock.recv_into(pv)
                        elif hasattr(self.sock, "readinto"):
                            n = self.sock.readinto(pv)
                        else:
                            raw_bytes = self.sock.recv(len(pv))
                            n = len(raw_bytes)
                            if n:
                                pv[:n] = raw_bytes
                except OSError:
                    break

                if n is None or n <= 0:
                    if n == 0:
                        self.connected = False
                    break

                struct.pack_into("<H", view, 0, n)
                self.rx_hub.commit()
            return

        except OSError:
            return

    def write(self, data: bytes):
        """大一統寫入"""
        if not self.connected:
            return False
        try:
            if self.type == self.TYPE_UDP:
                if self.target_addr:
                    self.sock.sendto(data, self.target_addr)
                return True
            elif self.type == self.TYPE_WS:
                hdr = bytearray([0x82])
                l = len(data)
                if l < 126: hdr.append(l)
                else: hdr.append(126); hdr.extend(struct.pack(">H", l))
                return self._send_all(hdr) and self._send_all(data)
            else:
                return self._send_all(data)
        except Exception:
            self.connected = False
            return False

    def _send_all(self, data):
        mv = memoryview(data)
        ln = len(mv)
        off = 0
        retry = 0
        while off < ln:
            try:
                n = self.sock.send(mv[off:])
                if n is None:
                    n = 0
                if n > 0:
                    off += n
                    retry = 0
                    continue
            except OSError as e:
                code = e.args[0] if e.args else None
                if code not in (11, 35):
                    self.connected = False
                    return False
            retry += 1
            if retry >= self._send_retry:
                self.connected = False
                return False
            try:
                time.sleep_ms(0)
            except Exception:
                try:
                    time.sleep(0)
                except Exception:
                    pass
        return True
