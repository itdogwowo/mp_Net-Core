# ui/lvgl/page/remote.py — 遙控器頁（橫屏 320×240）
#
# 角色：面板是**遙控器** —— 只發指令，不自己執行（ports/S3/ESP32-S3-Control_Panel_V2/README.md）。
#
# 版面：
#   左欄 節點清單（可捲）—— 「選中 = 操作對象」
#   右欄 身份（cID/MAC/目標）+ 模式表（source / 前幾筆）
#   底列 [掃描][綁定][解除][取得清單][取得細節] + Wi-Fi / ESP-NOW 開關
#
# 資料全部來自既有狀態（**本頁不自己維護任何狀態**）：
#   bus.shared["peers"]       ← PeerRegistry.snapshot()（第一個消費者）
#   bus.shared["node"]        ← ConfigManager.publish_node()（身份/角色/目標）
#   bus.shared["mode_table"]  ← ConfigManager.publish_modes()（儲存模式/清單）
#   bus.get_service("disp")   ← P3 註冊的指令接口（exec_cmd / make_cmd）
#
# 動作 → 全部走指令（沒有直接改狀態、沒有直接碰硬體）：
#   掃描        make_cmd(0x100D) → **廣播**（還不認識任何人，只能對所有人講話）
#   綁定/解除   exec_cmd(0x1016) → 方向確認（handler 負責持久化）
#   取得清單    make_cmd(0x3101) → 定向
#   取得細節    make_cmd(0x3107) → 定向（逐一，節流由接收端處理）
#   晶片開關    exec_cmd(0x1008/0x1301)
#
# ★ 廣播 vs 定向的判準（doc/03_notes/19 §5）：
#     「不認識對方」→ 廣播（掃描）。「已經認識」→ 定向（其餘全部）。
#   掃描是唯一「廣播出去、單播回來」的動作：回覆走 NowBus.write()，
#   也就是射頻層回給**剛剛講話的那個人**，不是回給 NC4 header 裡的 addr。
#   ⚠️ 所以「廣播不回」不等於「廣播沒回覆」—— 這兩件事容易混淆。
#
# ⚠️ 發射路徑（暫定）：本頁直接呼叫 NowBus.broadcast()/write_to()，與既有 task
#    （ControlPanelTask/PixelControlPanelTask）一致。計劃書 §7 P5 的取捨：
#    等 P7 抽出「發送任務」後，改走 Router（見 doc/03_notes/19 §2.6 / §8.3）。
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_BUTTON, ITEM_SWITCH

REFRESH_EVERY = 10       # 每 N 幀刷新一次顯示（run % N）
RECENT_MS = 10000        # 多久內見過算「在線」（只是顯示標記，不代表協定上的在線）

nav = Nav()
scr = None
_peer_list = None
_peer_btns = []
_peer_rows = []          # PeerRegistry.snapshot() 的內容（選中索引對應）
_sel = 0                 # 選中的節點索引
_wifi_sw = _now_sw = None
_lb = {}                 # 顯示用 label 快取
_last = {}               # 內容比對（避免每幀 set_text）
_btn_bind = None


# ══════════════════════════════════════════════════════════════════
#  發射（暫定：直呼 NowBus）
# ══════════════════════════════════════════════════════════════════
def _kv():
    from lib.sys.sys_bus import bus
    return bus


def _ctx():
    """handler 的 ctx。`app` 由 bus 服務取得（app.py 註冊）—— 不能傳 None，
    因為多個 handler（如 on_now_init）會 `if not ctx.get("app"): return`。"""
    b = _kv()
    return {"app": b.get_service("app"), "transport": "ui"}


