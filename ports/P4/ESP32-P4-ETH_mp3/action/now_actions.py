import time
import gc
import ubinascii
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus

BCAST_MAC = b'\xff\xff\xff\xff\xff\xff'


def _mac_str_to_bytes(mac_str):
    if isinstance(mac_str, bytes) and len(mac_str) == 6:
        return mac_str
    try:
        s = mac_str.replace(":", "").replace("-", "").replace(" ", "")
        return ubinascii.unhexlify(s)
    except Exception:
        return BCAST_MAC


def on_now_init(ctx, args):
    app = ctx.get("app")
    if not app:
        return

    esp_cfg = bus.shared.get('Network', {}).get('ESP_now', {})
    enable = esp_cfg.get('enable', 0)
    if not enable:
        print("[NOW] ESP_now disabled in config, skip init")
        return

    now = bus.get_service("NowBus")
    if now is None:
        try:
            from lib.sys.now_bus import NowBus
            wifi_cfg = bus.shared.get('Network', {}).get('wifi', {})
            wifi_enable = wifi_cfg.get('enable', 0)
            channel = esp_cfg.get('channel', 1)

            now = NowBus()
            if wifi_enable:
                ok = now.init()
            else:
                ok = now.init(channel=channel)

            if ok:
                bus.register_service("NowBus", now)
                sources = bus.get_service("bus_sources")
                if sources:
                    sources.add(now)
                print("[NOW] ESP-NOW ready, ch={}".format(now._channel()))
            else:
                print("[NOW] init failed")
        except Exception as e:
            print("[NOW] init err: {}".format(e))
    else:
        print("[NOW] already initialized")


def on_now_send_hb(ctx, args):
    app = ctx.get("app")
    if not app:
        return

    now = bus.get_service("NowBus")
    if now is None:
        print("[NOW] not initialized, run NOW_INIT first")
        return

    target_mac = args.get("target_mac", "FF:FF:FF:FF:FF:FF")
    count = max(1, int(args.get("count", 1)))

    hb_def = app.store.get(0x1201)
    if not hb_def:
        print("[NOW] HEARTBEAT schema not found")
        return

    payload_data = {
        "slave_id": bus.slave_id,
        "uptime_ms": time.ticks_ms(),
        "mem_free": gc.mem_free(),
        "ws_connected": 0,
    }

    try:
        payload = SchemaCodec.encode(hb_def, payload_data)
        packet = Proto.pack(0x1201, payload)
    except Exception as e:
        print("[NOW] pack err: {}".format(e))
        return

    mac = _mac_str_to_bytes(target_mac)
    ok = 0
    fail = 0

    for _ in range(count):
        if now.send(mac, packet):
            ok += 1
        else:
            fail += 1

    print("[NOW] send_hb -> {} count={} ok={} fail={}".format(target_mac, count, ok, fail))


def on_now_stats(ctx, args):
    now = bus.get_service("NowBus")
    if now is None:
        print("[NOW] not initialized")
        return

    s = now.stats
    print("[NOW] stats rx={} tx={} ok={} fail={} drop={}".format(
        s["rx"], s["tx"], s["tx_ok"], s["tx_fail"], s["rx_drop"]))


def register(app):
    app.disp.on(0x1301, on_now_init)
    app.disp.on(0x1302, on_now_send_hb)
    app.disp.on(0x1303, on_now_stats)
    print("[NOW] ESP-NOW actions registered")
