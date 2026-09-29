import time
import struct
from lib.sys.sys_bus import bus
from lib.sys.buffer_hub import AtomicStreamHub
from lib.sys.proto import Proto

import espnow
import network

MAX_PAYLOAD = 250
BCAST_MAC = b'\xff\xff\xff\xff\xff\xff'


class NowBus:

    def __init__(self, label="NOW-Bus", rx_hub=None):
        self.label = label
        self.connected = False
        self._esp = None
        self._peers = {}
        self._last_peer = None
        self._decode_ctx = {}

        buf_cfg = bus.shared.get('Buffer', {}) or {}
        self.rx_hub = rx_hub
        self._hub_off = 2
        if self.rx_hub is None:
            slots = int(buf_cfg.get("now_rx_slots", 2) or 0)
            if slots > 0:
                slots = min(slots, 4)
                self.rx_hub = AtomicStreamHub(MAX_PAYLOAD + self._hub_off, num_buffers=slots)
        self._drop_on_full = int(buf_cfg.get("drop_on_full", 0) or 0)
        self._drain_reads = int(buf_cfg.get("drain_reads", 1) or 0)
        if self._drain_reads <= 0:
            self._drain_reads = 1

        self.stats = {"rx": 0, "tx": 0, "tx_ok": 0, "tx_fail": 0, "rx_drop": 0}

        # ── 被動學習的射頻 peer 上限 ──────────────────────────────────
        #   ESP-NOW 的 peer 表是**硬體資源**：ESP-IDF 上限 20（加密時 6），
        #   超過 → esp_now_add_peer 回 ESP_ERR_ESPNOW_NO_MEM。
        #   poll() 會對「每個講話的新 MAC」add_peer（因為 MAC 無法枚舉，
        #   錯過那一刻就再也認不出對方）—— 但來源是**任意的射頻輸入**：
        #   附近別人的 ESP-NOW 裝置、隨機雜訊，都會消耗這張表。
        #   表填滿後，真正要綁定的目標反而進不來（而且沒人會發現）。
        #   → 被動學習**設上限**；使用者明確按「綁定」的 add_peer 不受限
        #     （那是意圖，不是雜訊）。上限可調，預設 16（留 4 格給明確綁定）。
        now_cfg = bus.shared.get('Network', {}).get('ESP_now', {}) or {}
        self.max_learn_peers = int(now_cfg.get("max_learn_peers", 16) or 0)
        if self.max_learn_peers <= 0:
            self.max_learn_peers = 16
        self._learn_full_logged = False

    def init(self, channel=None):
        try:
            sta = network.WLAN(network.STA_IF)
            ap = network.WLAN(network.AP_IF)
            if not sta.active() and not ap.active():
                if channel is None:
                    print(f"❌ [{self.label}] No active Wi-Fi interface")
                    return False
                sta.active(True)
                sta.config(channel=channel)
                time.sleep_ms(100)

            self._esp = espnow.ESPNow()
            self._esp.active(True)
            self._esp.add_peer(BCAST_MAC)
            self.connected = True
            print(f"✅ [{self.label}] ESP-NOW active, channel={self._channel()}")
            return True
        except Exception as e:
            print(f"❌ [{self.label}] Init failed: {e}")
            return False

    def _channel(self):
        try:
            sta = network.WLAN(network.STA_IF)
            if sta.active():
                return sta.config('channel')
            ap = network.WLAN(network.AP_IF)
            if ap.active():
                return ap.config('channel')
        except Exception:
            pass
        return '?'

    def deinit(self):
        try:
            if self._esp:
                self._esp.active(False)
                self._esp = None
            self.connected = False
            self._peers.clear()
            self._last_peer = None
            print(f"🔌 [{self.label}] Deinitialized")
        except Exception:
            pass

    def add_peer(self, mac):
        if mac == BCAST_MAC:
            return True
        if mac in self._peers:
            return True
        try:
            self._esp.add_peer(mac)
        except Exception:
            pass
        self._peers[mac] = True
        return True

    def learn_peer(self, mac):
        """**被動**學會一個射頻位址（收幀時呼叫）—— 受 `max_learn_peers` 限制。

        與 `add_peer()` 的差別就是「誰決定的」：
          add_peer   = 使用者的意圖（UI 綁定 / 定向發射前補通道）→ 不設限
          learn_peer = 射頻上剛好有人講話 → 設限，避免雜訊吃光硬體 peer 表

        為什麼「收幀就必須學」：ESP-NOW 的位址是 MAC，**無法枚舉**，
        所以「知道對方位址」的唯一時機就是它的幀到達射頻層的這一刻。
        不在這裡登記，之後 write()（單播回覆）會拿到 ESP_ERR_NOT_FOUND(-12393)。
        """
        if mac in self._peers:
            return True
        if len(self._peers) >= self.max_learn_peers:
            if not self._learn_full_logged:
                self._learn_full_logged = True
                print("⚠️ [{}] 被動 peer 表已滿 ({}), 不再自動登記新來源; "
                      "明確綁定不受限".format(self.label, self.max_learn_peers))
            return False
        return self.add_peer(mac)

    def has_peer(self, mac):
        return mac in self._peers

    @property
    def peers(self):
        return list(self._peers.keys())

    @property
    def peer_count(self):
        return len(self._peers)

    def send(self, mac, data):
        if not self.connected:
            return False
        if len(data) > MAX_PAYLOAD:
            return False
        try:
            ok = self._esp.send(mac, data)
            self.stats["tx"] += 1
            if ok:
                self.stats["tx_ok"] += 1
            else:
                self.stats["tx_fail"] += 1
            return ok
        except Exception:
            self.stats["tx"] += 1
            self.stats["tx_fail"] += 1
            return False

    def broadcast(self, data):
        return self.send(BCAST_MAC, data)

    def write(self, data):
        if self._last_peer is None:
            return False
        return self.send(self._last_peer, data)

    def write_to(self, mac, data):
        if mac is None:
            return self.write(data)
        return self.send(mac, data)

    def poll(self, **extra_ctx):
        if not self.connected:
            return
        if self.rx_hub is None:
            return

        if extra_ctx:
            self._decode_ctx = extra_ctx

        for _ in range(self._drain_reads):
            try:
                peer, msg = self._esp.recv(0)
            except Exception:
                break

            if peer is None or not msg:
                break

            self.stats["rx"] += 1
            n = len(msg)

            # ── 學會「剛剛是誰在講話」─────────────────────────────────
            #   ESP-NOW 的位址是 MAC，而 MAC **無法枚舉** —— 唯一能知道對端
            #   位址的時機，就是它的幀真的到達射頻層的這一刻。
            #   不在這裡登記的話，之後要回它（write() → send(peer)）會拿到
            #   ESP_ERR_NOT_FOUND(-12393)：ESP-NOW 不接受「沒註冊過的 peer」，
            #   即使我們手上已經有它的 MAC。
            #   → 這是「被發現 / 雙向通訊」成立的最小條件，也是 PeerRegistry
            #     被動學習（learn_from_frame）的前提。
            #   用 learn_peer（不是 add_peer）：來源是任意射頻輸入，要設上限，
            #   否則附近的雜訊會把 ESP-NOW 那張 20 格的硬體 peer 表吃光。
            self.learn_peer(peer)

            view = self.rx_hub.get_write_view()
            if view is None:
                self.stats["rx_drop"] += 1
                if not self._drop_on_full:
                    break
                continue

            pv = memoryview(view)[self._hub_off:]
            available = len(pv)

            if n > available:
                n = available
                msg = msg[:n]

            struct.pack_into("<H", view, 0, n)
            pv[:n] = msg
            self.rx_hub.commit()

            self._last_peer = peer
            self._decode_ctx["_peer_mac"] = peer

        return

    def recv(self):
        try:
            peer, msg = self._esp.recv(0)
        except Exception:
            return None, None
        if peer is None:
            return None, None
        self.stats["rx"] += 1
        return peer, bytes(msg)

    def recv_timeout(self, timeout_ms):
        try:
            peer, msg = self._esp.recv(timeout_ms)
        except Exception:
            return None, None
        if peer is None:
            return None, None
        self.stats["rx"] += 1
        return peer, bytes(msg)

    def discover(self, discover_payload, timeout_ms=2000):
        online = {}
        self.broadcast(discover_payload)

        deadline = time.ticks_ms() + timeout_ms
        while time.ticks_ms() < deadline:
            peer, msg = self.recv()
            if peer is not None and msg:
                online[peer] = msg
                self.add_peer(peer)
            time.sleep_ms(10)

        return online

    def send_proto(self, mac, cmd, payload=b"", addr=0xFFFF):
        frame = Proto.pack(cmd, payload, addr)
        if len(frame) > MAX_PAYLOAD:
            return False
        return self.send(mac, frame)

    def broadcast_proto(self, cmd, payload=b"", addr=0xFFFF):
        return self.send_proto(BCAST_MAC, cmd, payload, addr)
