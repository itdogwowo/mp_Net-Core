# -*- coding: utf-8 -*-
"""interactive_master.py — Master 板互動式選單（仿 NetBusMaster 風格）

跑在 Master 板（11201）REPL，透過 UART GPIO9/8 對 slave（11401）做互動操作。
編號選單 + input()，每步操作後回主選單。

用法（Master 板 REPL）：
    import interactive_master
    interactive_master.main()

功能：
    0. 掃描/敲門（確認 slave 在線 + 顯示 ID / baud）
    1. 檔案傳輸（傳到暫存 XD → 驗證 → 交換到正式 → 備份/確認/回滾/刪除）
    2. 查詢 slave 檔案
    3. 刪除 slave 檔案
    4. SPEED 提速（切高速傳檔再還原）
    q. 離開
"""

import time
import uhashlib
import os

import master_agent as ma
import safe_update


def _L():
    if ma._link is None:
        ma.init_link(115200)
    return ma._link


def _clear():
    print("\n" * 2)


def _menu():
    _clear()
    print("=" * 56)
    print("  🎬 Master 互動控制台 (UART -> slave)")
    print("=" * 56)
    print("  0. 敲門/狀態     | 確認 slave 在線、ID、baud")
    print("  1. 檔案傳輸      | 傳暫存→驗證→交換→備份/確認/回滾")
    print("  2. 固件更新      | SD → 根目錄正式上線（.bak 備份）")
    print("  3. 查詢檔案      | 查 slave 上某個檔是否存在/大小/sha")
    print("  4. 刪除檔案      | 刪 slave 檔案")
    print("  5. SPEED 提速    | 切高速測傳檔再還原")
    print("  q. 離開")
    print("=" * 56)


def _pick_local_file():
    """互動選一個 Master 本地檔案（列 /sd 下檔案）。"""
    try:
        files = sorted(os.listdir('/sd'))
    except Exception:
        files = []
    files = [f for f in files if not f.startswith('.')]
    if not files:
        print("  /sd 下沒有檔案")
        return None
    print("\n  Master 本地 /sd 檔案:")
    for i, f in enumerate(files):
        try:
            sz = os.stat('/sd/' + f)[6]
        except Exception:
            sz = 0
        print("    %d. %-30s %d B" % (i + 1, f, sz))
    c = input("\n👉 選檔案編號 (或輸入完整路徑): ").strip()
    if c.isdigit():
        idx = int(c) - 1
        if 0 <= idx < len(files):
            return '/sd/' + files[idx]
    return c if c else None


def _do_ping():
    L = _L()
    L.send(0x100D, {"reply_cid": 0xFFFF})
    r = L.recv_until([0x100E], 3000)
    if r is None:
        print("\n  ❌ slave 無回應")
    else:
        d = r[1]
        print("\n  ✅ slave 在線")
        print("     slave_id : %s" % d.get('slave_id', '?'))
        print("     cid      : 0x%04X" % (d.get('cid', 0)))
    L.send(0x1407, {"bus_type": 7, "bus_id": 0})
    r = L.recv_until([0x1408], 2000)
    if r is not None:
        d = r[1]
        print("     speed    : state=%s cur=%s target=%s" % (
            d.get('state'), d.get('cur_speed'), d.get('target_speed')))


def _do_transfer():
    L = _L()
    local = _pick_local_file()
    if not local:
        return
    final = input("👉 正式路徑 (預設 /sd/<同檔名>): ").strip() or ("/sd/" + local.split('/')[-1])

    print("\n  [1/4] 傳到暫存 XD ...")
    ok, msg = safe_update.stage(local)
    print("    %s" % msg)
    if not ok:
        return

    print("  [2/4] 驗證暫存 sha ...")
    ok, msg = safe_update.verify_stage(local)
    print("    %s" % msg)
    if not ok:
        print("  ⚠️ 驗證失敗，不交換到正式路徑")
        return

    print("  [3/4] 交換到正式路徑（覆蓋，留 .bak 備份）...")
    ok, msg = safe_update.apply(local, final)
    print("    %s" % msg)
    if not ok:
        return

    c = input("\n  [4/4] 確認(c) / 回滾(u) / 保留待定(其他): ").strip().lower()
    if c == 'c':
        ok, msg = safe_update.confirm(final)
        print("    %s" % msg)
    elif c == 'u':
        ok, msg = safe_update.undo(final)
        print("    %s" % msg)
    else:
        print("    已保留 pending，稍後可用 confirm/undo")


