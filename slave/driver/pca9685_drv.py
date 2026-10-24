"""
pca9685_drv.py — PCA9685 PWM pixel 管理 (走 I2C)

設定來源: bus.shared["PCA9685"]  ({enable, list})
         list item: {"i2c": <i2c_list index>, "address": ["0x40"]}
產物:    bus.register_service("pca9685_list", [...])  ← 串流播放用（PixelController）
         bus.register_service("pca9685_ctl",  [...])   ← UI 手動控制用（原始 PCA9685）

兩個產物為什麼分開
------------------
`pca9685_list` 是 **播放端** 的 controller 清單：PixelStreamer 每幀把它們全部
轉換＋輸出一次。它只能有「一顆控制器對應一次實體寫入」，所以：
  - address=["0xFF"] 時只建 **一個** 0x70 廣播 controller（不要每顆板子各建一個，
    否則每幀會先廣播一次、再逐板寫一次，I2C 流量直接翻倍）。
  - address=["0x40"] 時只建那一顆。

`pca9685_ctl` 是 **UI 控制端** 的清單：一個「控制目標」= 一個 record，裡面是
實際的 PCA9685 物件（`pca.buf[ch] = value; pca.show()`）。它和播放端分開，因為
UI 要能「選廣播」也能「選單一板」，而播放端只需要其中一種：
  - 每個實體位址一個 record（單板控制）
  - 每條 bus 一個 0x70 廣播 record（`broadcast=True`，`boards` = 該 bus 的實體板）

★ **控制表的 PCA9685 物件不共用播放端的 controller**（2026-10 定案）：
  兩邊各有自己的 `_buf` / `reg_buf`。共用的話，UI 在另一核按一下、播放端同時
  送一幀，`show()` 的 `reg_buf` 會互相蓋掉（I2C 交易層也無法原子化整個
  prepare+write）。分開之後，最壞只是「先寫的內容被後寫的蓋掉」——
  燈光 last-write-wins，但不會送出半張錯的 64-byte 幀。

record 欄位:
    {"i2c": 0, "addr": 0x40, "pca": <PCA9685>, "broadcast": False,
     "boards": [], "online": True}
    {"i2c": 0, "addr": 0x70, "pca": <PCA9685>, "broadcast": True,
     "boards": [0x40, 0x41], "online": True}

address 特殊值:
  "0xFF" (255) — 真·廣播 (LED All Call, datasheet Fig.25):
                找實體位址(排除 0x70) → 逐一對實體位址喚醒/開 ALLCALL →
                建立「單一」0x70 廣播 controller。每幀對 0x70 寫一筆 =
                所有板子同步收到相同 16 通道 (12-bit)。
  "0x70" (112) — 同上, 但不掃描、不逐一喚醒, 直接以 0x70 建立廣播 controller。
                僅當確定線上晶片 ALLCALL 已開啟時使用。

★ 位址不自己掃：讀 i2c_drv 在開機時掃好的 bus.shared["_i2c_devices"][i2c_idx]。
  這樣同一條匯流排只掃一次，而且 UI 看到的清單＝PCA 用的清單。
★ 掃描後熱插拔：`pixel_control_panel` 消費 `_pca_actions` 的 `scan` 後會呼叫
  `refresh_pca_ctl()`，把 `pca9685_ctl` 依新的 `_i2c_devices` 增減（播放端的
  controller 仍不重建，見 todo/10_i2c_scan.md 的已知限制）。
"""
from lib.sys.log_service import get_log
from lib.hw.pca9685 import PCA9685
from lib.sys.sys_bus import bus


# PCA9685 ALLCALL 廣播位址 (112 = 0x70)。所有 writeto_mem 寫到 0x70 都是
# 廣播: 匯流排上所有啟用 ALLCALL 的 PCA9685 同時收到。因此 0x70 不該當
# 一般裝置註冊 — scan 掃到要排除, 只有明確要用「單一廣播」才註冊它。
_PCA_ALL_CALL = 0x70
# config address 清單中的魔法值: 自動掃描註冊匯流排上全部 PCA9685。
_PCA_AUTO_SCAN = 0xFF

# ── UI 控制表 ──────────────────────────────────────────────────────
# ★ 這個 list **物件本身**是服務內容。refresh 時用 `_CTL[:] = new` 原地換內容，
#   這樣另一核（LVGL）拿到的引用永遠是同一個，不會讀到「舊清單還在服務上」。
# ★ `_CFG` 是 normalize 後的 config（(i2c_idx, item, addrs)），refresh 需要它。
_CTL = []
_CFG = []


