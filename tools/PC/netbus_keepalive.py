#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""netbus_keepalive.py — NetBusMaster 的獨立守護程式（保活機制）

用途
----
NetBusMaster 是長時間運行的演出控制服務。若它的終端機被關掉（例如 VS Code
被關閉、Terminal 視窗誤關），行程會收到 SIGHUP 而直接死亡，網頁遙控也跟著斷。
本程式是**完全獨立的行程**（`start_new_session=True`，不受終端機影響），負責：

  1. 每 N 秒寫自己的心跳檔。
  2. 檢查主服務心跳；超過逾時就判定死亡 → 重新拉起它。
  3. 主服務也會反過來監督本程式（見 NetBusMaster._start_guardian）。

互相監督的結果：任一方被外力殺掉，另一方會把它救回來。**只有**使用者主動
關閉（網頁技術台的關閉鈕、`netbus_ctl.py stop`、console 選單 q）會寫下
`stop.flag`，雙方看到旗標就停止互相救援並退出。

檔案佈局（data/keepalive/）
--------------------------
  service.pid       主服務 PID
  guardian.pid      本守護程式 PID
  service.heartbeat 主服務心跳（每 N 秒更新 mtime）
  guardian.heartbeat 本程式心跳
  stop.flag         存在 = 使用者要求關閉（雙方尊重）
  guardian.log      本程式日誌
  restart.sh        重啟主服務用的小腳本（供 osascript 呼叫）

用法
----
  python3 -B netbus_keepalive.py            # 前景跑（除錯用）
  python3 -B netbus_keepalive.py --daemon   # 由 NetBusMaster 拉起時使用
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
RESTART_SH = os.path.join(KA_DIR, "restart.sh")

MAIN_SCRIPT = os.path.join(SCRIPT_DIR, "NetBusMaster.py")

# 節奏（可被環境變數覆寫，讓測試能加速）
HEARTBEAT_S = float(os.environ.get("NETBUS_HB_S", "2.0"))
TIMEOUT_S = float(os.environ.get("NETBUS_TIMEOUT_S", "10.0"))
MAX_BACKOFF_S = float(os.environ.get("NETBUS_MAX_BACKOFF_S", "30.0"))


def _ensure_dir():
    os.makedirs(KA_DIR, exist_ok=True)


def log(msg):
    """寫入 guardian.log（帶時間戳）；失敗不影響主流程。"""
    try:
        _ensure_dir()
        line = "[{}] {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
        # 日誌過大就截斷（保留最後 ~2000 行）
        if os.path.getsize(LOG_PATH) > 512 * 1024:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-2000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except Exception:
        pass


def touch(path):
    """更新檔案 mtime（心跳）；檔案不存在則建立。"""
    try:
        _ensure_dir()
        with open(path, "a"):
            os.utime(path, None)
    except Exception:
        pass


