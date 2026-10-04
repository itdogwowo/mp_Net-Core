# -*- coding: utf-8 -*-
"""firmware_update.py — Master 端「一次過更新所有固件」完整流程

整合定址 + 掃描 + 批次更新 + 重試 + 上傳驗證/備份/運行確認。

定址模型（對應協議 §定址模型）：
  - Master 自己的 cid 從 config `System.cID` 讀（缺了 = 0xFFFF，需手動設）。
  - Master 透過 SET_MASTER(0x1016) 把「自己的 cid」告訴 slave；slave 存進
    bus.master_cid（只存內存、重開機歸零），之後回應定向回 master，不再廣播。

流程（一次過更新所有固件）：
  1. set_master()     告訴 slave「master 是誰」（定向回應）
  2. scan / identify  找到 slave（可指定範圍）
  3. update_all()     掃 Master 的 /sd 下所有檔案 → 逐檔：
        stage(傳到 slave 暫存) → verify(下載驗 sha) → promote(交換上線 + .bak 備份)
  4. 每步都有重試（send_wait retries=10 + BEGIN 自我修復）

用法（Master 板 REPL）：
    import master_agent as ma
    print(ma.init_link(115200))
    import firmware_update as fu
    fu.set_master()                    # 告訴 slave 我是誰
    fu.update_all()                    # 一次過更新 /sd 下所有檔案
"""

import os
import time
import uhashlib

import master_agent as ma
import safe_update as su


def _L():
    return ma._link


def master_cid():
    """Master 自己的 cid（從 config System.cID 讀）。缺了回 0xFFFF。"""
    try:
        import json
        d = json.load(open('/config.json'))
        v = d.get('System', {}).get('cID', '')
        return int(v, 16) & 0xFFFF if v else 0xFFFF
    except Exception:
        return 0xFFFF


def set_master(mcid=None):
    """發 SET_MASTER(0x1016) 告訴 slave「master cid 是多少」。
    mcid=None → 自動用 master_cid() 讀 config。回 (ok, 訊息)。"""
    L = _L()
    if mcid is None:
        mcid = master_cid()
    mcid = int(mcid) & 0xFFFF
    if mcid == 0xFFFF:
        return (False, "master cid 未設（config System.cID 空），無法定向")
    L.send(0x1016, {"master_cid": mcid}, addr=ma.ADDR_BROADCAST)
    # SET_MASTER 無回覆；用 IDENTIFY 驗證 slave 記住了
    time.sleep_ms(100)
    print("  [set_master] 已告知 slave master_cid=0x%04X" % mcid)
    return (True, "set master 0x%04X" % mcid)


def identify(addr=0xFFFF, reply_cid=None):
    """發 IDENTIFY_REQ。reply_cid=None 時自動填 master 自己的 cid。
    回 (cid, slave_id) dict 或 None。"""
    L = _L()
    if reply_cid is None:
        reply_cid = master_cid()
    d = ma._store.get(0x100D)
    payload = ma._cc.encode(d, {"reply_cid": reply_cid & 0xFFFF})
    frame = ma.Proto.pack(0x100D, payload, addr=int(addr) & 0xFFFF)
    L.uart.write(frame)
    L._wait_sent()
    # 收 IDENTIFY_RSP
    import struct
    t0 = time.ticks_ms()
    mv = bytearray(512)
    while time.ticks_diff(time.ticks_ms(), t0) < 800:
        try:
            if L.uart.any():
                n = L.uart.readinto(mv)
                if n and n > 0:
                    L.parser.feed(mv[:n])
                    r = L.parser.pop_frame()
                    if r is not None:
                        _v, a, cmd, pm = r
                        if cmd == 0x100E:
                            pb = bytes(pm)
                            cid = struct.unpack('<H', pb[0:2])[0]
                            off = 2
                            sl = struct.unpack('<H', pb[off:off+2])[0]; off += 2
                            slave_id = pb[off:off+sl].decode('utf-8', 'replace')
                            return {'cid': cid, 'slave_id': slave_id}
        except Exception:
            pass
        time.sleep_ms(2)
    return None


def scan_range(start, end):
    """掃描 [start, end]，逐個 addr 發 IDENTIFY。找到的 slave 順便 set_master。
    回找到的 dict 清單。"""
    start = int(start) & 0xFFFF
    end = int(end) & 0xFFFF
    print("  [掃描] 0x%04X ~ 0x%04X ..." % (start, end))
    found = []
    for a in range(start, end + 1):
        r = identify(a)
        if r is not None:
            found.append(r)
            print("    ✅ 0x%04X -> cid=0x%04X slave_id=%s" % (a, r['cid'], r['slave_id']))
            # 順便告訴它 master 是誰（定向回應）
            set_master()
    if not found:
        print("  ❌ 範圍內無 slave")
    return found