def _tx(cmd, args, peer=None):
    """產生並發射一個指令。回 True/False。

    peer=None  → 廣播（addr=0xFFFF；用途：掃描這種「不知道對象」的指令）
    peer=dict  → 定向（addr=對方 cid；射頻層先 add_peer 確保通道存在）
                 ★ 「配對 = 建立通道」：定向發射前自動 add_peer（對稱的一邊）。
    """
    b = _kv()
    disp = b.get_service("disp")
    now = b.get_service("NowBus")
    if disp is None:
        print("[remote] 無 disp 服務 → 無法發射")
        return False
    addr = 0xFFFF
    mac = None
    if peer:
        cid = peer.get("cid")
        if cid is not None:
            addr = int(cid) & 0xFFFF
        mac = peer.get("mac")
    frame = disp.make_cmd(cmd, args, addr)
    if frame is None:
        return False
    if now is None:
        print("[remote] 無 NowBus（ESP-NOW 未啟用？）")
        return False
    if mac:
        now.add_peer(mac)                 # 冪等；確保單播送得出去
        return bool(now.write_to(mac, frame))
    return bool(now.broadcast(frame))


def _selected():
    """目前選中的節點（沒有回 None）。"""
    if 0 <= _sel < len(_peer_rows):
        return _peer_rows[_sel]
    return None


def _exec(cmd, args):
    """本地執行一個指令（走 disp.exec_cmd —— 不經 Router）。"""
    b = _kv()
    disp = b.get_service("disp")
    if disp is None:
        return False
    return disp.exec_cmd(cmd, args, _ctx())


# ══════════════════════════════════════════════════════════════════
#  動作（全部走指令）
# ══════════════════════════════════════════════════════════════════
def _do_scan():
    """0x100D IDENTIFY_REQ（廣播）—— 回覆會自動被 PeerRegistry 登記。

    為什麼是**廣播**（而不是先想辦法定向）：掃描的定義就是「我還不認識任何人」。
    ESP-NOW 的位址是 MAC，**無法枚舉** → 沒有「掃一遍位址空間」這種事，
    只能廣播出去、讓願意回話的對端回話。

    為什麼回得來：對端的回覆走 `NowBus.write()` = 「射頻層回給剛剛講話的人」，
    所以是**單播**回本板（不是廣播）。前提是對端在收幀時學到了本板的 MAC
    ——`NowBus.poll()` 的 `learn_peer()`，兩端對稱（doc/03_notes/19 §9.7）。

    副作用（已知且刻意）：`on_identify_req` 會把 `reply_addr` 記成 `master_cid`，
    所以一次廣播掃描 = 射程內**所有**對端都把本板認成 master。
    對「一台遙控器 + 一群執行端」是想要的；若日後要「只認某一台」，
    就不能用廣播掃描，得先靠公告（0x1002）被動認識，再定向敲門。
    """
    b = _kv()
    ok = _tx(0x100D, {"reply_addr": int(getattr(b, "cid", 0xFFFF)) & 0xFFFF})
    print("[remote] 掃描 →", ok)


def _do_bind():
    """把選中的節點綁定為目標（**本板是控制方**）。

    ★ 方向很重要 —— `0x1016 SET_MASTER` 有兩種用法：
        我**發**它（payload = 我的 cid）→ 告訴對方「你的 master 是我」
        我**收**它（payload = 對方 cid）→ 我把對方當上級（action handler 處理，role=slave）
      面板是遙控器 → 這裡走**發**的那一邊。

    三步：
      ① 配對：add_peer(mac) —— 建立通道（對稱的一邊）
      ② 方向：發 SET_MASTER(我的 cid) 告訴對方回覆要回給我
      ③ 本地：把節點記進 @node.targets 並設 active（master_cid 不動 —— 那是「我的上級」）
    """
    b = _kv()
    p = _selected()
    if p is None:
        print("[remote] 沒有選中的節點")
        return
    cid = p.get("cid")
    if cid is None:
        print("[remote] 該節點沒有 cid（等 identify_rsp 補上）")
        return
    # ① 配對：建立通道（射頻層；雙向都要做，這邊做我們這半）
    now = b.get_service("NowBus")
    if now is not None and p.get("mac"):
        now.add_peer(p["mac"])
    # ② 方向：告訴對方「你的 master 是我」
    my_cid = int(getattr(b, "cid", 0xFFFF)) & 0xFFFF
    _tx(0x1016, {"master_cid": my_cid}, p)
    # ③ 本地：記住目標（active = 當前發射對象）
    tgt = {"cid": int(cid) & 0xFFFF, "mac": p.get("mac") or "",
           "name": (p.get("name") or "").strip() or (p.get("slave_id") or "")[-6:],
           "iface": (p.get("ifaces") or [""])[0], "active": True}
    targets = [t for t in (getattr(b, "targets", None) or []) if t.get("cid") != tgt["cid"]]
    for t in targets:
        t["active"] = False
    targets.append(tgt)
    b.targets = targets
    b.role = "master"
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.save_node()
    except Exception as e:
        print("[remote] 綁定持久化失敗:", e)
    print("[remote] 綁定 0x{:04X}（已告知方向 + 記入 targets）".format(tgt["cid"]))