def age(path):
    """回傳檔案 mtime 距今秒數；不存在回 None。"""
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def read_pid(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int((f.read() or "").strip())
    except Exception:
        return None


def pid_alive(pid):
    """檢查 PID 是否還活著（用 signal 0 探測，不影響目標）。"""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # 存在但無權限（例如 root 行程）
    except Exception:
        return False


def service_alive():
    """主服務是否活著：PID 存在 + 心跳新鮮。兩者都要成立。"""
    if os.path.exists(STOP_FLAG):
        return True   # 使用者要關，別當成「需要救」
    pid = read_pid(SERVICE_PID)
    if not pid_alive(pid):
        return False
    a = age(SERVICE_HB)
    if a is None:
        # PID 在但還沒寫心跳 → 剛啟動，給寬限期
        pa = age(SERVICE_PID)
        return pa is None or pa < TIMEOUT_S
    return a < TIMEOUT_S


def service_never_seen():
    """完全沒有任何主服務痕跡（PID 檔不存在）。用於啟動寬限期判斷。"""
    return read_pid(SERVICE_PID) is None and age(SERVICE_HB) is None


def _write_restart_script():
    """產生重啟腳本（osascript 會叫用它）。用 shell 是為了讓 Terminal 視窗留著。"""
    try:
        _ensure_dir()
        py = sys.executable
        body = (
            "#!/bin/bash\n"
            "# 由 netbus_keepalive.py 自動產生 — 重啟 NetBusMaster\n"
            "cd {d!r} || exit 1\n"
            "exec {py!r} -B {main!r} --guardian-restart\n"
        ).format(d=SCRIPT_DIR, py=py, main=MAIN_SCRIPT)
        with open(RESTART_SH, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(RESTART_SH, 0o755)
        return True
    except Exception as e:
        log("建立 restart.sh 失敗: {}".format(e))
        return False


def _restart_via_terminal():
    """用 osascript 開新 Terminal 視窗重啟（保留 console 選單）。

    需要 macOS「自動化」權限（控制 Terminal）。失敗回 False → 呼叫端退回背景啟動。
    """
    if sys.platform != "darwin":
        return False
    if not _write_restart_script():
        return False
    # 🔧 osascript 可能阻塞數秒；先刷新自己的心跳，避免主服務誤判守護程式死亡
    touch(GUARDIAN_HB)
    try:
        # 用 do script 開新視窗並執行重啟腳本
        script = 'tell application "Terminal"\n  do script "{}"\n  activate\nend tell'.format(
            RESTART_SH.replace('"', '\\"')
        )
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=8)
        touch(GUARDIAN_HB)
        if r.returncode == 0:
            log("已透過 Terminal 視窗重啟主服務")
            return True
        log("osascript 失敗 (rc={}): {}".format(r.returncode, (r.stderr or "").strip()[:200]))
    except Exception as e:
        touch(GUARDIAN_HB)
        log("osascript 例外: {}".format(e))
    return False


def _restart_background():
    """背景重啟（無 console）：stdin=/dev/null，輸出導向 log。"""
    try:
        _ensure_dir()
        out = open(LOG_PATH, "a", encoding="utf-8")
        subprocess.Popen(
            # --no-console: 背景啟動時 stdin 是 /dev/null, 不加會讓 input() 秒退
            [sys.executable, "-B", MAIN_SCRIPT, "--guardian-restart", "--no-console"],
            cwd=SCRIPT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
        log("已背景重啟主服務（無 console）")
        return True
    except Exception as e:
        log("背景重啟失敗: {}".format(e))
        return False


def restart_service():
    """重啟主服務：先試 Terminal 視窗，失敗退回背景。回傳是否成功。"""
    # 清掉舊 PID/心跳，避免剛啟動就被判定成舊狀態
    for p in (SERVICE_PID, SERVICE_HB):
        try:
            os.remove(p)
        except OSError:
            pass
    if _restart_via_terminal():
        return True
    return _restart_background()


def _install_signals(stop_ref):
    """收到 TERM/HUP 就優雅退出（並記 log）。"""
    def handler(signum, _frame):
        log("收到信號 {} → 守護程式退出".format(signum))
        stop_ref["stop"] = True
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="NetBus 保活守護程式")
    ap.add_argument("--daemon", action="store_true",
                    help="背景模式（由 NetBusMaster 拉起時使用）")
    args = ap.parse_args()

    _ensure_dir()

    # 單例保護：已有守護程式在跑就退出
    existing = read_pid(GUARDIAN_PID)
    if pid_alive(existing) and existing != os.getpid():
        print("ℹ️ 守護程式已在執行 (PID {}), 本次退出".format(existing))
        return 0

    with open(GUARDIAN_PID, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    stop_ref = {"stop": False}
    _install_signals(stop_ref)

    log("=" * 50)
    log("守護程式啟動 (PID {}, 心跳 {}s, 逾時 {}s)".format(os.getpid(), HEARTBEAT_S, TIMEOUT_S))

    backoff = 1.0
    started_at = time.time()
    # 🔧 啟動寬限期: 剛被主服務拉起時, 它可能還在 __init__ (載 config/開 socket),
    #    PID 檔尚未寫入。此時若立刻判定死亡就會誤開 Terminal 重啟 → 服務重複。
    #    因此「從未見過主服務」的情況下先等一個逾時週期。
    grace_until = started_at + max(TIMEOUT_S, 3.0)

    while not stop_ref["stop"]:
        touch(GUARDIAN_HB)

        # 1) 使用者要求關閉 → 尊重，退出
        if os.path.exists(STOP_FLAG):
            log("偵測到 stop.flag → 守護程式退出")
            break

        # 2) 主服務是否活著
        if service_alive():
            backoff = 1.0
            grace_until = 0.0
        elif service_never_seen() and time.time() < grace_until:
            # 剛啟動、主服務還沒現身 → 寬限期內不動作
            log("等待主服務啟動中 (寬限 {:.0f}s)".format(grace_until - time.time()))
            time.sleep(HEARTBEAT_S)
            continue
        else:
            # 🔧 重啟前二次確認: 避免與主服務正在啟動的瞬間競態
            time.sleep(min(HEARTBEAT_S, 1.0))
            touch(GUARDIAN_HB)
            if service_alive():
                continue
            log("主服務無回應 (心跳 {}s 前, PID {}) → 嘗試重啟".format(
                "?" if age(SERVICE_HB) is None else round(age(SERVICE_HB), 1),
                read_pid(SERVICE_PID)))
            if restart_service():
                backoff = 1.0
                # 給新行程時間寫心跳
                for _ in range(int(max(1, TIMEOUT_S / HEARTBEAT_S))):
                    time.sleep(HEARTBEAT_S)
                    touch(GUARDIAN_HB)
                    if service_alive() or os.path.exists(STOP_FLAG):
                        break
            else:
                log("重啟失敗 → 退避 {:.0f}s 後重試".format(backoff))
                time.sleep(backoff)
                backoff = min(MAX_BACKOFF_S, backoff * 2)
                continue

        time.sleep(HEARTBEAT_S)

    # 收尾：清掉自己的 PID（保留 stop.flag 讓主服務也退出）
    try:
        if read_pid(GUARDIAN_PID) == os.getpid():
            os.remove(GUARDIAN_PID)
    except OSError:
        pass
    try:
        os.remove(GUARDIAN_HB)
    except OSError:
        pass
    log("守護程式結束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