def _found_on_bus(sysbus, i2c_idx):
    """該匯流排上掃到的位址（**含 0x70**）。快取不存在 → []。"""
    devs = sysbus.shared.get("_i2c_devices") or {}
    return list(devs.get(i2c_idx) or [])


def _norm_addr(a):
    """config 的位址可能是 "0x40"（字串）或 64（int）。"""
    if isinstance(a, str):
        return int(a, 16)
    return int(a)


def _ctl_entry(pca, i2c_idx, addr, broadcast=False, boards=None, online=True):
    return {
        "i2c": i2c_idx,
        "addr": addr,
        "pca": pca,
        "broadcast": bool(broadcast),
        "boards": list(boards or []),
        "online": bool(online),
    }


def _add_ctl(ctl, pca, i2c_idx, addr, broadcast=False, boards=None, online=True):
    """加入控制表；同一 (bus, addr) 已存在就更新（不重複建 PCA 物件）。"""
    for r in ctl:
        if r["i2c"] == i2c_idx and r["addr"] == addr:
            if pca is not None:
                r["pca"] = pca
            r["broadcast"] = bool(broadcast)
            if boards is not None:
                r["boards"] = list(boards)
            r["online"] = bool(online)
            return r
    r = _ctl_entry(pca, i2c_idx, addr, broadcast, boards, online)
    ctl.append(r)
    return r


def _make_raw(i2c, addr):
    """建立一個「只給 UI 控制用」的原始 PCA9685（不進播放端 pixel_list）。"""
    try:
        pca = PCA9685(i2c, address=addr)
        pca.freq(1000)
        return pca
    except Exception as e:
        get_log().error("PCA9685@{} init error: {}".format(hex(addr), e))
        return None


def _make_controller(pca, item):
    """用 PCA9685 實體建立 PixelController (與串流渲染格式一致)。"""
    from lib.sw.PixelController import PixelController
    return PixelController("i2c_pixel", {
        "pixel_IO": pca,
        "Q": 16,
        "order": "W",
        "dStay": item.get("dStay", 0),
    })


def _register_pca(i2c, addr, item, pca_list):
    """建立單一 PCA9685(位址 addr)並加入 pca_list。回傳 PixelController/None。

    addr == 0x70 → ALLCALL 廣播位址 (broadcast controller): 每幀對 0x70 寫一筆
    = 所有啟用 ALLCALL 的板子同時收到。其餘位址為一般單一裝置, 個別寫入。
    """
    try:
        pca = PCA9685(i2c, address=addr)
        pca.freq(1000)
    except Exception as e:
        get_log().error("PCA9685@{} init error: {}".format(hex(addr), e))
        return None
    ctrl = _make_controller(pca, item)
    pca_list.append(ctrl)
    return ctrl


def _scan_and_register(i2c, i2c_idx, item, pca_list, sysbus):
    """0xFF: 真·廣播 (LED All Call, datasheet Fig.25)。

    掃描找到實體位址 (排除 0x70 自身) 後:
      1. 逐一對「每個實體位址」初始化 (PCA9685.freq 保留 ALLCALL 位元), 把
         晶片從 Sleep 喚醒、開 auto-increment、並確保 ALLCALL=1。
      2. 建立「單一」0x70 廣播 controller。每幀 show() 對 0x70 寫一筆 64
         bytes, 所有板子同時收到並各自寫入自己的 LED0~LED15 (16 通道 / 12-bit
         完全獨立, 等同控制一顆晶片; 只是每顆板子內容相同)。
      3. 另外為每個實體位址各建一個單獨 controller（0x70 廣播 + 0x40/0x41
         單板並存），讓 PixelLab 可以依 target 選 0x70 或單一板，不必改
         0x70 的 address。

    廣播要生效的關鍵 (社群踩坑): 每個晶片的 ALLCALL(MODE1 bit0) 都必須是 1,
    但重刷韌體通常不斷電, 舊程式若寫過 MODE1=0x00/0x10 就會把它關掉。因此
    這裡一定要「先逐一對實體位址寫入重設」, 之後 0x70 才會被 ACK; 若直接對
    0x70 初始化, 在 ALLCALL 已被關掉的板子上會靜默失敗 (ENODEV / 不亮燈)。

    線上完全沒板子時不建 0x70, 避免每幀對不存在的位址刷 ENODEV。

    回傳 (0x70 controller / None, {實體位址: 喚醒用的 PCA9685})。
    第二個回傳值讓 `init_pca9685` 的 UI 控制表**重用**這些物件，不再多寫一次 I2C。

    ★ 位址來源：`bus.shared["_i2c_devices"][i2c_idx]`（i2c_drv 開機掃好的）。
      快取存的是「匯流排事實」（含 0x70），0x70 的過濾留在這裡做。
    """
    found = [a for a in _found_on_bus(sysbus, i2c_idx) if a != _PCA_ALL_CALL]
    get_log().info("I2C devices (bus {} excl 0x70): {}".format(
        i2c_idx, [hex(a) for a in found]))
    if not found:
        get_log().warn("PCA9685: no physical device found — skip 0x70 broadcast")
        return None, {}

    # 1. 逐一喚醒實體板 (保留 ALLCALL), 不註冊成 controller
    wake = {}
    for addr in found:
        try:
            pca = PCA9685(i2c, address=addr)
            pca.freq(1000)
            wake[addr] = pca
        except Exception as e:
            get_log().error("PCA9685@{} init error: {}".format(hex(addr), e))

    # 2. 單一 0x70 廣播 controller: 寫一次 = 所有板子同步收到
    bcast = _register_pca(i2c, _PCA_ALL_CALL, item, pca_list)
    if bcast is not None:
        get_log().info("PCA9685: 0x70 broadcast controller ({} physical board(s))".format(len(found)))

    # 3. 每個實體位址再各建一個單獨 controller（跟 0x70 廣播並存）。
    #    0x70 是 ALLCALL，不能拿來當某顆板子的 address；要單獨控制 0x40/0x41
    #    時必須有對應的 PixelController，否則只能改 0x70 的 address（會失去廣播）。
    for addr in found:
        pca = wake.get(addr)
        if pca is None:
            continue
        try:
            ctrl = _make_controller(pca, item)
            pca_list.append(ctrl)
        except Exception as e:
            get_log().error("PCA9685@{} controller error: {}".format(hex(addr), e))
    return bcast, wake


