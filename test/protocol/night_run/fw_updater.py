# -*- coding: utf-8 -*-
"""fw_updater.py — Master 板更新 slave 板「固件」（slave 專案檔案）的工具

角色：本檔跑在 Master 板（11201，已部署 slave 專案 + lib/schema）。
      透過 UART GPIO9=TX/8=RX 走 NC4 FILE 協議(0x20xx)，把 Master 上的檔案
      （.py/.json/子目錄，即「固件」）逐一上傳到 slave（11401），並驗證 sha256。

用法（Master 板 REPL）：
    import fw_updater
    fw_updater.scan()                      # 列出 Master 上可更新檔案
    fw_updater.update(slave_id, files)     # 傳到指定 slave（定址）
    fw_updater.update_all(slave_id)        # 全量更新（除 config.json/secrets.db/清單）

定址：
    - slave_id 為 None → 廣播 0xFFFF（單 slave 場景）
    - 否則用 IDENTIFY 確認 slave 在線後，定址傳送

依賴：lib.sys.proto / schema_loader / schema_codec（Master 已部署）
"""

import os
import time
import uhashlib
import ubinascii

from machine import UART
from lib.sys.proto import Proto, StreamParser, ADDR_BROADCAST
from lib.sys.schema_loader import SchemaStore
from lib.sys.schema_codec import SchemaCodec

TX, RX = 9, 8
BAUD = 115200
CHUNK_SIZE = 4096

EXCLUDE = {"config.json", "secrets.db", "manifest.json", ".delta.json",
           ".manifest.json", "boot.py", "main.py"}   # 危險檔跳過（boot/main 可另選）
# 說明：boot.py/main.py 是開機入口，一般整包更新時也包含；此處預設排除以避免
# 更新中斷線。全量更新時可用 include_boot=True。

_store = SchemaStore('/schema')
_store.finalize()
_cc = SchemaCodec(_store)


class Link:
    def __init__(self, baud=BAUD):
        self.uart = UART(1, baud, tx=TX, rx=RX, rxbuf=4096)
        self.parser = StreamParser()
        self._drain()

    def _drain(self):
        b = bytearray(512)
        for _ in range(12):
            try:
                if self.uart.any():
                    self.uart.readinto(b)
                else:
                    break
            except Exception:
                break

    def send(self, cmd, args, addr=ADDR_BROADCAST):
        d = _store.get(cmd)
        payload = _cc.encode(d, args)
        self.uart.write(Proto.pack(cmd, payload, addr=addr))

    def recv(self, timeout_ms=3000):
        t0 = time.ticks_ms()
        mv = bytearray(1024)
        while time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
            try:
                if self.uart.any():
                    n = self.uart.readinto(mv)
                    if n and n > 0:
                        self.parser.feed(mv[:n])
                        r = self.parser.pop_frame()
                        if r is not None:
                            ver, addr, cmd, payload_mv = r
                            pb = bytes(payload_mv)
                            d = _store.get(cmd)
                            vals = _cc.decode(d, pb, _store) if d else {}
                            return cmd, vals, pb
            except Exception:
                pass
            time.sleep_ms(2)
        return None

    def recv_until(self, want_cmds, timeout_ms=4000):
        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
            r = self.recv(300)
            if r is not None and r[0] in want_cmds:
                return r
        return None


def _sha256_file(path):
    h = uhashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(4096)
            if not b:
                break
            h.update(b)
    return h.digest()


def scan(root='/sd'):
    """掃描 Master 上可更新檔案（預設 /sd 資料固件），回 (rel_path, abs_path, size) 清單。

    注意：fs_manager.resolve() 只管理 /sd 和 /ram；系統檔(根目錄 /action 等)無法用
    FILE 域覆蓋。因此本工具掃 /sd 下的資料/固件檔（素材、bin、config 等）。
    """
    out = []
    def walk(d, rel):
        try:
            entries = os.listdir(d)
        except Exception:
            return
        for e in sorted(entries):
            p = d + '/' + e
            r = (rel + '/' + e) if rel else e
            try:
                st = os.stat(p)
            except Exception:
                continue
            if st[0] & 0x4000:      # dir
                walk(p, r)
            else:
                if e in EXCLUDE or e.endswith('.pyc'):
                    continue
                out.append((r, p, st[6]))
    walk(root, '')
    return out


