# wtt_rx_probe.py — 用 app 的 NowBus 收 0x1501 WTT_CTL 並解碼（階段 A 測試）
# ---------------------------------------------------------------------
# 目的: 不 boot 完整 app，直接用 app 的同一套元件 (NowBus + rx_hub +
#       StreamParser) 收 master(LG 遙控)廣播的 0x1501，確認「now_bus 收
#       → 解碼 → on_ctl 行為」整條鏈是否正常。
#
# 用法:
#   1) 確定 ESP-NOW 乾淨(soft reboot 後 Ctrl-C 停到 REPL，見 SOP)。
#   2) 執行:  python3 -B -m mpremote connect <PORT> resume run wtt_rx_probe.py
#   3) 看到 === PROBE READY === 後，按 LG 遙控 2~3 次(每次隔 1~2s)。
#   4) Ctrl-C 停止。
#
# 判讀:
#   [P#n] WTT_CTL mode=skip bri=X   → NowBus 收得到 + 解碼正確 = 鏈 OK ✅
#   [P#n] cmd=0x.... (非 0x1501)     → 有收到但 cmd 不同(master 送別的)
#   完全沒輸出                        → NowBus 這層收不到，需對照 espnow_mon.py
# ---------------------------------------------------------------------
import time
import network
from lib.sys.sys_bus import bus
from lib.sys.now_bus import NowBus
from lib.sys.proto import StreamParser

CH = 6
try:
    import json
    CH = int(json.load(open("config.json"))["Network"]["ESP_now"]["channel"])
except Exception:
    pass

now = NowBus(label="PROBE-RX")
if not now.init(channel=CH):
    print("PROBE init FAIL")
    raise SystemExit
try:
    bus.register_service("NowBus", now)
except Exception:
    pass

parser = StreamParser()
def _log(s):
    print(s)
    try:
        with open("/wtt_rx.log", "a") as f:
            f.write(s + "\n")
    except Exception:
        pass

print("=== PROBE READY ===")
print("my mac  :", network.WLAN(network.STA_IF).config("mac").hex())
print("channel :", CH)
print("now poll every 10ms — 請按 LG 遙控 2~3 次 (Ctrl-C 停止)")

n = 0
while True:
    now.poll()                      # 同 NowTask.loop 的做法
    hub = now.rx_hub
    if hub is not None:
        view = hub.get_read_view()
        if view is not None:
            sz = view[0] | (view[1] << 8)
            if sz:
                parser.feed(memoryview(view)[2:2 + sz])
            hub.commit()
            f = parser.pop_frame()
            while f is not None:
                _v, _a, cmd, payload = f
                n += 1
                if cmd == 0x1501:
                    mode = payload[0] if len(payload) > 0 else 0xFF
                    bri = payload[1] if len(payload) > 1 else 0xFF
                    cmd_d = {}
                    if mode != 0xFF:
                        cmd_d["mode"] = mode & 0xFF
                    if bri != 0xFF:
                        cmd_d["brightness"] = max(0, min(bri, 36))
                    if cmd_d:
                        bus.shared["_display_cmd"] = cmd_d
                    _log("[P#%d] WTT_CTL mode=%s bri=%s -> _display_cmd=%s" % (
                        n,
                        "skip" if mode == 0xFF else mode,
                        "skip" if bri == 0xFF else bri,
                        bus.shared["_display_cmd"]))
                else:
                    _log("[P#%d] cmd=0x%04X payload=%s len=%d" % (
                        n, cmd, bytes(payload).hex(), len(payload)))
                f = parser.pop_frame()
    time.sleep_ms(10)