def _do_unbind():
    """從 targets 移除選中的節點；沒有目標了就回到未定。"""
    b = _kv()
    p = _selected()
    cur = list(getattr(b, "targets", None) or [])
    if not cur:
        print("[remote] 目前沒有目標")
        return
    if p is not None and p.get("cid") is not None:
        cid = int(p["cid"]) & 0xFFFF
        new = [t for t in cur if t.get("cid") != cid]
        if len(new) == len(cur):
            # 選中的節點不是目標 → 退而解除「目前 active」那一個（比較符合直覺）
            new = [t for t in cur if not t.get("active")]
            print("[remote] 選中的不是目標 → 解除目前 active")
        cur = new
    else:
        cur = []                       # 沒選中 → 全部清掉
    if cur and not any(t.get("active") for t in cur):
        cur[0]["active"] = True        # 至少留一個 active
    b.targets = cur
    b.role = "master" if cur else None
    try:
        from lib.sys.ConfigManager import cfg_manager
        cfg_manager.save_node()
    except Exception:
        pass
    print("[remote] 解除綁定 → 剩 {} 個目標".format(len(cur)))


def _do_fetch_list():
    """0x3101 MODE_LIST_QUERY（定向給選中的節點）→ 0x3102 寫入模式表。"""
    p = _selected()
    if p is None:
        print("[remote] 沒有選中的節點 → 無法查詢")
        return
    print("[remote] 取得模式清單 →", _tx(0x3101, {"mode_type": 0}, p))


def _do_fetch_details():
    """0x3107 MODE_DETAIL_QUERY（逐一，定向）→ 0x3108 寫入模式表。

    逐一發射；落盤由接收端節流處理（見 ConfigManager.flush_modes）。
    """
    p = _selected()
    if p is None:
        print("[remote] 沒有選中的節點 → 無法查詢")
        return
    b = _kv()
    tbl = b.shared.get("mode_table") or {}
    ids = [e["id"] for e in (tbl.get("entries") or [])]
    if not ids:
        print("[remote] 模式表是空的 → 先取得清單")
        return
    ok = 0
    for mid in ids:
        if _tx(0x3107, {"mode_type": (mid >> 8) & 0xFF, "mode_id": mid & 0xFF}, p):
            ok += 1
    print("[remote] 取得細節 {}/{}".format(ok, len(ids)))


def _toggle_wifi():
    on = not u.sw_get(_wifi_sw)
    u.sw_set(_wifi_sw, on)
    _exec(0x1008, {"wifi_enable": 1 if on else 0})
    print("[remote] Wi-Fi →", "ON" if on else "OFF")


