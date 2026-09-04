import socket
import struct
import time
from lib.sys.sys_bus import bus
from lib.sys.buffer_hub import AtomicStreamHub
from lib.sys.proto import RX_BUF_SIZE, SEND_CAP

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
        self._last_rx = 0       # 上次收到資料的 ticks_ms (0 = 從未)，供半開連線偵測
        
        buf_cfg = bus.shared.get('Buffer', {}) or {}
        buf_size = RX_BUF_SIZE
        self._buf = bytearray(buf_size)
        self.rx_hub = rx_hub
        self._drop_buf = bytearray(min(2048, buf_size))
        self._hub_off = 2
        if self.rx_hub is None:
            slots = int(buf_cfg.get("net_rx_slots", 2) or 0)
            if slots > 0:
                slots = min(slots, 4)
                self.rx_hub = AtomicStreamHub(buf_size + self._hub_off, num_buffers=slots)
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
        self.cache_hub = None  # 消費端緩存(rx_hub 鏡像),首次 read_into() 時惰性建立一次,永久重用

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
            self._last_rx = time.ticks_ms()   # 🔧 連上 = 當下有流量, 供半開連線偵測基準
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
            self._last_rx = 0   # 🔧 斷線後清掉 liveness 基準, 下次連上重算
            print(f"🔌 [{self.label}] Connection Closed.")

    def idle_ms(self):
        """距離上次收到資料的毫秒數（0 = 從未收到 / 剛連上）。

        供 on_connect_request 的防抖動 + 健康檢查用：對面靜默消失（沒有 FIN/RST）
        時 connected 仍會卡在 True，靠這個時間戳判斷連線是否已死。
        """
        if not self._last_rx:
            return 0
        return time.ticks_diff(time.ticks_ms(), self._last_rx)

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
                    except OSError as e:
                        # 🔧 非阻塞 socket 在無資料時拋 EAGAIN(11)/EWOULDBLOCK(35) = 正常;
                        #    其他錯誤 (RST/對端重啟等) = 真斷線 → 通知 + 關閉 socket,
                        #    讓 master 也看到斷線 (避免 half-open 靜默掉包導致下載卡住)
                        code = e.args[0] if e.args else None
                        if code not in (11, 35):
                            if self.connected:
                                print("⚠️ [{}] 連線中斷: {}".format(self.label, e))
                            self.connected = False
                            try:
                                self.sock.close()
                            except Exception:
                                pass
                        break
                    if n is None or n <= 0:
                        if n == 0:
                            # 🔧 對端正常關閉 (FIN) → 通知 + 關閉
                            if self.connected:
                                print("⚠️ [{}] 對端關閉連線 (FIN)".format(self.label))
                            self.connected = False
                            try:
                                self.sock.close()
                            except Exception:
                                pass
                        break

                    self._last_rx = time.ticks_ms()   # 🔧 收到資料 → 刷新 liveness 時間戳
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
                        pv[:take] = chunk
                        self._commit(view, take)

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

                self._last_rx = time.ticks_ms()   # 🔧 收到資料 → 刷新 liveness 時間戳
                self._commit(view, n)
            return

        except OSError:
            return

    def _commit(self, view, n):
        """提交一筆資料到 rx_hub(解碼器讀);若 cache_hub 已建立則同時鏡像複製一份
        (消費端 read_into 讀)。cache_hub 為 None 時只做原本 commit,零成本。"""
        struct.pack_into("<H", view, 0, n)
        self.rx_hub.commit()
        cache = self.cache_hub
        if cache is not None:
            cview = cache.get_write_view()
            if cview is not None:
                take = 2 + n  # 含 2-byte 長度標頭(mv[a:b]=mv[c:d] 兩邊長度必須相同)
                cview[:take] = view[:take]
                cache.commit()

    def read_into(self, target):
        """消費端直接讀取:複製本 bus 緩存(cache_hub)的下一筆原始資料到 target。
        回傳實際長度(不含 2-byte 長度標頭);無資料回 0。
        cache_hub 首次呼叫時建立一次,之後永久重用(存在 self.cache_hub)。
        cache_hub 與 rx_hub 各自 SPSC:解碼器讀 rx_hub,本方法讀 cache_hub,
        互不影響;不碰底層 io,不影響 poll()。"""
        cache = self.cache_hub
        if cache is None:
            buf_cfg = bus.shared.get('Buffer', {}) or {}
            size = RX_BUF_SIZE + self._hub_off
            slots = int(buf_cfg.get("net_rx_slots", 2) or 0)
            slots = min(slots, 4)
            cache = AtomicStreamHub(size, num_buffers=slots)
            self.cache_hub = cache
        view = cache.get_read_view()
        if view is None:
            return 0
        ln = view[0] | (view[1] << 8)
        n = 0
        if ln > 0:
            src = memoryview(view)[2:2 + ln]
            n = min(ln, len(target))
            target[:n] = src[:n]
        cache.release_read()
        return n

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
            seg = mv[off:off + SEND_CAP]   # 每次 send 壓在 4KB 內 (lwIP 硬約束)
            try:
                n = self.sock.send(seg)
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
                    try:
                        self.sock.close()  # 🔧 讓對端 (master) 也看到斷線
                    except Exception:
                        pass
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
