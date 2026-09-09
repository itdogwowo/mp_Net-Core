# main.py
# 雙核心模式啟動器 — 只負責選擇和路由
#
#   taskmanager  → Core_Manager.launcher()
#   worker_engine → Core0.worker_start()
#
# 硬件初始化已由 boot.py 完成

from lib.sys.sys_bus import bus


def main():
    raw = bus.shared.get("System", {}).get("dual_core_mode", 0)

    # 支援字串 ("taskmanager" / "worker_engine") 和數字 (0 / 1)
    if isinstance(raw, int):
        mode = int(raw)
    else:
        s = str(raw).strip().lower()
        mode = 1 if s in ("worker_engine", "1") else 0

    if mode == 1:
        _launch_worker_engine()
    else:
        _launch_taskmanager()


def _launch_taskmanager():
    from Core_Manager import launcher
    launcher()


def _launch_worker_engine():
    from Core0 import worker_start
    worker_start()


if __name__ == "__main__":
    main()