def _toggle_now():
    """ESP-NOW 開關 → `0x1304 NOW_CTRL{action}`（0=查詢 1=開 2=關）。

    與 Wi-Fi 開關（0x1008）刻意長得不一樣，因為底層是兩件事：
      Wi-Fi  : enable 只是一個**授權旗標**，實際連線由 NetworkManager 非同步做
      ESP-NOW: 開/關是**立即的射頻動作**（NowBus.init / deinit），成敗當下就知道

    開不起來最常見的原因不是指令失敗，而是 `Network.ESP_now.enable = 0`
    —— 那是授權，`on_now_ctrl` 會拒絕在未授權時偷偷開（見 now_actions）。
    所以這裡要把開關撥回去，不要讓 UI 顯示一個騙人的 ON。
    """
    want = not u.sw_get(_now_sw)
    u.sw_set(_now_sw, want)
    _exec(0x1304, {"action": 1 if want else 2})
    print("[remote] ESP-NOW →", "ON" if want else "OFF")
    _sync_now_switch()          # 以實際狀態回寫開關（可能與剛才撥的不同）


# ══════════════════════════════════════════════════════════════════
#  建立畫面
# ══════════════════════════════════════════════════════════════════
def _panel(parent, x, y, w, h):
    c = lv.obj(parent)
    c.set_size(w, h)
    c.set_pos(x, y)
    c.set_style_bg_color(u.C(u.SURFACE), 0)
    c.set_style_radius(8, 0)
    c.set_style_border_width(0, 0)
    c.set_style_pad_all(0, 0)
    c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return c


@register(id="remote", title="遙控器", icon="wifi",
          desc="配對·節點·模式", order=0, accent=0x188038)
def build():
    global scr, _peer_list, _peer_btns, _peer_rows, _sel
    global _wifi_sw, _now_sw, _btn_bind, _lb, _last
    nav.reset()
    _sel = 0
    _lb = {}
    _last = {}
    _peer_rows = []

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    u.mk_label(scr, "遙控器", 8, 5, u.TEXT, u.ZH)

    # ── 左欄：節點清單（選中 = 操作對象）──
    lx, lw = 4, 124
    _peer_list, _peer_btns = u.mk_list(scr, lx, 24, lw, 168, ["(尚無節點)"],
                                       font=u.F_NUM_S)
    nav.add(_peer_list, ITEM_LIST, on_change=_on_list_move)

    # ── 右欄上：身份 ──
    rx = lx + lw + 6
    rw = u.W - 4 - rx
    c1 = _panel(scr, rx, 24, rw, 58)
    u.mk_label(c1, "身份", 6, 3, u.TEXT3, u.ZH)
    _lb["cid"] = u.mk_label(c1, "—", 6, 18, u.TEXT, u.F_NUM_S)
    _lb["mac"] = u.mk_label(c1, "—", 6, 32, u.TEXT2, u.F_NUM_S)
    _lb["dst"] = u.mk_label(c1, "—", 6, 45, u.PRIMARY, u.F_NUM_S)

    # ── 右欄下：模式表 ──
    c2 = _panel(scr, rx, 88, rw, 58)
    _lb["msrc"] = u.mk_label(c2, "模式 —", 6, 3, u.TEXT3, u.ZH)
    for i in range(3):
        _lb["m%d" % i] = u.mk_label(c2, "", 6, 18 + i * 13, u.TEXT2, u.F_NUM_S)

    # ── 右欄中：選中節點的細節 ──
    c3 = _panel(scr, rx, 150, rw, 42)
    _lb["sel"] = u.mk_label(c3, "—", 6, 3, u.TEXT, u.F_NUM_S)
    _lb["sel2"] = u.mk_label(c3, "選一個節點", 6, 22, u.TEXT3, u.F_NUM_S)

    # ── 底列：動作按鈕（5 顆）──
    bw, gap, bx = 60, 3, 4
    y = 198
    b1 = u.mk_btn(scr, "掃描", bx, y, bw, 22, "primary")
    nav.add(b1, ITEM_BUTTON, on_change=_do_scan)
    _btn_bind = u.mk_btn(scr, "綁定", bx + (bw + gap), y, bw, 22, "secondary")
    nav.add(_btn_bind, ITEM_BUTTON, on_change=_do_bind)
    b3 = u.mk_btn(scr, "解除", bx + 2 * (bw + gap), y, bw, 22, "secondary")
    nav.add(b3, ITEM_BUTTON, on_change=_do_unbind)
    b4 = u.mk_btn(scr, "取清單", bx + 3 * (bw + gap), y, bw, 22, "secondary")
    nav.add(b4, ITEM_BUTTON, on_change=_do_fetch_list)
    b5 = u.mk_btn(scr, "取細節", bx + 4 * (bw + gap), y, bw, 22, "secondary")
    nav.add(b5, ITEM_BUTTON, on_change=_do_fetch_details)

    # ── 最底：晶片開關 ──
    _wifi_sw = u.mk_switch(scr, 10, 224, on=False)
    nav.add(_wifi_sw, ITEM_SWITCH, on_change=_toggle_wifi)
    u.mk_label(scr, "Wi-Fi", 34, 226, u.TEXT2, u.ZH)
    _now_sw = u.mk_switch(scr, 168, 224, on=False)
    nav.add(_now_sw, ITEM_SWITCH, on_change=_toggle_now)
    u.mk_label(scr, "ESP-NOW", 192, 226, u.TEXT2, u.ZH)

    u.fade_in(_peer_list, dy=5, time_ms=280, delay_ms=40)
    u.fade_in(c1, dy=5, time_ms=280, delay_ms=120)
    u.fade_in(c2, dy=5, time_ms=280, delay_ms=200)

    _refresh_peers(force=True)
    _refresh_info()
    nav.paint()
    return scr


