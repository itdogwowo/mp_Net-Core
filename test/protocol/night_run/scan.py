# -*- coding: utf-8 -*-
"""scan.py — Master 端「找 ID」完整實作（三層設計，對應用戶需求）

三種模式（都可選）：
  1. broadcast()        廣播一次：addr=0xFFFF，所有 slave 都回應。
                         ⚠️ 單 slave 點對點最快，但多 slave 會「同時回應撞車」。
  2. ping_one(cid)      指定 ID：幀頭 addr = 該 cid，只有 cid 匹配的 slave 收得到。
                         （= 用戶說的「手動設置對面 ID」）
  3. scan_range(a, b)   掃描範圍：從 addr=a 到 addr=b 逐個發 IDENTIFY_REQ，
                         找到就回報。（= 用戶說的「設置搜索範圍」）

定址模型（重要）：
  - 幀頭 `addr` 欄位才是「定址過濾」的依據（slave handle_stream 只收
    addr==0xFFFF(廣播) 或 addr==自己 cid 的幀）。
  - IDENTIFY_REQ 的 payload `reply_cid` 是「回覆定址」：告訴 slave 回覆時
    addr 填多少（非 0xFFFF 時 slave 記住為 master_cid）。

用法（Master 板 REPL）：
    import master_agent as ma
    print(ma.init_link(115200))
    import scan
    scan.broadcast()              # 廣播找（單 slave 用）
    scan.ping_one(0x0CA4)         # 指定 ID（手動設置對面 ID）
    scan.scan_range(0x0C00, 0x0CFF)   # 掃描範圍
"""

import time

import master_agent as ma


def _L():
    if ma._link is None:
        ma.init_link(115200)
    return ma._link


def _decode_identify(pb):
    """手動解 IDENTIFY_RSP payload: cid(u16) + slave_id(str_u16len) + ip(str_u16len)。"""
    import struct
    if len(pb) < 2:
        return {}
    cid = struct.unpack('<H', pb[0:2])[0]
    off = 2
    try:
        sl = struct.unpack('<H', pb[off:off+2])[0]
        off += 2
        slave_id = pb[off:off+sl].decode('utf-8', 'replace')
        off += sl
    except Exception:
        slave_id = ''
    try:
        il = struct.unpack('<H', pb[off:off+2])[0]
        off += 2
        ip = pb[off:off+il].decode('utf-8', 'replace')
    except Exception:
        ip = ''
    return {'cid': cid, 'slave_id': slave_id, 'ip': ip}


def _identify(addr):
    """發 IDENTIFY_REQ（幀頭 addr = 參數），收 IDENTIFY_RSP。回 dict 或 None。"""
    L = _L()
    # 發送：幀頭 addr 用參數；payload reply_cid 用廣播（讓 slave 回覆時廣播，單 master 收得到）
    d = ma._store.get(0x100D)
    payload = ma._cc.encode(d, {"reply_cid": 0xFFFF})
    frame = ma.Proto.pack(0x100D, payload, addr=addr)
    # RS485 半雙工：必須拉高 EN(DE) 才發得出（走 Link.send 的 en 控制邏輯）
    if getattr(L, 'en', None) is not None:
        L.en.value(1)
        time.sleep_ms(2)
    try:
        L.uart.write(frame)
        L._wait_sent()
    finally:
        if getattr(L, 'en', None) is not None:
            L.en.value(0)
    # 收
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
                        _v, a, cmd, payload_mv = r
                        if cmd == 0x100E:
                            return _decode_identify(bytes(payload_mv))
        except Exception:
            pass
        time.sleep_ms(2)
    return None


def broadcast():
    """模式 1：廣播一次，找所有在線 slave。回 dict 或 None。"""
    print("  [廣播] addr=0xFFFF 問一次 ...")
    r = _identify(0xFFFF)
    if r is None:
        print("  ❌ 無 slave 回應")
        return None
    print("  ✅ 找到 slave:")
    _show(r)
    return r


def ping_one(cid):
    """模式 2：手動指定對面 cid，只問這一個。回 dict 或 None。"""
    cid = int(cid) & 0xFFFF
    print("  [指定] addr=0x%04X 問指定 slave ..." % cid)
    r = _identify(cid)
    if r is None:
        print("  ❌ 0x%04X 無回應（可能不在線或 cid 不符）" % cid)
        return None
    print("  ✅ 找到 slave:")
    _show(r)
    return r