def init_pca9685(sysbus=None):
    global _CTL, _CFG
    sysbus = sysbus or bus
    cfg = sysbus.shared.get("PCA9685") or {}
    if not cfg.get("enable"):
        _CFG = []
        _CTL[:] = []
        return []

    i2c_list = sysbus.get_service("i2c_list") or []
    items = []
    for item in cfg.get("list", []):
        i2c_idx = (item.get("GPIO") or {}).get("i2c", 0)
        if i2c_idx < 0 or i2c_idx >= len(i2c_list):
            get_log().error("PCA9685: i2c index {} not found".format(i2c_idx))
            continue
        addrs = [_norm_addr(a) for a in (item.get("address") or [])]
        items.append((i2c_idx, item, addrs))

    pca_list = []
    wake = {}      # (i2c_idx, addr) → 自動掃描時用來喚醒的原始 PCA9685（控制表重用）

    # ── 1. 播放端 controller（行為與舊版完全相同）────────────────────
    for i2c_idx, item, addrs in items:
        i2c = i2c_list[i2c_idx]

        # 沒給 address → 該匯流排上的全部（快取），排除 0x70（舊行為）
        if not addrs:
            found = [a for a in _found_on_bus(sysbus, i2c_idx)
                     if a != _PCA_ALL_CALL]
            get_log().info("I2C devices (bus {}): {}".format(
                i2c_idx, [hex(a) for a in found]))
            for addr in found:
                _register_pca(i2c, addr, item, pca_list)

        has_auto = _PCA_AUTO_SCAN in addrs
        seen = set()
        for addr in addrs:
            if addr in seen:
                continue
            seen.add(addr)
            if addr == _PCA_AUTO_SCAN:
                _bcast, w = _scan_and_register(i2c, i2c_idx, item, pca_list, sysbus)
                for a, p in w.items():
                    wake[(i2c_idx, a)] = p
            elif addr == _PCA_ALL_CALL and has_auto:
                # 0xFF 已經建過 0x70 廣播；同一條 config 又列 0x70 → 不重複
                continue
            else:
                _register_pca(i2c, addr, item, pca_list)

    # ── 2. UI 控制表（新增；不影響播放端）────────────────────────────
    #   auto（0xFF / 沒給 address）→ 掃到的實體板全部可控
    #   明確位址                  → 只控設定的那些
    #   只設 0x70（廣播）         → 實體板仍從掃描結果補（UI 才能選單板）
    ctl = []
    for i2c_idx, item, addrs in items:
        i2c = i2c_list[i2c_idx]
        found = [a for a in _found_on_bus(sysbus, i2c_idx)
                 if a != _PCA_ALL_CALL]
        auto = (not addrs) or (_PCA_AUTO_SCAN in addrs)
        if auto:
            phys = found
        else:
            phys = [a for a in addrs
                    if a not in (_PCA_ALL_CALL, _PCA_AUTO_SCAN)]
            if not phys:
                phys = found

        for addr in phys:
            # 0xFF 模式重用「喚醒時建的」物件（同一顆、不共用播放端）；
            # explicit / 沒給 address / 只設 0x70 模式才另建控制用的物件。
            hw = wake.get((i2c_idx, addr))
            if hw is None:
                hw = _make_raw(i2c, addr)
            if hw is None:
                continue
            _add_ctl(ctl, hw, i2c_idx, addr, online=(addr in found))

        # ★ 保留廣播位址：只要這條 bus 有實體板，UI 就一定有一個 0x70 目標。
        #   自己建一個控制用的 0x70（播放端不一定有；就算有也不共用，見檔頭）。
        if phys or (_PCA_ALL_CALL in addrs):
            hw = _make_raw(i2c, _PCA_ALL_CALL)
            if hw is not None:
                _add_ctl(ctl, hw, i2c_idx, _PCA_ALL_CALL,
                         broadcast=True, boards=phys,
                         online=bool(found))

    sysbus.register_service("pca9685_list", pca_list)
    try:
        names = []
        for c in pca_list:
            a = int(getattr(c.hw, "address", -1))
            names.append("0x{:02X}{}".format(a, "(bcast)" if a == _PCA_ALL_CALL else ""))
        get_log().info("PCA9685: {} controller(s): {}".format(len(pca_list), ", ".join(names)))
    except Exception:
        pass
    _CFG = items
    old = sysbus.get_service("pca9685_ctl")
    if isinstance(old, list):
        old[:] = ctl
        _CTL = old
    else:
        _CTL = ctl
        sysbus.register_service("pca9685_ctl", _CTL)

    get_log().info("PCA9685: {} stream controller(s), {} control target(s)".format(
        len(pca_list), len(ctl)))
    return pca_list


