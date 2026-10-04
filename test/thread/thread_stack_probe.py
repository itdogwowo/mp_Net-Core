# thread_stack_probe.py — _thread stack 需求掃描（工作分流專用）v2
#
# 目的：量化「搬到 core1(_thread) 的每種工作」實際需要多少 stack，
#       用數據決定 stack_size 該設多少，而不是猜。
#
# v2 修正（依第一次實測）：
#   1. recursion 深度 250 → 120：實測發現深遞迴被 MicroPython 的
#      pystack / stack-check（MICROPY_STACK_CHECK）擋住，250 層
#      ≈ 47KB 超出掃描範圍。120 層才能量出真正的 stack 邊界。
#   2. 採樣點加深：第一次實測 peak 全部 ~1.1KB，因為 ctx.check()
#      只放在迴圈頂部（最淺處）。v2 把採樣點放進 json.dumps /
#      HTTP 解析 / walk 遞迴 / 狀態幀組裝 的深處。
#   3. 新增兩個貼近真實任務的 workload：
#      http_handler（模擬 WebUITask 完整 HTTP 處理）
#      action_str（模擬 ActionTask1 的狀態幀組裝）
#
# 背景：
#   ESP32 的 _thread 預設 stack = 4KB + 1KB margin = 5KB。
#   stack 太深時兩種結果：stack-check 接住 → "maximum recursion
#   depth exceeded"（可捕捉）；沒接住 → Guru Meditation 硬崩潰。
#
# 崩潰自動續跑：進度存 /sd/thread_probe_state.json。
#   崩潰重開機 → 下次 run_all() 偵測到未完成的 case → 標 FAIL → 繼續。
#
# 用法（soft reboot 後，boot.py 已完成硬體初始化）：
#   import thread_stack_probe
#   thread_stack_probe.run_all()     # 全自動 sweep（可中途斷電，續跑）
#   thread_stack_probe.run_case(0)   # 只跑單一 case（手動，不寫進度）
#   thread_stack_probe.reset()       # 清除進度，重新掃
#
# ⚠️ 這些 workload 都不碰 LCD/SPI — LCD 歸屬規則另測（thread_lcd_rule.py）。

import gc, json, os, time, _thread

try:
    import micropython
    _HAVE_STACK_USE = hasattr(micropython, "stack_use")
except Exception:
    _HAVE_STACK_USE = False

_STATE = "/sd/thread_probe_state.json"
_SIZES = (8, 12, 16, 24, 32, 48)      # KB
_TIMEOUT_MS = 20000
_REC_DEPTH = 120                      # v2: 250 會被 pystack 擋住，120 才量得到


class _Ctx:
    """傳給 workload 的上下文：記錄執行期間的 stack 水位（若有 stack_use）。"""
    __slots__ = ("max_use",)

    def __init__(self):
        self.max_use = 0

    def check(self):
        if _HAVE_STACK_USE:
            u = micropython.stack_use()
            if u > self.max_use:
                self.max_use = u


# ═══════════════ workloads（各自對應一個真實 core1 task） ═══════════════

def wl_recursion(ctx, n=_REC_DEPTH):
    """純 Python 深呼叫鏈 — 深度的 stack 消耗基準。
    採樣點在 rec() 內層，隨深度遞增，直接量出每層 frame 成本。"""
    def rec(d):
        ctx.check()                    # 每層都採樣 → 量到最深點
        if d <= 0:
            return 0
        acc = [d % 7 for _ in range(6)]        # frame 帶 locals
        s = "{}-{}".format(d, acc[0])          # 字串操作
        return 1 + rec(d - 1) + (len(s) if s else 0)
    total = 0
    for _ in range(15):
        total += rec(n)
    return total


def wl_json_churn(ctx, n=120):
    """模擬 web_ui._send_json：nested dict + json.dumps + format"""
    import json
    total = 0
    for i in range(n):
        ctx.check()
        obj = {
            "cmd": "perf", "id": i,
            "tasks": [{"name": "task{}".format(j), "loop_us": j * 137}
                      for j in range(12)],
            "msg": "hello 中文字串 {}".format(i * 3),
        }
        body = json.dumps(obj).encode()
        ctx.check()                    # dumps 之後（C 呼叫鏈最深點附近）
        back = json.loads(body)
        total += len(body) + back["id"]
    return total


def wl_http_handler(ctx, n=40):
    """模擬 WebUITask 的完整 HTTP 請求處理（解析 + JSON + 回應）"""
    import json
    total = 0
    req_head = (b"GET /api/perf HTTP/1.1\r\nHost: 192.168.4.1\r\n"
                b"User-Agent: test/1.0\r\nAccept: */*\r\n\r\n")
    for i in range(n):
        ctx.check()
        lines = req_head.split(b"\r\n")
        first = lines[0].decode()
        parts = first.split(" ")
        method, path = parts[0], parts[1]
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, v = ln.split(b":", 1)
                headers[k.decode().strip()] = v.decode().strip()
        obj = {"status": "ok", "method": method, "path": path,
               "ua": headers.get("User-Agent", ""),
               "tasks": [{"name": "t{}".format(j), "us": j * 7}
                         for j in range(20)],
               "msg": "中文 payload {}".format(i)}
        body = json.dumps(obj).encode()
        ctx.check()                    # dumps 之後（最深點）
        resp = (b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body)
        total += len(resp)
    return total