# ══════════════════════════════════════════════════════════════════
#  刷新
# ══════════════════════════════════════════════════════════════════
def _peer_label(r):
    """節點清單的一行。`*` = 最近見過（字型沒有 ●○，用 ASCII 免得變方塊）。"""
    age = r.get("age_ms")
    mark = "*" if (age is not None and age < RECENT_MS) else " "
    cid = r.get("cid")
    cs = "0x{:04X}".format(int(cid) & 0xFFFF) if cid is not None else "-----"
    nm = (r.get("name") or "").strip() or (r.get("slave_id") or "")[-6:]
    return "{} {} {}".format(mark, cs, nm)


def _refresh_peers(force=False):
    """從 PeerRegistry.snapshot() 重建清單（內容有變才動 widget）。"""
    global _peer_rows, _peer_btns, _sel
    b = _kv()
    reg = b.get_service("peers")
    rows = []
    if reg is not None:
        try:
            rows = reg.snapshot()
        except Exception:
            rows = []
    labels = [_peer_label(r) for r in rows] or ["(尚無節點)"]
    if not force and labels == _last.get("peers"):
        _peer_rows = rows
        return
    _last["peers"] = labels
    _peer_rows = rows
    try:
        _peer_list.clean()
    except Exception:
        pass
    _peer_btns = []
    for txt in labels:
        try:
            btn = _peer_list.add_text(txt)
            if u.F_NUM_S:
                btn.set_style_text_font(u.F_NUM_S, 0)
            _peer_btns.append(btn)
        except Exception:
            continue
    _sel = max(0, min(_sel, max(0, len(_peer_btns) - 1)))
    _sync_list()


def _sync_list():
    if _peer_btns:
        u.list_select(_peer_btns, _sel, color=u.PRIMARY)


def _set(key, txt, color=None):
    """只有在文字變了才 set_text（省 LVGL 重繪）。"""
    if _last.get(key) == txt:
        return
    _last[key] = txt
    lb = _lb.get(key)
    if lb is None:
        return
    try:
        lb.set_text(txt)
        if color is not None:
            lb.set_style_text_color(u.C(color), 0)
    except Exception:
        pass