def _identify(L):
    """廣播 IDENTIFY，回 slave 的 cid/slave_id。無回應回 None。"""
    L.send(0x100D, {"reply_cid": 0xFFFF})
    r = L.recv_until([0x100E], 3000)
    if r is None:
        return None
    return r[1]


def update_file(L, addr, rel_path, src_path, file_id):
    """上傳單一檔案，回 True/False。"""
    data = open(src_path, 'rb').read()
    total = len(data)
    sha = _sha256_file(src_path)
    remote = '/' + rel_path

    # 先 QUERY 看 slave 端現有 sha（相同則跳過）
    L.send(0x2005, {"path": remote}, addr=addr)
    r = L.recv_until([0x2006], 3000)
    if r and r[1].get('exists') == 1 and r[1].get('sha256') == sha:
        return True   # 已相同，跳過
    L._drain()                          # 清掉 QUERY 回應殘留

    # BEGIN
    L.send(0x2001, {"file_id": file_id, "total_size": total, "chunk_size": CHUNK_SIZE,
                    "sha256": sha, "path": remote}, addr=addr)
    time.sleep_ms(300)                  # 讓 slave 建立 .tmp 檔（實測必要）

    # CHUNK loop
    off = 0
    while off < total:
        chunk = data[off:off + CHUNK_SIZE]
        L.send(0x2002, {"file_id": file_id, "offset": off, "data": chunk}, addr=addr)
        r = L.recv_until([0x2004, 0x2010], 5000)
        if r is None:
            return False
        if r[0] == 0x2010:
            return False
        off += len(chunk)
        time.sleep_ms(20)

    # END
    L.send(0x2003, {"file_id": file_id}, addr=addr)
    r = L.recv_until([0x2006, 0x2010], 5000)
    if r is None:
        return False
    if r[0] == 0x2010:
        return False
    return r[1].get('sha256') == sha


def update(slave_id=None, files=None, include_boot=False, include_main=False):
    """更新 slave。files=None 則全量（掃描）。slave_id=None 則廣播。"""
    L = Link()
    addr = ADDR_BROADCAST

    # 定址
    if slave_id is not None:
        info = _identify(L)
        if info is None:
            L.uart.deinit()
            return "FAIL: no slave on bus"
        # 用 reply_cid 定址（slave 的 cid）
        addr = info.get('cid', ADDR_BROADCAST)
        print("slave: %s cid=0x%04X" % (info.get('slave_id'), addr))
    else:
        print("slave: broadcast 0xFFFF")

    # 檔案清單
    if files is None:
        files = scan()
    # 過濾 boot/main
    keep = []
    for f in files:
        rel = f[0]
        if rel == 'boot.py' and not include_boot:
            continue
        if rel == 'main.py' and not include_main:
            continue
        keep.append(f)
    files = keep

    if not files:
        L.uart.deinit()
        return "no files to update"

    print("更新 %d 個檔案..." % len(files))
    ok = 0
    fail = []
    for i, (rel, src, size) in enumerate(files):
        fid = (i % 65500) + 1
        r = update_file(L, addr, rel, src, fid)
        status = "OK" if r else "FAIL"
        print("  [%d/%d] %-30s %-6d %s" % (i + 1, len(files), rel, size, status))
        if r:
            ok += 1
        else:
            fail.append(rel)

    L.uart.deinit()
    print("-" * 50)
    print("結果: %d ok, %d fail" % (ok, len(fail)))
    if fail:
        print("失敗檔案:")
        for f in fail:
            print("  -", f)
    return "OK" if not fail else "PARTIAL"


def update_all(slave_id=None, include_boot=True, include_main=True):
    """全量更新（含 boot/main）。"""
    return update(slave_id=slave_id, files=scan(), include_boot=include_boot,
                  include_main=include_main)