def refresh_pca_ctl(sysbus=None):
    """掃描後同步 UI 控制表（熱插拔用）。回傳控制表。

    ★ 只動 `pca9685_ctl`；播放端的 `pca9685_list` **不重建**（已知限制，見
      todo/10_i2c_scan.md）。所以熱插拔的板子可以手動控制，但不會進自動播放。
    ★ 已存在的 record 重用同一個 PCA 物件 → 手動調過的亮度不會因為掃描被清掉。
    ★ auto 模式：掃不到的板子從清單移除；明確位址模式：保留並標 `online=False`。
    """
    sysbus = sysbus or bus
    if not _CFG:
        return _CTL

    i2c_list = sysbus.get_service("i2c_list") or []
    by_key = {}
    for r in _CTL:
        by_key[(r["i2c"], r["addr"])] = r

    new = []
    added = set()      # (i2c_idx, addr)：同一條 bus 有多個 config item 時去重
    for i2c_idx, item, addrs in _CFG:
        if i2c_idx < 0 or i2c_idx >= len(i2c_list):
            continue
        i2c = i2c_list[i2c_idx]
        found = [a for a in _found_on_bus(sysbus, i2c_idx)
                 if a != _PCA_ALL_CALL]
        auto = (not addrs) or (_PCA_AUTO_SCAN in addrs)
        if auto:
            phys = found
        else:
            phys = [a for a in addrs
                    if a not in (_PCA_ALL_CALL, _PCA_AUTO_SCAN)]
            if not phys:
                phys = found

        phys_now = []
        for addr in phys:
            key = (i2c_idx, addr)
            rec = by_key.get(key)
            if rec is None:
                hw = _make_raw(i2c, addr)
                if hw is None:
                    continue
                rec = _ctl_entry(hw, i2c_idx, addr)
                by_key[key] = rec
            rec["online"] = addr in found
            if key not in added:
                new.append(rec)
                added.add(key)
            if addr not in phys_now:
                phys_now.append(addr)

        if phys_now or (_PCA_ALL_CALL in addrs):
            key = (i2c_idx, _PCA_ALL_CALL)
            brec = by_key.get(key)
            if brec is None:
                hw = _make_raw(i2c, _PCA_ALL_CALL)
                if hw is None:
                    continue
                brec = _ctl_entry(hw, i2c_idx, _PCA_ALL_CALL, broadcast=True)
                by_key[key] = brec
            brec["boards"] = phys_now
            brec["online"] = bool(found)
            if key not in added:
                new.append(brec)
                added.add(key)

    _CTL[:] = new
    get_log().info("PCA9685: control targets -> {}".format(
        ["0x{:02X}".format(r["addr"]) for r in new]))
    return _CTL


def gpios(sysbus=None):
    # PCA9685 走 I2C，無獨立 GPIO
    return {}