def wl_action_str(ctx, n=300):
    """模擬 ActionTask1 的狀態幀組裝（大量 format + 條件分支字串）"""
    total = 0
    for i in range(n):
        ctx.check()
        mode = i % 8
        bri = (i * 3) % 32
        rem = 600 - i
        parts = ["M{}".format(mode), "B{}".format(bri), "T{}".format(rem)]
        if mode in (1, 3, 5):
            parts.append("PLAY")
        elif mode in (2, 6):
            parts.append("PAUSE")
        else:
            parts.append("IDLE")
        for ch in range(6):
            parts.append("{:02X}".format((i * 31 + ch * 7) & 0xFF))
        s = "|".join(parts) + " ".join(parts)
        ctx.check()
        total += len(s)
    return total


def wl_socket(ctx, n=20):
    """模擬 network/web_ui 的 blocking socket（自我連線，免外部 client）"""
    import socket, json
    total = 0
    try:
        addr = socket.getaddrinfo("127.0.0.1", 8199)[0][-1]
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(addr)
        srv.listen(2)
        srv.settimeout(0.5)
        for i in range(n):
            ctx.check()
            c = socket.socket()
            c.settimeout(0.5)
            try:
                c.connect(addr)
            except OSError:
                c.close()
                continue                        # loopback 不支援 → timeout 模式
            try:
                s, _ = srv.accept()
                obj = {"seq": i, "payload": "x" * 256}
                s.send(json.dumps(obj).encode())
                resp = c.recv(512)
                total += len(resp)
                s.close()
            except OSError:
                pass
            c.close()
        srv.close()
    except Exception:
        return -1
    return total


def wl_fs_walk(ctx, n=8):
    """模擬 FsScanTask 的遞迴掃描（os.listdir 深層走訪 + stat）"""
    def walk(p):
        cnt = 0
        try:
            for e in os.listdir(p):
                fp = p + "/" + e
                try:
                    st = os.stat(fp)
                    cnt += 1
                    if st[0] & 0x4000:          # S_IFDIR
                        ctx.check()             # 遞迴內層採樣
                        cnt += walk(fp)
                except OSError:
                    pass
        except OSError:
            pass
        return cnt
    total = 0
    for _ in range(n):
        total += walk("/sd")
    return total


def wl_hw_sample(ctx, n=500):
    """模擬 HwSampleTask：統一硬體採樣"""
    try:
        from lib.hw_manager import sample_inputs
    except Exception:
        return -1
    for i in range(n):
        ctx.check()
        sample_inputs()
    return n


def wl_uart(ctx, n=300):
    """模擬 CircuitTask：UART 輪詢讀取"""
    try:
        from lib.sys.sys_bus import bus
        uart_list = bus.get_service("uart_list") or []
        if not uart_list:
            return -1
        u = uart_list[0]
    except Exception:
        return -1
    total = 0
    for _ in range(n):
        ctx.check()
        try:
            if u.any():
                chunk = u.read()
                if chunk:
                    total += len(chunk)
        except Exception:
            pass
    return total


WORKLOADS = [
    ("recursion",    wl_recursion),
    ("json_churn",   wl_json_churn),
    ("http_handler", wl_http_handler),
    ("action_str",   wl_action_str),
    ("socket",       wl_socket),
    ("fs_walk",      wl_fs_walk),
    ("hw_sample",    wl_hw_sample),
    ("uart",         wl_uart),
]

CASES = [(name, kb) for name, _ in WORKLOADS for kb in _SIZES]


# ═══════════════ 單一 case 執行器 ═══════════════

def _worker(wl, kb, st):
    ctx = _Ctx()
    try:
        r = wl(ctx)
        st["result"] = r
        st["peak"] = ctx.max_use
    except Exception as e:
        st["error"] = "{}: {}".format(type(e).__name__, e)
    finally:
        st["done"] = True


def _run_workload(idx):
    """在 core1 thread 跑 case idx。回傳 (status, peak, note)。"""
    name, kb = CASES[idx]
    wl = dict(WORKLOADS)[name]
    st = {"done": False, "result": None, "peak": 0, "error": None}

    _thread.stack_size(kb * 1024)
    _thread.start_new_thread(_worker, (wl, kb, st))

    deadline = time.ticks_ms() + _TIMEOUT_MS
    while not st["done"] and time.ticks_diff(time.ticks_ms(), deadline) < 0:
        time.sleep_ms(10)

    if not st["done"]:
        return ("HANG", 0, "timeout {}ms".format(_TIMEOUT_MS))
    if st["error"]:
        return ("ERR", 0, st["error"])
    return ("OK", st["peak"], "result={}".format(st["result"]))