def _scan_sd_files(root='/sd'):
    """掃 Master /sd 下所有檔案（跳過 . 開頭），回 (rel_path, abs_path) 清單。"""
    out = []
    def walk(d, rel):
        try:
            entries = os.listdir(d)
        except Exception:
            return
        for e in sorted(entries):
            if e.startswith('.'):
                continue
            p = d + '/' + e
            r = (rel + '/' + e) if rel else e
            try:
                st = os.stat(p)
            except Exception:
                continue
            if st[0] & 0x4000:      # dir
                walk(p, r)
            else:
                out.append((r, p))
    walk(root, '')
    return out


def update_one(local_path, remote_dst):
    """更新單一檔案：stage → verify → promote。回 (ok, 訊息)。"""
    # 1. stage（傳到 slave 暫存，含重試 + BEGIN 自我修復）
    ok, msg = su.stage(local_path)
    print("    [stage] %s" % msg)
    if not ok:
        return (False, "stage fail: " + msg)

    # 2. verify（下載驗 sha）
    ok, msg = su.verify_stage(local_path)
    print("    [verify] %s" % msg)
    if not ok:
        return (False, "verify fail: " + msg)

    # 3. promote（交換上線 + .bak 備份）
    stage_path = su.STAGE_PREFIX + local_path.split('/')[-1]
    ok, msg = su.promote(stage_path, remote_dst)
    print("    [promote] %s" % msg)
    if not ok:
        return (False, "promote fail: " + msg)

    return (True, "updated %s -> %s" % (local_path, remote_dst))


def update_all(root='/sd', remote_prefix='/', use_speed=True, target_baud=None,
               file_retries=3, inter_file_ms=500):
    """一次過更新 /sd 下所有檔案到 slave（remote_prefix 預設根目錄）。

    流程 = 上傳(stage) → 驗證(verify) → 備份上線(promote)，全程：
      - use_speed    臨時提速（預設開；失敗自動退回 115200 續跑）
      - file_retries 每個檔整檔重試次數（一個檔 stage 掉包會從頭再來）
      - inter_file_ms 檔間「喘口氣」（讓 slave 排空 rxbuf / 收尾寫 flash / GC）

    提速說明（重要）：
      協議本身是 stop-and-wait —— slave「寫完一個 chunk 才回 ACK」，master 收到
      ACK 才發下一包，所以提速不會造成灌爆（這點你的理解是對的）。但掉包發生在
      「收端 drain 飢餓」（slave 的 CircuitTask/BusDecodeTask 被 core0 其他任務
      擠壓，rxbuf 溢位 → 幀 CRC 壞掉被丟），更高 baud 只會讓 rxbuf 塞得更快、
      同一個 stall 掉更多位元組，所以速度本身不是解藥。這裡靠「整檔重試 + 喘口氣」
      遮蓋它；若 460800 掉太多，把 target_baud 降到 230400 通常是甜蜜點。
    回 (成功數, 失敗清單)。
    """
    files = _scan_sd_files(root)
    if not files:
        print("  ⚠️ %s 下沒有檔案" % root)
        return (0, [])

    print("  [update_all] 共 %d 個檔案，一次過更新 ..." % len(files))

    if target_baud is None:
        target_baud = ma.SPEED_BAUD

    speed_on = False
    if use_speed:
        ok, msg = ma.speed_enter(target_baud)
        if ok:
            speed_on = True
            print("  🚀 提速 %d 成功" % target_baud)
        else:
            print("  ⚠️ 提速失敗（%s），改用 %d 續跑" % (msg, ma.DEF_BAUD))

    ok = 0
    fail = []
    try:
        for i, (rel, abs_path) in enumerate(files):
            dst = remote_prefix.rstrip('/') + '/' + rel
            print("  [%d/%d] %s" % (i + 1, len(files), rel))
            done = False
            last_msg = "unexpected"
            for attempt in range(file_retries):
                r, msg = update_one(abs_path, dst)
                if r:
                    ok += 1
                    done = True
                    break
                last_msg = msg
                print("    ⚠️ 第 %d/%d 次失敗: %s" % (attempt + 1, file_retries, msg))
                if attempt < file_retries - 1:
                    _breathe(inter_file_ms)
            if not done:
                fail.append((rel, last_msg))
                print("    ❌ %s" % last_msg)
            elif inter_file_ms > 0:
                _breathe(inter_file_ms)
    finally:
        if speed_on:
            r, msg = ma.speed_revert()
            print("  ↩️ 還原速度: %s" % msg)

    print("-" * 56)
    print("  結果: %d ok, %d fail" % (ok, len(fail)))
    if fail:
        print("  失敗清單:")
        for rel, msg in fail:
            print("    - %s: %s" % (rel, msg))
    return (ok, fail)


def _breathe(ms):
    """檔間喘口氣：等 slave 收尾，並清掉 master 端可能殘留的半幀。"""
    time.sleep_ms(int(ms))
    L = ma._link
    if L is not None:
        try:
            L._drain()
        except Exception:
            pass