def _do_query():
    L = _L()
    path = input("👉 查詢路徑 (e.g. /sd/final.bin): ").strip()
    if not path:
        return
    L.send(0x2005, {"path": path})
    r = L.recv_until([0x2006], 3000)
    if r is None:
        print("\n  ❌ 無回應")
        return
    d = r[1]
    print("\n  ✅ 查詢結果:")
    print("     path   : %s" % d.get('path'))
    print("     exists : %s" % d.get('exists'))
    print("     size   : %s" % d.get('size'))
    print("     pending: %s" % d.get('pending'))
    print("     free   : %s" % d.get('free'))
    try:
        sha = d.get('sha256', b'')
        import ubinascii
        print("     sha    : %s" % ubinascii.hexlify(sha).decode()[:16])
    except Exception:
        pass


def _do_delete():
    L = _L()
    path = input("👉 刪除路徑 (e.g. /sd/final.bin): ").strip()
    if not path:
        return
    c = input("⚠️ 確認刪除 '%s'? (y/n): " % path).strip().lower()
    if c != 'y':
        print("  已取消")
        return
    L.send(0x2009, {"path": path})
    r = L.recv_until([0x2006], 3000)
    if r is None:
        print("\n  ❌ 無回應")
    else:
        print("\n  ✅ 已刪除 (exists=%s)" % r[1].get('exists'))


def _do_promote():
    """固件更新：先傳到 slave /sd 暫存，驗證後 promote 到根目錄。"""
    L = _L()
    local = _pick_local_file()
    if not local:
        return
    dst = input("👉 根目錄正式路徑 (e.g. /app.py): ").strip()
    if not dst:
        return

    print("\n  [1/3] 傳到 slave /sd 暫存 ...")
    ok, msg = safe_update.stage(local)
    print("    %s" % msg)
    if not ok:
        return

    print("  [2/3] 驗證暫存 sha ...")
    ok, msg = safe_update.verify_stage(local)
    print("    %s" % msg)
    if not ok:
        print("  ⚠️ 驗證失敗，不 promote")
        return

    print("  [3/3] promote 到根目錄（舊檔留 .bak）...")
    stage_path = safe_update.STAGE_PREFIX + local.split('/')[-1]
    ok, msg = safe_update.promote(stage_path, dst)
    print("    %s" % msg)


def _do_list():
    L = _L()
    # 透過 manifest 查 /manifest.json（本地 manifest）或直接列常見路徑
    print("\n  （slave 檔案列表需經 FILE_SCAN / manifest，這裡查 /manifest.json）")
    L.send(0x2007, {"path": "/manifest.json", "offset": 0, "length": 4096})
    r = L.recv_until([0x2002], 3000)
    if r is None:
        print("  ❌ 無 manifest 回應")
        return
    data = bytes(r[1].get('data', b''))
    print("  manifest %d bytes" % len(data))
    try:
        import json
        m = json.loads(data)
        for k in sorted(m.keys()):
            print("    %s  (%s B)" % (k, m[k].get('s', '?')))
    except Exception as e:
        print("  解析失敗: %s" % e)


def _do_speed():
    print("\n  SPEED 提速測試 ...")
    print(ma.t_speed())


def main():
    ma.init_link(115200)
    while True:
        _menu()
        try:
            ch = input("\n👉 請選擇操作: ").strip().lower()
        except EOFError:
            break
        if not ch:
            continue
        if ch == '0':
            _do_ping()
        elif ch == '1':
            _do_transfer()
        elif ch == '2':
            _do_promote()
        elif ch == '3':
            _do_query()
        elif ch == '4':
            _do_delete()
        elif ch == '5':
            _do_speed()
        elif ch == 'q':
            print("\n再見! 👋")
            break
        else:
            print("❌ 無效選擇")
        input("\n按 Enter 返回...")


if __name__ == "__main__":
    main()