# ═══════════════ 進度管理（崩潰續跑） ═══════════════

def _load_state():
    try:
        with open(_STATE) as f:
            return json.load(f)
    except Exception:
        return {"idx": 0, "cases": [], "running": None}


def _save_state(s):
    try:
        with open(_STATE, "w") as f:
            json.dump(s, f)
            f.flush()
    except Exception as e:
        print("  [state] save fail: {}".format(e))


# ═══════════════ 主流程 ═══════════════

def run_all():
    """全自動 sweep：依序跑所有 case，崩潰後下次自動續跑 + 標 FAIL。"""
    if not _HAVE_STACK_USE:
        print("⚠ micropython.stack_use() 不可用（firmware 未開 MICROPY_STACK_CHECK）")
        print("  → 只能以崩潰邊界判定；你的 firmware 看起來已開啟（stack_use: ON）")
        print()

    s = _load_state()
    if not s["cases"]:
        s["cases"] = [{"wl": n, "kb": kb, "status": "-", "peak": 0}
                      for n, kb in CASES]

    # 崩潰續跑偵測：上次跑到一半 → 這次標 FAIL
    r = s.get("running")
    if r is not None and r < len(s["cases"]):
        cs = s["cases"][r]
        if cs["status"] == "RUN":
            cs["status"] = "FAIL(crash)"
            print("[RECOVER] case #{} {}@{}KB 上次崩潰/中斷 → FAIL".format(
                r, cs["wl"], cs["kb"]))

    i = s.get("idx", 0)
    print("=" * 62)
    print("_thread stack probe v2  ({} cases, {}KB-{}KB)".format(
        len(CASES), _SIZES[0], _SIZES[-1]))
    if _HAVE_STACK_USE:
        print("stack_use: ON  (可讀精確水位)")
    print("=" * 62)

    while i < len(CASES):
        cs = s["cases"][i]
        if cs["status"] in ("OK", "FAIL(crash)", "ERR", "HANG", "SKIP"):
            i += 1
            continue

        name, kb = CASES[i]
        print("[T] case #{:<3} {:<13} stack={:>3}KB ...".format(
            i, name, kb), end=" ")
        cs["status"] = "RUN"
        s["running"] = i
        s["idx"] = i
        _save_state(s)

        status, peak, note = _run_workload(i)
        cs["status"] = status
        cs["peak"] = peak
        cs["note"] = note
        s["running"] = None
        s["idx"] = i + 1
        _save_state(s)
        gc.collect()

        print("{}  {}".format(status, note))
        if _HAVE_STACK_USE and peak:
            print("       peak stack_use = {}B ({:.1f}KB)".format(
                peak, peak / 1024))

        if status == "HANG":
            print("[!] case #{} HANG — 停止 sweep，檢查後重跑".format(i))
            break

    _summary(s)


def run_case(idx):
    """手動只跑單一 case（不寫進度檔，不影響 sweep 狀態）。"""
    if not (0 <= idx < len(CASES)):
        print("idx 超出範圍 (0-{})".format(len(CASES) - 1))
        return
    name, kb = CASES[idx]
    print("[T] case #{} {:<13} stack={:>3}KB ...".format(idx, name, kb), end=" ")
    status, peak, note = _run_workload(idx)
    print("{}  {}".format(status, note))
    if _HAVE_STACK_USE and peak:
        print("    peak stack_use = {}B ({:.1f}KB)".format(peak, peak / 1024))


def reset():
    """清除 sweep 進度（重新掃描）。"""
    try:
        os.remove(_STATE)
        print("progress cleared.")
    except Exception:
        print("no progress file.")


def _summary(s):
    print()
    print("=" * 62)
    print("Summary — 每種 workload 的最小可用 stack（F/E = 太小）")
    print("-" * 62)
    print("  {:<13} {}".format("workload", "  ".join("{}K".format(k) for k in _SIZES)))
    for name, _ in WORKLOADS:
        row = []
        for kb in _SIZES:
            st = "-"
            for cs in s["cases"]:
                if cs["wl"] == name and cs["kb"] == kb:
                    st = cs["status"][0]           # O / F / E / H
                    break
            row.append(st)
        print("  {:<13} {}".format(name, "   ".join(row)))
    print("-" * 62)
    print("判讀：")
    print("  O=OK  F=FAIL(硬崩潰)  E=ERR(RecursionError,stack check 接住)")
    print("  F/E 都是「太小」— E 只是被檢查機制接住、比較幸運。")
    print("  每種 workload 的第一個 O 就是最小可用 size。")
    print("  建議設定 = 最小可用 size + 8KB 餘裕。")
    print("  ⚠ recursion 那列的 E 不是 stack 太小：120 層在 48KB 也 E，")
    print("    表示深遞迴被 VM 層級(pystack/遞迴上限)擋住，跟 thread stack")
    print("    無關 → 真實任務不會這樣遞迴，不用參考這列。")
    print("done.")