def _refresh_info():
    b = _kv()
    node = b.shared.get("node") or {}
    # 身份
    cid = node.get("cid")
    mac = (node.get("mac") or "")[-12:]
    now = b.get_service("NowBus")
    try:
        ch = now._channel() if now is not None else None
    except Exception:
        ch = None
    _set("cid", "cID {}  {}".format(
        "0x{:04X}".format(int(cid) & 0xFFFF) if cid is not None else "—",
        "ch{}".format(ch) if ch is not None else "no-now"))
    _set("mac", "MAC {} {}".format(mac or "—",
                                   "Wi-Fi" if u.sw_get(_wifi_sw) else ""))
    # 目標：目前 active 的那一筆（targets 是清單；master_cid 是「我的上級」，不同事）
    tgts = node.get("targets") or []
    act = None
    for t in tgts:
        if t.get("active"):
            act = t
            break
    if act is None and tgts:
        act = tgts[0]
    if act is not None:
        _set("dst", "目標 0x{:04X} ({}/{})".format(
            int(act.get("cid") or 0) & 0xFFFF, tgts.index(act) + 1, len(tgts)))
    else:
        _set("dst", "目標 未綁定")
    # 選中節點
    p = _selected()
    if p is None:
        _set("sel", "—")
        _set("sel2", "選一個節點")
    else:
        age = p.get("age_ms")
        _set("sel", "{} {}".format(
            "0x{:04X}".format(int(p["cid"]) & 0xFFFF) if p.get("cid") is not None else "-----",
            (p.get("slave_id") or "")[-6:]))
        _set("sel2", "{}  {}".format(
            "{}ms".format(age) if age is not None else "未見過",
            ",".join(p.get("ifaces") or []) or "-"))
    # 模式表
    tbl = b.shared.get("mode_table") or {}
    src = tbl.get("source") or "—"
    ents = tbl.get("entries") or []
    _set("msrc", "模式 {} ({})".format(src, len(ents)))
    for i in range(3):
        if i < len(ents):
            e = ents[i]
            _set("m%d" % i, "{} {}".format(e.get("hex", ""), e.get("name", "")))
        else:
            _set("m%d" % i, "")

    # 晶片開關：UI 只是顯示器，狀態來源是 config / 服務 → 單向同步
    #   （不這樣做的話，頁面會顯示「上次點擊的狀態」而不是實際狀態）
    try:
        w = bool(int(((b.shared.get("Network") or {}).get("wifi") or {}).get("enable", 0)))
        if u.sw_get(_wifi_sw) != w:
            u.sw_set(_wifi_sw, w)
    except Exception:
        pass
    _sync_now_switch()


def _sync_now_switch():
    """把 ESP-NOW 開關拉回**實際**狀態（單向：服務 → UI）。

    ★ 判準是 `connected`，不是「服務存不存在」。
      `0x1304 NOW_CTRL{action:2}` 關掉之後，`NowBus` 服務**仍然在 bus 上**
      （刻意的，見 now_actions.on_now_ctrl）—— 只把 `connected` 變 False。
      所以用 `now is not None` 判斷的話，關掉之後開關會彈回 ON。
    """
    b = _kv()
    now = b.get_service("NowBus")
    try:
        on = bool(now is not None and now.connected)
    except Exception:
        on = False
    try:
        if u.sw_get(_now_sw) != on:
            u.sw_set(_now_sw, on)
    except Exception:
        pass
    return on


# ══════════════════════════════════════════════════════════════════
#  頁面接口
# ══════════════════════════════════════════════════════════════════
def _on_list_move(dd):
    """清單編輯態：encoder 移動 → 更新選中索引（nav 只把 delta 交給頁面）。"""
    global _sel
    n = len(_peer_btns)
    if n:
        _sel = (_sel + dd) % n
    _sync_list()


def on_enter():
    _refresh_peers(force=True)
    _refresh_info()


def on_leave():
    if nav.is_editing():
        nav.exit()
        _sync_list()


def on_enc(d):
    nav.enc(d)
    if nav.current_kind() == ITEM_LIST:
        _sync_list()


def on_confirm():
    nav.confirm()
    _sync_list()
    return None


def on_exit():
    consumed = nav.exit()
    _sync_list()
    return consumed


def update(run):
    if run % REFRESH_EVERY != 0:
        return
    try:
        _refresh_peers()
        _refresh_info()
    except Exception:
        pass
