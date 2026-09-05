# espnow_mon.py — ESP-NOW 接收監控（最少版，跑在「接收端 / slave」板）
# ---------------------------------------------------------------------
# 目的: 驗證 ESP-NOW 封包到底收不收得到 + 自動回 PONG 確認雙向鏈路。
#
# 用法:
#   1) 若板子 app 正在跑(ESP-NOW 已被佔用)，先 Ctrl-C 停到 REPL。
#   2) 上傳:  python3 -B -m mpremote connect <PORT> cp espnow_mon.py :/espnow_mon.py
#   3) 執行:  python3 -B -m mpremote connect <PORT> resume run espnow_mon.py
#   看到 "MON READY" 後，在 master 板跑 espnow_send.py。
#   Ctrl-C 停止。
#
# 判讀:
#   [RX#n] from=xxxx...      → 收到封包 (from 是發送端實際 mac)
#   PONG sent                 → 已自動回廣播 PONG
#   一直沒 RX                 → 沒封包到達：查 channel / 距離 / master send 結果
# ---------------------------------------------------------------------
import network
import espnow
import time

# channel: 優先讀板上 config，讀不到預設 6（須與 master 相同！）
CH = 6
try:
    import json
    CH = int(json.load(open("config.json"))["Network"]["ESP_now"]["channel"])
except Exception:
    pass

# 若 master 用「單播」送，必須把 master 的 mac 填進來（例 "aabbccddeeff"）。
# 留 None = 只收廣播（ESP-NOW 單播需要接收方先把 sender 加入 peer，否則丟棄）。
MASTER_MAC = None

BCAST = b"\xff\xff\xff\xff\xff\xff"

sta = network.WLAN(network.STA_IF)
if not sta.active():
    sta.active(True)
try:
    sta.config(channel=CH)
except Exception:
    pass

e = espnow.ESPNow()
e.active(True)
e.add_peer(BCAST)                    # 廣播也必須註冊 peer
if MASTER_MAC:
    try:
        import ubinascii
        e.add_peer(ubinascii.unhexlify(MASTER_MAC))
    except Exception as ex:
        print("add_peer(master) fail:", repr(ex))

print("=== ESP-NOW MON READY ===")
print("my mac  :", sta.config("mac").hex())
print("channel :", CH)
print("rx mode :", "broadcast + unicast(%s)" % MASTER_MAC if MASTER_MAC else "broadcast only")
print("waiting RX... (Ctrl-C to stop)")

n = 0
while True:
    peer, data = e.recv(0)           # 非阻塞
    if peer is not None and data:
        n += 1
        print("[RX#%d] from=%s len=%d hex=%s" % (n, peer.hex(), len(data), bytes(data).hex()))
        # 自動回 PONG（廣播），讓 master 端確認雙向
        try:
            e.send(BCAST, b"PONG:" + bytes(data))
            print("        -> PONG sent")
        except Exception as ex:
            print("        -> PONG send FAIL:", repr(ex))
    else:
        time.sleep_ms(10)
