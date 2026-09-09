#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netbus_ctl.py — NetBus 主服務與保活守護程式的控制工具（CLI）

用法
----
  python3 -B netbus_ctl.py start    啟動主服務（會自動拉起守護程式）
  python3 -B netbus_ctl.py stop     關閉整個系統（寫 stop.flag + 終止兩個行程）
  python3 -B netbus_ctl.py status   顯示兩邊 PID 與心跳狀態
  python3 -B netbus_ctl.py restart  等於 stop 後 start

設計說明
--------
- `stop` 會先寫下 `stop.flag`，這樣守護程式看到就不會把主服務救回來。
- 兩個行程都會被終止；若守護程式仍試圖重啟，`stop.flag` 會讓它立刻退出。
- 只有這個 CLI、網頁技術台的關閉鈕、console 選單 q 能真正關閉系統。
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
KA_DIR = os.path.join(DATA_DIR, "keepalive")

SERVICE_PID = os.path.join(KA_DIR, "service.pid")
GUARDIAN_PID = os.path.join(KA_DIR, "guardian.pid")
SERVICE_HB = os.path.join(KA_DIR, "service.heartbeat")
GUARDIAN_HB = os.path.join(KA_DIR, "guardian.heartbeat")
STOP_FLAG = os.path.join(KA_DIR, "stop.flag")
LOG_PATH = os.path.join(KA_DIR, "guardian.log")

MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "NetBusMaster.py")
GUARDIAN_SCRIPT = os.path.join(SCRIPT_DIR, "netbus_keepalive.py")


def _ensure_dir():
    os.makedirs(KA_DIR, exist_ok=True)


def read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((f.read() or "").strip())
    except Exception:
        return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def hb_age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _fmt_age(a):
    if a is None:
        return "無心跳"
    return "{:.1f}s 前".format(a)


def cmd_status(_args):
    _ensure_dir()
    print("=" * 52)
    print(" NetBus 系統狀態")
    print("=" * 52)
    print(" stop.flag : {}".format("存在（使用者已要求關閉）" if os.path.exists(STOP_FLAG) else "無"))
    for label, pid_path, hb_path in (
        ("主服務", SERVICE_PID, SERVICE_HB),
        ("守護程式", GUARDIAN_PID, GUARDIAN_HB),
    ):
        pid = read_pid(pid_path)
        alive = pid_alive(pid)
        print(" {:<8}: PID {} {}".format(
            label, pid if pid else "-", "（執行中）" if alive else "（未執行）"))
        print("           心跳 {}".format(_fmt_age(hb_age(hb_path))))
    print("=" * 52)
    return 0


def cmd_stop(_args):
    _ensure_dir()
    print("🛑 關閉整個系統…")
    # 1) 先寫 stop.flag（守護程式看到就不會救回）
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write("stopped by netbus_ctl at {}\n".format(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        print("   ✔ 已寫下 stop.flag")
    except Exception as e:
        print("   ⚠️ 寫 stop.flag 失敗: {}".format(e))

    # 2) 終止守護程式（先殺守護，避免它在主服務死後又拉起來）
    gpid = read_pid(GUARDIAN_PID)
    if pid_alive(gpid):
        try:
            os.kill(gpid, signal.SIGTERM)
            print("   ✔ 已終止守護程式 (PID {})".format(gpid))
        except Exception as e:
            print("   ⚠️ 終止守護程式失敗: {}".format(e))
    else:
        print("   · 守護程式未執行")

    # 3) 終止主服務
    spid = read_pid(SERVICE_PID)
    if pid_alive(spid):
        try:
            os.kill(spid, signal.SIGTERM)
            print("   ✔ 已終止主服務 (PID {})".format(spid))
        except Exception as e:
            print("   ⚠️ 終止主服務失敗: {}".format(e))
    else:
        print("   · 主服務未執行")

    # 4) 等它們收尾
    for _ in range(20):
        if not pid_alive(read_pid(GUARDIAN_PID)) and not pid_alive(read_pid(SERVICE_PID)):
            break
        time.sleep(0.25)

    # 5) 補刀：仍在跑就 SIGKILL
    for label, pid_path in (("守護程式", GUARDIAN_PID), ("主服務", SERVICE_PID)):
        pid = read_pid(pid_path)
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                print("   ⚠️ {} 未回應 TERM，已強制終止 (PID {})".format(label, pid))
            except Exception:
                pass

    # 6) 清掉殘留狀態檔
    for p in (SERVICE_PID, GUARDIAN_PID, SERVICE_HB, GUARDIAN_HB):
        try:
            os.remove(p)
        except OSError:
            pass
    print("✅ 已關閉。要再啟動請執行: python3 -B netbus_ctl.py start")
    return 0


def cmd_start(_args):
    _ensure_dir()
    # 清掉 stop.flag（使用者要啟動）
    try:
        os.remove(STOP_FLAG)
        print("   ✔ 已清除 stop.flag")
    except OSError:
        pass

    if pid_alive(read_pid(SERVICE_PID)):
        print("ℹ️ 主服務已在執行 (PID {})，不重複啟動".format(read_pid(SERVICE_PID)))
    else:
        try:
            out = open(LOG_PATH, "a", encoding="utf-8")
            subprocess.Popen(
                # --no-console: stdin 是 /dev/null, 不加會讓 input() 立刻 EOF 而秒退
                [sys.executable, "-B", MAIN_SCRIPT, "--no-console"],
                cwd=SCRIPT_DIR, stdin=subprocess.DEVNULL,
                stdout=out, stderr=out, start_new_session=True,
            )
            print("🚀 已啟動主服務（背景）。守護程式會由主服務自動拉起。")
        except Exception as e:
            print("❌ 啟動失敗: {}".format(e))
            return 1

    # 等主服務寫下 PID
    for _ in range(20):
        if pid_alive(read_pid(SERVICE_PID)):
            break
        time.sleep(0.5)
    time.sleep(1.0)
    return cmd_status(_args)


def cmd_restart(args):
    cmd_stop(args)
    time.sleep(1.5)
    return cmd_start(args)


def main():
    ap = argparse.ArgumentParser(description="NetBus 系統控制")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("start", help="啟動主服務與守護程式")
    sub.add_parser("stop", help="關閉整個系統")
    sub.add_parser("status", help="顯示狀態")
    sub.add_parser("restart", help="重新啟動")
    args = ap.parse_args()

    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "restart":
        return cmd_restart(args)
    if args.cmd == "status":
        return cmd_status(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
