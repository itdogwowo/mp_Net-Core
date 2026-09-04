from lib.sys.sys_bus import bus
from lib.sys.hw_manager import HW
from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys import bus_speed


def _reply(ctx, rsp_cmd, fields):
    app = ctx["app"]
    cmd_def = app.store.get(rsp_cmd)
    if not cmd_def or "send" not in ctx:
        return
    try:
        payload = SchemaCodec.encode(cmd_def, fields)
        ctx["send"](Proto.pack(rsp_cmd, payload, addr=bus.master_cid))
    except Exception as e:
        print("❌ [HW] reply {} failed: {}".format(hex(rsp_cmd), e))


def on_hw_ctl(ctx, args):
    hw_type = int(args.get("type", 0) or 0)
    hw_id   = int(args.get("id", 0) or 0)
    value   = int(args.get("value", 0) or 0)
    label   = args.get("label") or ""

    if hw_type == HW.VBTN:
        HW.set(HW.VBTN, hw_id, value)
        if hw_id == 1:
            bus.shared["_vbtn1_event"] = value
        print("[HW] vbtn {}={}".format(hw_id, value))
        return

    if hw_type == HW.PIN:
        try:
            p = HW.resolve_pin(hw_id)
            p.value(value)
            print("[HW] pin {}={}".format(hw_id, value))
        except Exception as e:
            print("[HW] pin err: {}".format(e))
        return

    if hw_type == HW.PWM:
        try:
            HW.set(HW.PWM, hw_id, value)
            print("[HW] pwm {} duty={}".format(hw_id, value))
        except Exception as e:
            print("[HW] pwm err: {}".format(e))
        return

    if label == "enc_delta":
        # HW_CTL schema 的 value 是 u16，這裡轉回 signed delta。
        if value & 0x8000:
            value -= 0x10000
        cur = int(bus.shared.get("_enc_delta", 0) or 0)
        bus.shared["_enc_delta"] = cur + value
        print("[HW] enc_delta={:+d}".format(value))
        return

    if label:
        print("[HW] {}={}".format(label, value))
        bus.shared["hw_events"] = {"label": label, "value": value}
        return

    print("[HW] unknown type=0x{:02X}".format(hw_type))


def on_hw_query(ctx, args):
    hw_type = int(args.get("type", 0) or 0)
    hw_id   = int(args.get("id", 0) or 0)

    if hw_type == HW.PIN:
        try:
            p = HW.resolve_pin(hw_id)
            print("[HW] pin {} = {}".format(hw_id, p.value()))
        except Exception as e:
            print("[HW] pin query err: {}".format(e))

    elif hw_type == HW.PWM:
        val = HW.get(HW.PWM, hw_id)
        print("[HW] pwm {} duty={}".format(hw_id, val))


# ── 臨時提速 (bus_speed) ──

def on_speed_set(ctx, args):
    """0x1403: 記 old/target/timeout（不切速），先回 ACK(舊速)，再 apply 切速。

    同步點 = SPEED_ACK：slave 用舊速發出 ACK，master 收到後兩邊一起切速。
    因此 ACK 必須「先送出」，送出後才 uart.init(target)。"""
    bus_type = int(args.get("bus_type", 0) or 0)
    bus_id   = int(args.get("bus_id", 0) or 0)
    speed    = int(args.get("speed", 0) or 0)
    timeout_ms = int(args.get("timeout_ms", 0) or 0)

    ok, cur, target = bus_speed.bus_speed_set(bus_type, bus_id, speed, timeout_ms)
    # 先回 ACK（此時仍在舊速，master 能收到）
    _reply(ctx, 0x1404, {
        "ok": ok, "bus_type": bus_type, "bus_id": bus_id,
        "cur_speed": cur, "target_speed": target,
    })
    # ACK 發出後才真正切速（等 FIFO 排空 + 末 byte 離開發射器）
    if ok:
        bus_speed.bus_speed_apply()


def on_speed_commit(ctx, args):
    """0x1405: 鎖定新速、取消回滾。"""
    bus_type = int(args.get("bus_type", 0) or 0)
    bus_id   = int(args.get("bus_id", 0) or 0)
    ok = bus_speed.bus_speed_commit(bus_type, bus_id)
    if ok:
        print("[HW] SPEED_COMMIT ok")


def on_speed_revert(ctx, args):
    """0x1406: 還原 old_baud。"""
    bus_type = int(args.get("bus_type", 0) or 0)
    bus_id   = int(args.get("bus_id", 0) or 0)
    bus_speed.bus_speed_revert(bus_type, bus_id)


def on_speed_query(ctx, args):
    """0x1407: 查狀態, 回 0x1408。"""
    bus_type = int(args.get("bus_type", 0) or 0)
    bus_id   = int(args.get("bus_id", 0) or 0)
    state, bt, bid, cur, target, remain = bus_speed.bus_speed_query(bus_type, bus_id)
    _reply(ctx, 0x1408, {
        "state": state, "bus_type": bt, "bus_id": bid,
        "cur_speed": cur, "target_speed": target, "remain_ms": remain,
    })


def register(app):
    app.disp.on(0x1401, on_hw_ctl)
    app.disp.on(0x1402, on_hw_query)
    app.disp.on(0x1403, on_speed_set)
    app.disp.on(0x1405, on_speed_commit)
    app.disp.on(0x1406, on_speed_revert)
    app.disp.on(0x1407, on_speed_query)
    print("[HW] Hardware actions registered")
