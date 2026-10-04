# -*- coding: utf-8 -*-
"""CPU0 發指令 → CPU1 執行 的完整整合測試 (硬體無關 + 新增指令實測)

三個部分:
  A. ADDR 過濾 + 跨核:
        cpu0 用三種 addr 發送指令 (走真實 dispatch 鏈), 觀察 cpu1 是否執行:
          - 正確 addr (本機 cid)   → 應執行 (ack +1)
          - FF addr (0xFFFF 廣播)  → 應執行 (ack +1)
          - 錯誤 addr (他人 cid)   → 不應執行 (core0 過濾, cpu1 無感)
        統計「判斷正確次數」= 三類各自符合預期的筆數總和。
  B. 新增指令實測 (NET_START):
        發 NET_START(0x1012, iface_type=1=wifi), 驗證真的把 WiFi 開起來
        (回應 ok=1 且 network_manager 實際取得 IP)。硬體限制: 只測 wifi。

機制: ESP32 主執行緒=core0, _thread=core1 (真實跨核)。跨核指令沿用
      producer→bus.shared→consumer 契約 (同 PixelTask._consume_cmds)。
      0x19F0 為測試專用指令碼, 只存在本腳本 store.cmd_map, 不寫 /schema。

用法 (裝置 REPL):
  exec(open("test/thread/test_cpu01_full.py").read())
  run()
"""

import sys, time

IS_MP = (sys.implementation.name == 'micropython')