def scan_range(start, end):
    """模式 3：在 [start, end] 範圍逐個掃描。回找到的 dict 清單。"""
    start = int(start) & 0xFFFF
    end = int(end) & 0xFFFF
    print("  [掃描範圍] 0x%04X ~ 0x%04X (%d 個位址) ..." % (start, end, end - start + 1))
    found = []
    for a in range(start, end + 1):
        r = _identify(a)
        if r is not None:
            found.append(r)
            print("    ✅ 0x%04X -> cid=0x%04X slave_id=%s" % (a, r['cid'], r['slave_id']))
    if not found:
        print("  ❌ 範圍內無 slave")
    return found


def _identify_fast(addr, wait_ms=80):
    """快速版 _identify：每個位址只等 wait_ms（slave 在線會即回）。"""
    L = _L()
    d = ma._store.get(0x100D)
    payload = ma._cc.encode(d, {"reply_cid": 0xFFFF})
    frame = ma.Proto.pack(0x100D, payload, addr=addr)
    # RS485 半雙工：必須拉高 EN(DE) 才發得出
    if getattr(L, 'en', None) is not None:
        L.en.value(1)
        time.sleep_ms(2)
    try:
        L.uart.write(frame)
        L._wait_sent()
    finally:
        if getattr(L, 'en', None) is not None:
            L.en.value(0)
    t0 = time.ticks_ms()
    mv = bytearray(512)
    while time.ticks_diff(time.ticks_ms(), t0) < wait_ms:
        try:
            if L.uart.any():
                n = L.uart.readinto(mv)
                if n and n > 0:
                    L.parser.feed(mv[:n])
                    r = L.parser.pop_frame()
                    while r is not None:
                        _v, a, cmd, payload_mv = r
                        if cmd == 0x100E:
                            return _decode_identify(bytes(payload_mv))
                        r = L.parser.pop_frame()
        except Exception:
            pass
        time.sleep_ms(1)
    return None


def scan_range_fast(start, end, wait_ms=80):
    """快速掃描：每個位址只等 wait_ms，適合大範圍（256+ 位址）。"""
    start = int(start) & 0xFFFF
    end = int(end) & 0xFFFF
    print("  [快速掃描] 0x%04X ~ 0x%04X (%d 位址, 每址 %dms) ..." % (start, end, end - start + 1, wait_ms))
    found = []
    for a in range(start, end + 1):
        r = _identify_fast(a, wait_ms)
        if r is not None:
            found.append(r)
            print("    ✅ 0x%04X -> cid=0x%04X slave_id=%s" % (a, r['cid'], r['slave_id']))
    if not found:
        print("  ❌ 範圍內無 slave")
    return found


def _show(r):
    print("     cid      : 0x%04X (%d)" % (r['cid'], r['cid']))
    print("     slave_id : %s" % r['slave_id'])
    print("     ip       : %s" % r['ip'])


# 互動入口
def main():
    L = _L()
    print("=" * 56)
    print("  找 ID — 三種模式")
    print("=" * 56)
    print("  1. 廣播一次        (單 slave 最快)")
    print("  2. 指定 ID         (手動設置對面 ID)")
    print("  3. 掃描範圍        (設置搜索範圍)")
    print("  q. 離開")
    while True:
        c = input("\n👉 選擇: ").strip().lower()
        if c == '1':
            broadcast()
        elif c == '2':
            v = input("👉 輸入對面 cid (hex, e.g. 0CA4): ").strip()
            if v:
                try:
                    ping_one(int(v, 16))
                except Exception as e:
                    print("  解析失敗: %s" % e)
        elif c == '3':
            a = input("👉 起始 cid (hex, e.g. 0C00): ").strip()
            b = input("👉 結束 cid (hex, e.g. 0CFF): ").strip()
            try:
                scan_range(int(a, 16), int(b, 16))
            except Exception as e:
                print("  解析失敗: %s" % e)
        elif c == 'q':
            break
        else:
            print("  ❌ 無效選擇")
