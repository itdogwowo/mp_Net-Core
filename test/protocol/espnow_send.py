# espnow_send.py — ESP-NOW 發送測試（最少版，跑在「發送端 / master」板）
# ---------------------------------------------------------------------
# 目的: 每 0.5s 送一筆 PING，並同時收 PONG，驗證單向/雙向鏈路。
#
# 用法:
#   1) 上傳:  python3 -B -m mpremote connect <PORT> cp espnow_send.py :/espnow_send.py
#   2) 執行:  python3 -B -m mpremote connect <PORT> resume run espnow_send.py
#   看到 "SEND READY" 後即開始送。Ctrl-C 停止。
#
# 判讀:
#   [TX#n] sent ... ok        → 送得出去
#   [TX#n] send FAIL NOT_FOUND(-12393) → 目標 peer 沒註冊（檢查 DST / add_peer）
#   [TX#n] send FAIL EXIST(-12395)     → 板子 app 正佔用 ESP-NOW，先 Ctrl-C 停 app
#   [RX] PONG from=...        → 對端有收到並回 PONG = 雙向通 ✅
#   只看到 TX ok 沒 PONG      → 對端沒收到(查 channel/距離) 或對端沒在跑監控
#
# 切換 廣播 / 單播:
#   DST = None            → 廣播（接收端不需填 MASTER_MAC 就收得到）
#   DST = "e4b323f93868"  → 單播（接收端 espnow_mon.py 的 MASTER_MAC 必須填本板 mac）
# ---------------------------------------------------------------------
import network
import espnow
import time

# channel: 優先讀板上 config，讀不到預設 6（須與接收端相同！）
CH = 6
try:
    import json
    CH = int(json.load(open("config.json"))["Network"]["ESP_now"]["channel"])
except Exception:
    pass

# None = 廣播；或填接收端 mac（例 "e4b323f93868"）做單播
DST = None

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
e.add_peer(BCAST)
target = BCAST
if DST:
    import ubinascii
    target = ubinascii.unhexlify(DST)
    e.add_peer(target)

print("=== ESP-NOW SEND READY ===")
print("my mac  :", sta.config("mac").hex())
print("channel :", CH)
print("target  :", DST if DST else "BROADCAST")
print("sending PING every 0.5s... (Ctrl-C to stop)")

i = 0
while True:
    i += 1
    payload = b"PING-%d" % i
    try:
        e.send(target, payload)
        print("[TX#%d] sent %d B to %s ok" % (i, len(payload), target.hex()))
    except Exception as ex:
        print("[TX#%d] send FAIL: %r" % (i, ex))
    # 收 PONG（非阻塞）
    peer, data = e.recv(0)
    if peer is not None and data:
        print("[RX] PONG from=%s: %s" % (peer.hex(), bytes(data).hex()))
    time.sleep_ms(500)
