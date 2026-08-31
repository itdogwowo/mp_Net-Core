#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebMaster 一鍵啟動腳本。

自動:
  1. 建立/進入虛擬環境 (.venv)
  2. 安裝/更新 requirements.txt 的模組
  3. 啟動 WebMaster 伺服器
  4. 開啟瀏覽器到 http://127.0.0.1:<port>/

若 venv 建立/安裝失敗（例如平台限制、無 pip），會自動 fallback 到「目前 Python」
（需已裝好 fastapi/uvicorn/websockets），確保仍能啟動。

用法:
    python launch.py            # 預設 port 8000
    python launch.py 9000       # 自訂 port
    python launch.py --no-browser   # 不開瀏覽器
"""
import os
import sys
import time
import subprocess
import threading
import webbrowser

DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(DIR, ".venv")
REQ = os.path.join(DIR, "requirements.txt")
RUN = os.path.join(DIR, "run.py")

if os.name == "nt":
    PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
else:
    PY = os.path.join(VENV_DIR, "bin", "python")

DEP_CHECK = "import fastapi, uvicorn, websockets"


def _has_deps(py):
    """檢查該 python 能否 import fastapi/uvicorn/websockets。"""
    try:
        subprocess.check_call(
            [py, "-c", DEP_CHECK],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _venv_ok():
    try:
        return os.path.exists(PY) and _has_deps(PY)
    except Exception:
        return False


def resolve_python():
    """回傳要用的 python: 優先 venv (完全就緒)，否則 fallback 目前 python。

    回傳 (python_path, used_venv: bool)。
    """
    # 1. venv 已存在且 deps 齊 → 直接用
    if _venv_ok():
        return PY, True

    # 2. venv 不存在 → 嘗試建立
    if not os.path.exists(PY):
        print("[WebMaster] 建立虛擬環境 .venv ...")
        try:
            subprocess.check_call([sys.executable, "-m", "venv", VENV_DIR])
        except Exception as e:
            print("[WebMaster] ⚠️ 建立 venv 失敗，改用目前 Python:", e)
            return sys.executable, False

    # 3. venv 建立成功但 deps 未裝 → 安裝
    if not _has_deps(PY):
        print("[WebMaster] 安裝/更新模組 ...")
        try:
            # 先升級 pip，再裝 requirements；不靜默，讓使用者看到進度
            subprocess.check_call([PY, "-m", "pip", "install", "--upgrade", "pip"],
                                  stdout=subprocess.DEVNULL)
            subprocess.check_call([PY, "-m", "pip", "install", "-r", REQ])
        except Exception as e:
            print("[WebMaster] ⚠️ venv pip 安裝失敗，改用目前 Python:", e)
            return sys.executable, False

    # 4. venv 就緒
    if _has_deps(PY):
        return PY, True

    # 5. 兜底: 目前 python 若有 deps 就用它
    if _has_deps(sys.executable):
        print("[WebMaster] 使用目前 Python（已有 fastapi/uvicorn/websockets）。")
        return sys.executable, False

    print("[WebMaster] ❌ 找不到可用 Python（都沒裝 fastapi/uvicorn/websockets）。")
    print("   可執行: python -m pip install -r requirements.txt")
    return None, False


def main():
    argv = sys.argv[1:]
    open_browser = "--no-browser" not in argv
    port = 8000
    for a in argv:
        if a.isdigit():
            port = int(a)

    py, used_venv = resolve_python()
    if py is None:
        return 1

    url = f"http://127.0.0.1:{port}/"
    mode = "venv" if used_venv else "global"
    print(f"[WebMaster] 用 {mode} Python 啟動伺服器 {url} ...")

    server = subprocess.Popen([py, RUN, str(port)])

    if open_browser:
        def _open():
            time.sleep(2.0)
            try:
                webbrowser.open(url)
            except Exception as e:
                print("[WebMaster] 開瀏覽器失敗:", e)
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.wait()
    except KeyboardInterrupt:
        print("\n[WebMaster] 停止伺服器 ...")
        server.terminate()
        try:
            server.wait(timeout=3)
        except Exception:
            try:
                server.kill()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