def run(M=10, timeout_ms=5000):
    if not IS_MP:
        print("[SKIP] 需 MicroPython (CPython 無 _thread / ESP32 跨核)")
        return False

    import _thread
    from app import App
    from lib.sys.sys_bus import bus
    from lib.sys.proto import Proto, StreamParser, MAX_PAYLOAD, ADDR_BROADCAST
    from lib.sys.schema_codec import SchemaCodec

    TEST_CMD = 0x19F0
    MY_CID = 0xABCD
    WRONG_CID = 0x1234

    app = App()
    app.disp.debug_level = 0   # 關閉 dispatch log 噪音

    # 注入測試指令 (只 store 內, 不寫 schema)
    app.store.cmd_map[TEST_CMD] = {
        "cmd": "0x19F0", "name": "TEST_CPU01_CMD",
        "payload": [{"name": "seq", "type": "u8"}],
    }
    app.store.finalize()

    # ── 本機 cid (測試用常數, 僅內存) ──
    bus.cid = MY_CID

    # ── core0 handler: 收到指令 → 寫共享指令 (供 cpu1 執行) ──
    def on_test_cmd(ctx, args):
        bus.shared["test_cmd"] = args.get("seq", 0)

    app.disp.on(TEST_CMD, on_test_cmd)

    bus.shared.pop("test_cmd", None)
    bus.shared["test_ack"] = 0
    bus.shared["test_ack_last"] = -1

    # ── core1 executor ──
    stop = [False]

    def executor_loop(stop):
        while not stop[0]:
            cmd = bus.shared.pop("test_cmd", None)
            if cmd is not None:
                bus.shared["test_ack"] = bus.shared.get("test_ack", 0) + 1
                bus.shared["test_ack_last"] = cmd
            time.sleep_ms(0)

    _thread.stack_size(16 * 1024)
    _thread.start_new_thread(executor_loop, (stop,))

    parser = StreamParser(max_len=MAX_PAYLOAD)
    cmd_def = app.store.get(TEST_CMD)

    print("\n" + "=" * 60)
    print("A. ADDR 過濾 + 跨核 (cpu0 發 → cpu1 執行)")
    print("=" * 60)
    print("本機 cid = 0x{:04X}  錯誤 cid = 0x{:04X}  每類 {} 條".format(MY_CID, WRONG_CID, M))

    correct = 0   # 判斷正確次數

    def send(addr, seq):
        """走真實 dispatch 發一幀, 回傳 handle_stream 的 packet_found。"""
        payload = SchemaCodec.encode(cmd_def, {"seq": seq & 0xFF})
        frame = Proto.pack(TEST_CMD, payload, addr=addr)
        return app.handle_stream(parser, frame, "Test", None, None)

    def wait_ack(target, timeout_ms=timeout_ms):
        t0 = time.ticks_ms()
        while bus.shared.get("test_ack", 0) < target:
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                return False
            time.sleep_ms(0)
        return True

    # ── 正確 addr: 每條都應被接受並執行 (逐條握手, 避免單 key 覆寫) ──
    base = bus.shared.get("test_ack", 0)
    for i in range(M):
        r = send(MY_CID, i)
        if r and wait_ack(base + i + 1):
            correct += 1          # 接受且執行 → 判斷正確
    executed_correct = bus.shared.get("test_ack", 0) - base
    print("  正確 addr: 接受 {} 條, 執行 {} 條  {}".format(
        M, executed_correct, "✅" if executed_correct == M else "❌"))

    # ── FF addr (廣播): 每條都應被接受並執行 (逐條握手) ──
    base = bus.shared.get("test_ack", 0)
    for i in range(M):
        r = send(ADDR_BROADCAST, i)
        if r and wait_ack(base + i + 1):
            correct += 1          # 廣播接受且執行 → 判斷正確
    executed_bcast = bus.shared.get("test_ack", 0) - base
    print("  FF 廣播 : 接受 {} 條, 執行 {} 條  {}".format(
        M, executed_bcast, "✅" if executed_bcast == M else "❌"))

    # ── 錯誤 addr: 每條都應被過濾 (不執行) ──
    base = bus.shared.get("test_ack", 0)
    rejected = 0
    for i in range(M):
        r = send(WRONG_CID, i)
        if not r:
            rejected += 1
            correct += 1          # 拒絕 → 判斷正確
    time.sleep_ms(100)            # 給 cpu1 機會 (若錯誤沒被擋, 會誤執行)
    executed_wrong = bus.shared.get("test_ack", 0) - base
    print("  錯誤 addr: 過濾 {} 條, 誤執行 {} 條  {}".format(
        M, executed_wrong, "✅" if (rejected == M and executed_wrong == 0) else "❌"))

    total_sent = M * 3
    print("-" * 60)
    print("  判斷正確: {}/{}".format(correct, total_sent))

    stop[0] = True
    time.sleep_ms(50)

    # ═══════════════════════════════════════════════════════
    # B. 新增指令實測: NET_START wifi
    # ═══════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("B. NET_START 指令實測 (iface_type=1 wifi)")
    print("=" * 60)

    nm = bus.get_service("network_manager")
    if nm is None:
        from lib.network_manager import NetworkManager
        nm = NetworkManager(bus)
        bus.register_service("network_manager", nm)
        print("  (建立 network_manager 服務)")

    rsp_frames = []

    def collect(data):
        rsp_frames.append(bytes(data))

    net_cmd_def = app.store.get(0x1012)
    net_payload = SchemaCodec.encode(net_cmd_def, {"iface_type": 1})
    net_frame = Proto.pack(0x1012, net_payload, addr=ADDR_BROADCAST)
    app.handle_stream(parser, net_frame, "Test", collect, None)

    # 解碼 NET_START_RSP (0x1013)
    rsp_ok = None
    rsp_iface = None
    rsp_ip = None
    rsp_parser = StreamParser(max_len=MAX_PAYLOAD)
    for f in rsp_frames:
        rsp_parser.feed(f)
        while True:
            r = rsp_parser.pop_frame()
            if r is None:
                break
            _v, _a, cmd, pl = r
            if cmd == 0x1013:
                d = app.store.get(0x1013)
                args = SchemaCodec.decode(d, pl, app.store)
                rsp_ok = args.get("ok")
                rsp_iface = args.get("iface")
                rsp_ip = args.get("ip")

    print("  NET_START_RSP: ok={} iface={} ip={}".format(rsp_ok, rsp_iface, rsp_ip))

    # 等 network_manager 實際取得 IP (最多 ~20s, wifi 掃描/連線較慢)
    ips = nm.get_ips()
    t0 = time.ticks_ms()
    while not ips and time.ticks_diff(time.ticks_ms(), t0) < 20000:
        time.sleep_ms(200)
        ips = nm.get_ips()

    net_started = (rsp_ok == 1) and bool(ips)
    print("  實際介面 IP : {}".format(ips))
    print("  {}".format("✅ PASS — NET_START(wifi) 真的把網絡開起來" if net_started else "❌ FAIL — 網絡未開"))

    # ═══════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    part_a_ok = (correct == total_sent)
    overall = part_a_ok and net_started
    print("A. ADDR 過濾+跨核 : {}".format("✅" if part_a_ok else "❌"))
    print("B. NET_START wifi : {}".format("✅" if net_started else "❌"))
    print("整體 : {}".format("✅ PASS" if overall else "❌ FAIL"))
    print("=" * 60)
    return overall


if __name__ == "__main__":
    run()
