#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_delta_upload.py — 本地 USB/REPL delta 上傳工具

每次**重新抓取**設備的 /manifest.json，跟本地 slave/ 逐檔比對 sha256，
只上傳「真正有差異」的檔（base64 走 normal REPL），最後觸發重掃重建 manifest。

與網絡 delta（NetBusMaster）的差別：走 USB serial，不經 WiFi/WS，適合設備
斷網、或 crash/重啟循環時做救援式更新。

用法:
    python -B tools/PC/local_delta_upload.py [port] [--reboot] [--dry-run] [--slave=<dir>]

預設:
    port     = 自動偵測 USB serial（找不到就手動指定，例如 COM21）
    slave    = <repo>/slave
    --dry-run= 只比對列出差異，不上傳
    --reboot = 更新完軟重啟

依賴: pyserial (pip install pyserial)
"""

import base64
import hashlib
import json
import os
import sys
import time

try:
    import serial
except ImportError:
    print("❌ 需要 pyserial: pip install pyserial")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_SLAVE_DIR = os.path.join(PROJECT_ROOT, "slave")

JUNK_DIR_NAMES = {"__pycache__", "__MACOSX", ".Spotlight-V100", ".Trashes",
                  ".fseventsd", "$RECYCLE.BIN", "System Volume Information"}
JUNK_FILE_NAMES = {".DS_Store", "Thumbs.db", "ehthumbs.db", "desktop.ini"}
JUNK_FILE_PREFIXES = ("._", "~$")
JUNK_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".tmp")


def is_junk_dir(name):
    return name in JUNK_DIR_NAMES


def is_junk_name(name):
    if name in JUNK_FILE_NAMES:
        return True
    if name.startswith(JUNK_FILE_PREFIXES):
        return True
    return name.endswith(JUNK_FILE_SUFFIXES)


class SerialREPL:
    """透過 normal REPL (ctrl-B) 與設備溝通。"""

    def __init__(self, port, baud=115200):
        self.s = serial.Serial(port, baud, timeout=0.5)
        self.s.dtr = False
        self.s.rts = False
        time.sleep(0.3)
        self.s.reset_input_buffer()
        self._enter_normal_repl()

    def _enter_normal_repl(self):
        # ctrl-C 中斷執行 + ctrl-B 切 normal REPL（TaskManager 佔著 raw REPL）
        self.s.write(b"\x03\x03\x03")
        time.sleep(0.6)
        self.s.write(b"\x02")
        time.sleep(0.6)
        self.s.reset_input_buffer()

    def exec(self, code, wait=0.25):
        self.s.write((code + "\r\n").encode("utf-8"))
        time.sleep(wait)

    def drain(self, seconds=0.3):
        t0 = time.time()
        out = b""
        while time.time() - t0 < seconds:
            while self.s.in_waiting:
                out += self.s.read(self.s.in_waiting)
            time.sleep(0.02)
        return out

    def read_until(self, marker, timeout=40.0):
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            while self.s.in_waiting:
                buf += self.s.read(self.s.in_waiting)
            if marker in buf:
                return buf
            time.sleep(0.05)
        return buf

    def read_bytes(self, remote_path, timeout=40.0):
        """讀設備檔案 → bytes。用 base64 + chr(64) 組 marker 避開 REPL echo。"""
        self.drain(0.3)
        self.exec("import ubinascii")
        self.exec("_rd=ubinascii.b2a_base64(open(%r,'rb').read()).decode()" % remote_path)
        self.exec("print(chr(64)+'RDS'+chr(64))")   # 輸出 @RDS@
        self.exec("print(_rd)")
        self.exec("print(chr(64)+'RDE'+chr(64))")   # 輸出 @RDE@
        buf = self.read_until(b"@RDE@", timeout=timeout)
        txt = buf.decode("utf-8", "replace")
        if "@RDS@" not in txt or "@RDE@" not in txt:
            raise RuntimeError("read marker missing: %r" % txt[-200:])
        b64 = txt.split("@RDS@", 1)[1].split("@RDE@", 1)[0].strip()
        return base64.b64decode(b64)

    def write_bytes(self, remote_path, data, chunk=400):
        """寫 bytes 到設備檔案（base64 → 暫存 → 解碼，沿用 repl_upload 做法）。"""
        b64 = base64.b64encode(data).decode()
        self.exec("f=open('/_up.b64','wb')")
        for i in range(0, len(b64), chunk):
            c = b64[i:i + chunk]
            self.exec("f.write(%r)" % c, wait=0.02)
        self.exec("f.close()")
        self.exec("import ubinascii")
        self.exec("d=ubinascii.a2b_base64(open('/_up.b64').read())")
        self.exec("open(%r,'wb').write(d)" % remote_path)
        self.exec("import os; os.remove('/_up.b64')")
        self.exec("print('WROTE', len(d))")
        self.drain(0.4)

    def trigger_rescan(self):
        """觸發 root flash 背景重掃重建 manifest（走 FsScanTask）。"""
        self.exec("from lib.sys.fs_manager import fs")
        self.exec("fs.scan_all()")

    def soft_reset(self):
        self.exec("import machine")
        self.exec("machine.reset()")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def collect_local_files(slave_dir):
    """掃本地 slave 目錄 → [(local_path, remote_path), ...]（跳 config.json / 垃圾檔）。"""
    out = []
    for root, dirs, files in os.walk(slave_dir):
        dirs[:] = [d for d in dirs if not is_junk_dir(d)]
        for name in files:
            if name == "config.json" or is_junk_name(name):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, slave_dir).replace("\\", "/")
            out.append((full, "/" + rel))
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(65536), b""):
            h.update(c)
    return h.hexdigest()


def auto_detect_port():
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            desc = (p.description or "") + (p.manufacturer or "")
            if "USB" in desc or "CP210" in desc or "CH340" in desc or "FTDI" in desc or "Serial" in desc:
                return p.device
    except Exception:
        pass
    return None


def main():
    argv = sys.argv[1:]
    port = None
    slave_dir = DEFAULT_SLAVE_DIR
    do_reboot = False
    dry_run = False
    for a in argv:
        if a == "--reboot":
            do_reboot = True
        elif a == "--dry-run":
            dry_run = True
        elif a.startswith("--slave="):
            slave_dir = a.split("=", 1)[1]
        else:
            port = a

    if port is None:
        port = auto_detect_port()
    if port is None:
        print("❌ 找不到 USB serial port，請手動指定: python local_delta_upload.py COM21")
        return 1

    if not os.path.isdir(slave_dir):
        print(f"❌ slave 目錄不存在: {slave_dir}")
        return 1

    print(f"🔌 連線 {port} @ 115200 ...")
    try:
        dev = SerialREPL(port)
    except Exception as e:
        print(f"❌ 連線失敗: {e}")
        return 1

    # 1. 每次全新抓 manifest
    print("📥 抓取 /manifest.json ...")
    try:
        raw = dev.read_bytes("/manifest.json", timeout=40.0)
        manifest = json.loads(raw.decode("utf-8"))
        print(f"   ✅ manifest {len(manifest)} 個條目")
    except Exception as e:
        print(f"   ⚠️ manifest 讀取失敗({e}) → 視為空，全量比對")
        manifest = {}

    # 2. 真正 delta
    local_files = collect_local_files(slave_dir)
    diff = []
    for full, remote in local_files:
        lsha = sha256_file(full)
        ent = manifest.get(remote)
        rsha = None
        if isinstance(ent, dict):
            rsha = ent.get("h")
        if rsha != lsha:
            diff.append((full, remote, lsha, rsha))

    print(f"\n📊 [Delta] 本地 {len(local_files)} 檔 / 需更新 {len(diff)} 檔:")
    for full, remote, lsha, rsha in diff:
        mark = "＋新增" if rsha is None else "≠ 差異"
        print(f"   {mark}  {remote}  (local {lsha[:10]}…  remote {(rsha or '—')[:10]}…)")

    if not diff:
        print("✅ 全部一致，無需上傳")
        dev.close()
        return 0

    if dry_run:
        print("\n[dry-run] 只列出差異，不上傳")
        dev.close()
        return 0

    # 3. 上傳差異檔
    print(f"\n⬆️ 上傳 {len(diff)} 個差異檔 ...")
    for idx, (full, remote, lsha, rsha) in enumerate(diff, 1):
        with open(full, "rb") as f:
            data = f.read()
        print(f"   [{idx}/{len(diff)}] {remote} ({len(data)}B)")
        dev.write_bytes(remote, data)
        time.sleep(0.2)

    # 4. 觸發重掃重建 manifest（root flash 背景掃描）
    print("\n🔄 觸發重掃重建 manifest ...")
    dev.trigger_rescan()
    time.sleep(8)  # 給 Core1 FsScanTask 一點時間重掃

    # 5. 選用: 軟重啟
    if do_reboot:
        print("🔁 軟重啟 ...")
        dev.soft_reset()
        time.sleep(1)

    dev.close()
    print("\n✅ 本地 delta 更新完成")
    if not do_reboot:
        print("   💡 提示: 修改的 .py 模組需重啟才生效（加 --reboot 或手動重啟）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
