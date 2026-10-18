#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upload_pixel_lab.py — 只上傳 PixelLab P1 需要的最少檔案（USB REPL）。

用途：板子用 USB 連著、但 agent 的 shell 看不到 /dev 時，由使用者在自己的
終端機跑這一支。**不會上傳 config.json**（板子的 4016/接腳設定不動）。

用法：
    pip3 install pyserial
    python3 -B tools/PC/upload_pixel_lab.py /dev/cu.usbmodemXXXX --reboot

    # 不指定 port → 自動找第一個 USB serial
    python3 -B tools/PC/upload_pixel_lab.py --reboot

依賴：pyserial（與 tools/PC/local_delta_upload.py 相同）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SLAVE = os.path.join(ROOT, "slave")

# 本次 PixelLab P1 需要的檔案（順序無關；全部相對 slave/）
FILES = [
    "Core_Manager.py",
    "tasks/pixel_task.py",
    "tasks/stream_task.py",
    "tasks/pixel_lab_task.py",
    "tasks/pixel_control_panel.py",
    "driver/pca9685_drv.py",
    "lib/hw/pca9685.py",
    "ui/lvgl/page/__init__.py",
    "ui/lvgl/page/pixel_lab_common.py",
    "ui/lvgl/page/pixel_lab_apa.py",
    "ui/lvgl/page/pixel_lab_rgb.py",
    "ui/lvgl/page/pixel_lab_pca.py",
    "ui/lvgl/src/zh_hant_16.bin",
]


def main():
    try:
        import serial
    except ImportError:
        print("需要 pyserial: pip3 install pyserial")
        return 2

    argv = sys.argv[1:]
    reboot = "--reboot" in argv
    argv = [a for a in argv if a != "--reboot"]
    port = argv[0] if argv else None
    if port is None:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
        # 優先 usbmodem / usbserial
        usb = [p for p in ports if "usbmodem" in p or "usbserial" in p]
        port = (usb or ports or [None])[0]
    if not port:
        print("找不到 USB serial；請指定 port，例如 /dev/cu.usbmodem11301")
        return 2

    sys.path.insert(0, HERE)
    from local_delta_upload import SerialREPL

    print("[upload] port =", port)
    for rel in FILES:
        local = os.path.join(SLAVE, rel)
        if not os.path.isfile(local):
            print("  ! 缺少本地檔:", rel)
            return 3
    total = sum(os.path.getsize(os.path.join(SLAVE, f)) for f in FILES)
    print("[upload] {} files, {:.1f} KB".format(len(FILES), total / 1024.0))

    repl = SerialREPL(port)
    try:
        for rel in FILES:
            local = os.path.join(SLAVE, rel)
            data = open(local, "rb").read()
            remote = "/" + rel
            print("  -> {} ({:.1f} KB)".format(remote, len(data) / 1024.0))
            repl.write_bytes(remote, data)
        print("[upload] 重建 manifest ...")
        repl.trigger_rescan()
        if reboot:
            print("[upload] reboot ...")
            repl.soft_reset()
    finally:
        try:
            repl.s.close()
        except Exception:
            pass
    print("[upload] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
