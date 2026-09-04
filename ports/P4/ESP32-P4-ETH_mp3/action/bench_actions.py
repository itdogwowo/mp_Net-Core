# action/bench_actions.py
# 通用接收吞吐性能測試（不分傳輸通道，純 NC4 協議層）
#
# 指令集（schema/bench.json，0x1811–0x1814）：
#   0x1811 BENCH_READY   清空計數器，回 0x1814 {ok:0} 證明已空
#   0x1812 BENCH_DATA    收到測試包（CRC 已由 StreamParser 驗證）→ ok+1，不回覆
#   0x1813 BENCH_RESULT  回 0x1814 {ok:N} 並清空計數器
#   0x1814 BENCH_REPORT  唯一回覆指令，payload {ok:u32}
#
# 統計語義：只統計「成功接收數 ok」。CRC 錯的幀在 StreamParser 就被丟棄、
# 不會 dispatch 到 handler，所以天然不計入 ok；失敗數由發送端自己算（發送數 - ok）。
#
# 計數器為模組層單例，不分通道 —— 這套測試是純 NC4 協議層的通用吞吐測試。

from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec

CMD_BENCH_READY = 0x1811
CMD_BENCH_DATA = 0x1812
CMD_BENCH_RESULT = 0x1813
CMD_BENCH_REPORT = 0x1814

_OK = 0


def _send_report(ctx, ok):
    """回 0x1814 BENCH_REPORT {ok:N}。走 ctx["send"]（綁定的傳輸層 write）。"""
    app = ctx.get("app")
    send_func = ctx.get("send")
    if not app or not send_func:
        print("❌ [bench] 回覆失敗: app={} send_func={}".format(
            "有" if app else "無", "有" if send_func else "無"))
        return
    cmd_def = app.store.get(CMD_BENCH_REPORT)
    if not cmd_def:
        print("❌ [bench] 回覆失敗: 找不到 BENCH_REPORT schema (0x1814)")
        return
    payload = SchemaCodec.encode(cmd_def, {"ok": int(ok) & 0xFFFFFFFF})
    pkt = Proto.pack(CMD_BENCH_REPORT, payload)
    # 回報實際送出 byte 數，方便確認回覆方向有沒有真的上線
    n = send_func(pkt)
    print("🔁 [bench] 回覆 REPORT ok={} ({}B) send_ret={}".format(ok, len(pkt), n))


def on_bench_ready(ctx, args):
    """0x1811：清空計數器，回 ok=0 證明已空。"""
    global _OK
    _OK = 0
    _send_report(ctx, 0)


def on_bench_data(ctx, args):
    """0x1812：收到一個 CRC 通過的測試包，ok+1，不回覆。"""
    global _OK
    _OK = (_OK + 1) & 0xFFFFFFFF


def on_bench_result(ctx, args):
    """0x1813：回 ok=N 統計結果，並清空計數器。"""
    global _OK
    ok = _OK
    _OK = 0
    _send_report(ctx, ok)


def register(app):
    app.disp.on(CMD_BENCH_READY, on_bench_ready)
    app.disp.on(CMD_BENCH_DATA, on_bench_data)
    app.disp.on(CMD_BENCH_RESULT, on_bench_result)
    print("✅ [Action] bench actions registered")
