# -*- coding: utf-8 -*-
"""CPU0 發指令 → CPU1 執行的完整跨核測試 (硬體無關)

驗證完整鏈路:
  core0: 真實 NC4 幀 → app.handle_stream → pop_frame → ADDR 過濾 → dispatch
         → SchemaCodec.decode → on_test_cmd handler 寫 bus.shared["test_cmd"]
  core1: _thread executor 輪詢 bus.shared.pop("test_cmd") → 執行(ack 計數器 +1)
  core0: 輪詢 bus.shared["test_ack"] 斷言 == N

機制說明:
  - ESP32 上主執行緒 = core0, _thread.start_new_thread = core1 (真實跨核)。
  - 跨核指令沿用既有 producer→shared→consumer 契約 (同 PixelTask._consume_cmds /
    FsScanTask.fs_scan_requested), 用 bus.shared dict pop/get, 不引入新傳輸層。
  - 0x19F0 為測試專用指令碼 (0x19xx 空域), 只存在本腳本 store.cmd_map, 不寫 /schema、
    不影響 production。

用法 (裝置 REPL):
  exec(open("test/thread/test_cpu01_cmd.py").read())
  run()          # 發 N 條指令, 驗證 core1 全部執行

PC (CPython) 無 _thread/machine → 印 SKIP。
"""

import sys, time

IS_MP = (sys.implementation.name == 'micropython')


def run(N=50, timeout_ms=5000):
    if not IS_MP:
        print("[SKIP] 需 MicroPython (CPython 無 _thread / ESP32 跨核)")
        return False

    import _thread
    from app import App
    from lib.sys.sys_bus import bus
    from lib.sys.proto import Proto, StreamParser, MAX_PAYLOAD
    from lib.sys.schema_codec import SchemaCodec

    TEST_CMD = 0x19F0

    # ── App (真實 schema + dispatcher + register_all) ──
    app = App()

    # ── 注入測試指令 (不改 schema 檔, 僅 store 內) ──
    app.store.cmd_map[TEST_CMD] = {
        "cmd": "0x19F0",
        "name": "TEST_CPU01_CMD",
        "payload": [{"name": "seq", "type": "u8"}],
    }
    app.store.finalize()

    # ── core0 handler: 收到指令 → 寫共享指令 ──
    def on_test_cmd(ctx, args):
        bus.shared["test_cmd"] = args.get("seq", 0)

    app.disp.on(TEST_CMD, on_test_cmd)

    # 關閉 dispatch debug log (否則 50 條指令刷屏)
    app.disp.debug_level = 0

    # ── 清除測試鍵 ──
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
            time.sleep_ms(0)   # 讓 GIL, 給 core0 發指令機會

    _thread.stack_size(16 * 1024)
    _thread.start_new_thread(executor_loop, (stop,))

    # ── core0 發指令: 走完整真實 ingress ──
    # 同步握手: 發一條 → 等 core1 消費 (ack 遞增) 再發下一條。
    # test_cmd 是單一 key (非佇列), 一次只能有一個 outstanding 指令,
    # 與 pixel_play / fs_scan_requested 的 one-shot 語意一致。
    parser = StreamParser(max_len=MAX_PAYLOAD)
    cmd_def = app.store.get(TEST_CMD)

    print("\n" + "=" * 56)
    print("CPU0 發指令 → CPU1 執行 跨核測試")
    print("=" * 56)
    print("發送 {} 條 0x19F0 指令 (真實 dispatch + 同步握手)...".format(N))

    for seq in range(N):
        payload = SchemaCodec.encode(cmd_def, {"seq": seq & 0xFF})
        frame = Proto.pack(TEST_CMD, payload, addr=0xFFFF)
        app.handle_stream(parser, frame, "Test", None, None)

        # 等 core1 消費這條 (ack 由 seq 變 seq+1)
        t0 = time.ticks_ms()
        while bus.shared.get("test_ack", 0) <= seq:
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                break
            time.sleep_ms(0)

    # ── 最終斷言 ──
    ack = bus.shared.get("test_ack", 0)
    last = bus.shared.get("test_ack_last", -1)

    stop[0] = True
    time.sleep_ms(50)

    print("-" * 56)
    print("core1 ack 計數 : {} / {}".format(ack, N))
    print("core1 最後 seq : {}".format(last))
    ok = (ack == N) and (last == (N - 1) & 0xFF)
    if ok:
        print("✅ PASS — core0 指令逐條由 core1 執行 (無遺漏、順序正確)")
    else:
        print("❌ FAIL — 期望 ack={} last={}".format(N, (N - 1) & 0xFF))
    print("=" * 56)
    return ok


if __name__ == "__main__":
    run()
