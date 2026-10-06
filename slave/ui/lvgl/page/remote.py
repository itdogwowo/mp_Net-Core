# ui/lvgl/page/remote.py — 遙控器頁（橫屏 320×240）
#
# 角色：面板是**遙控器** —— 只發指令，不自己執行（ports/S3/ESP32-S3-Control_Panel_V2/README.md）。
#
# 版面：
#   左欄 節點清單（可捲）—— 「選中 = 操作對象」
#   右欄 身份（cID/MAC/目標）+ 模式表（source / 前幾筆）+ 選中節點細節
#   底列 [掃描][綁定][解除][取得清單][取得細節]
#
# ★ 2026-10（使用者定案）：**晶片開關已從本頁移除**。
#   本頁是**應用層**（我要操作誰、播什麼），開關是**傳輸層**（射頻怎麼出去），
#   兩層的數量限制與持久化位置都不同，混在一頁會讓「這清單在限制什麼」說不清：
#     Wi-Fi 開關    → `settings.py`（系統設定）已經有一顆，本頁那份是**重複的**
#     ESP-NOW 開關  → 搬到新頁 `now_setting.py`（傳輸層設定）
#   舊版把兩顆開關擺在 (10,224) / (168,224)、標籤擺在 x=34 / 192 ——
#   但 `mk_switch` 是 **44×24**，所以標籤正好**壓在開關上面**；
#   而且 y=224+24=248 早就超出 240 高的螢幕，下緣被切掉。
#   移除後底列空出來的 42px 還給了左欄清單與 c3 面板。
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
# ★ 發射路徑（2026-10 定案）：**本頁不碰實體管子、不碰 MAC**。
#   兩個入口要分流（todo/05_node_pairing.md §3.1 / doc/19 §2.5）：
#     ① 內部執行 → `disp.exec_cmd(...)`（不經 Router，保證不外送）
#     ② 對外發出 → 產生幀（含 addr）→ `vbus.inject(frame)` → Router 決定走哪條管子
#   舊版直接 `now.add_peer(mac)` / `now.write_to(mac, frame)` / `now.broadcast(frame)`
#   —— 那是把 ESP-NOW 的實作細節寫進 UI，換一條管子就不能用（已移除）。
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
# 本頁已無開關 → 不需要 ITEM_SWITCH。
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_BUTTON

REFRESH_EVERY = 10       # 每 N 幀刷新一次顯示（run % N）
SCAN_SPREAD_MS = 500     # 掃描的抖動視窗（0 = 不抖動）。見 _do_scan
RECENT_MS = 10000        # 多久內見過算「在線」（只是顯示標記，不代表協定上的在線）

nav = Nav()
scr = None
_peer_list = None
_peer_btns = []
_peer_rows = []          # PeerRegistry.snapshot() 的內容（選中索引對應）
_sel = 0                 # 選中的節點索引
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


def _vbus():
    """取得 vBus（本機發起的注入點）。取不到回 None。

    實作在 `lib/sys/sys_bus.get_vbus()` —— 讓所有 UI 頁 / task 共用同一個查找順序。
    """
    from lib.sys.sys_bus import get_vbus
    return get_vbus()


def _tx(cmd, args, peer=None):
    """產生一個指令並**本機發起**（注入 vBus → 走 Router）。回 True/False。

    ★ **本頁不碰實體管子、不碰 MAC**（`todo/05_node_pairing.md` §3.1 / D14/D16）：
        UI 只做兩件事 —— 產生指令（含 `addr`）丟進 vBus；其餘由 Router 決定
        「執行 or 發射、走哪條管子」，由**管子自己**解析 `addr` 成實體位址。
      舊版直接呼叫 `now.add_peer(mac)` / `now.write_to(mac, frame)` /
      `now.broadcast(frame)` —— 那是把 ESP-NOW 的實作細節寫進 UI，換一條管子
      就不能用（`todo/05` §3.1 記載的現況違反）。

    peer=None  → addr = 0xFFFF（廣播；用途：掃描這種「不知道對象」的指令）
    peer=dict  → addr = 對方 cid（定向）

    ⚠️ 走 Router 的前提是 `config.json` 的 `Router.routes` 有
       `{ "in": "self", "out": ["now"] }`（面板 config 已經有了）。
       Router `enable=0` 時 `gate()` 直接回 V_OK → 幀仍然照原路徑進來解碼，
       所以**不轉發**（＝發不出去）—— 這一點由 §12 真機清單第 7 項驗證。
    """
    b = _kv()
    disp = b.get_service("disp")
    if disp is None:
        print("[remote] 無 disp 服務 → 無法發射")
        return False
    addr = 0xFFFF
    if peer:
        cid = peer.get("cid")
        if cid is not None:
            addr = int(cid) & 0xFFFF
    frame = disp.make_cmd(cmd, args, addr)
    if frame is None:
        return False
    vb = _vbus()
    if vb is None:
        print("[remote] 無 vBus → 無法發射")
        return False
    # make_cmd 回的是共享 buffer 的 view（下一次 pack 就覆蓋）→ inject 立即消費 ✓
    return bool(vb.inject(frame))


def _selected():
    """目前選中的節點（沒有回 None）。"""
    if 0 <= _sel < len(_peer_rows):
        return _peer_rows[_sel]
    return None


def _exec(cmd, args):
    """**內部執行**一個指令（走 disp.exec_cmd —— 不經 Router、保證不外送）。

    ★ 這是「兩個入口」的另一半（`todo/05` §3.1）：
        `_exec()` = 「我呼叫一個函式」（args 已是 dict，不經編解碼）
        `_tx()`   = 「我發起一幀」（經 Router，可能被轉發出去）
      `app.py` header 明說「兩者場合不同，不要混用」。
    """
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

    副作用：**沒有**（2026-10 修正）。掃描只問「你是誰」，**不改方向**。
    以前 `on_identify_req` 會把 `reply_cid` 記成 `master_cid`，所以一次廣播
    掃描 = 射程內**所有**對端都把本板認成 master（無聲搶奪：第二台遙控器
    只要按一下掃描就搶走全部，第一台完全不知道）。那個寫入已移除 ——
    方向只由「綁定」的 `0x1016` 決定（見 net_actions.on_identify_req）。

    ★ `timeout_ms = SCAN_SPREAD_MS`（2026-10 新增）：**抖動視窗**。
      廣播點名會讓射程內所有對端在**同一瞬間**回話 —— 在 ESP-NOW 空中／
      RS485 匯流排上是**硬碰撞**，會整批掉。帶上這個值，各對端會各自隨機
      延遲 `[0, timeout_ms]` 才回，把「同時」攤成「序列」。
      **`0` 或缺席 = 不抖動**（舊對端的行為）→ 追加欄位，向後相容。
      真的被撞掉的節點：**再按一次掃描**即可（不做自動重試）。
    """
    b = _kv()
    ok = _tx(0x100D, {"reply_cid": int(getattr(b, "cid", 0xFFFF)) & 0xFFFF,
                      "timeout_ms": SCAN_SPREAD_MS})
    print("[remote] 掃描 →", ok)


def _do_bind():
    """把選中的節點綁定為目標（**本板是控制方**）。

    ★ 方向很重要 —— `0x1016 SET_MASTER` 有兩種用法：
        我**發**它（payload = 我的 cid）→ 告訴對方「你的 master 是我」
        我**收**它（payload = 對方 cid）→ 我把對方當上級（action handler 處理，role=slave）
      面板是遙控器 → 這裡走**發**的那一邊。

    兩步（2026-10 起；通道建立已自動化）：
      ① 方向：發 SET_MASTER(我的 cid) 告訴對方回覆要回給我
      ② 本地：把節點記進 @node.targets 並設 active（master_cid 不動 —— 那是「我的上級」）

    ★ 原本還有一步「配對：`now.add_peer(p["mac"])` 建立通道」——
      那是**傳輸層的實作細節**（ESP-NOW 才需要 add_peer），已從 UI 移除：
      幀走 vBus → Router → 射頻管，由 `NowBus` **自己**在送出前確保通道
      （`todo/05` §3.1 / D14：MAC 不得出現在協議層或 UI）。
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
    # ① 方向：告訴對方「你的 master 是我」
    my_cid = int(getattr(b, "cid", 0xFFFF)) & 0xFFFF
    _tx(0x1016, {"master_cid": my_cid}, p)
    # ③ 本地：記住目標（active = 當前發射對象）
    #   ★ **只存 cid**（2026-10）：cid 是協議層的定址鍵（發射時填幀頭 addr），
    #     射頻層的 MAC 由 `PeerRegistry.by_cid()` 查 —— **單一事實**，
    #     與 `sys_bus.py:31` / `ConfigManager.py:380` 原本就寫明的設計一致
    #     （那兩處說「目標的 MAC 不重複存」，但程式碼自己存了一份 = 漂移）。
    #     額外好處：cid 撞號時不會有兩份互相矛盾的 MAC。
    tgt = {"cid": int(cid) & 0xFFFF,
           "name": (p.get("name") or "").strip() or (p.get("slave_id") or "")[-6:],
           "active": True}
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
    """從 targets 移除選中的節點；沒有目標了就回到未定。

    ★ **雙邊一致（2026-10）**：配對是**兩邊的狀態** —— 本機的 `@node.targets`
      與對方的 `master_cid`。只改本機的話，對方會永遠把回應送給一台已經不要它
      的主控（`todo/04_remote_pairing.md` 的 G1）。
      所以移除前先對每一台被解除的對象發 `0x1016{master_cid: 0xFFFF}` = **解除**
      （見 `net_actions.on_set_master`：解除會清對方的 master_cid/master_mac/role
        並**重新開放認領**，所以它不必等重開機就能被新主控接手）。
    """
    b = _kv()
    p = _selected()
    old_targets = list(getattr(b, "targets", None) or [])
    cur = list(old_targets)
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

    # ① 先通知每一個「被解除」的對方（用 cid 集合比對 —— 不要用 `t not in cur`，
    #    dict 的 `in` 是逐值相等，兩個 target 內容剛好相同時會漏判；cid 才是身分）
    kept = {int(t.get("cid") or 0) & 0xFFFF for t in cur}
    removed = [t for t in old_targets
               if (int(t.get("cid") or 0) & 0xFFFF) not in kept]
    for t in removed:
        ok = _tx(0x1016, {"master_cid": 0xFFFF}, t)
        print("[remote] 解除通知 0x{:04X} → {}".format(
            int(t.get("cid") or 0) & 0xFFFF, ok))

    # ② 再改本機
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
    global _btn_bind, _lb, _last
    nav.reset()
    _sel = 0
    _lb = {}
    _last = {}
    _peer_rows = []

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    u.mk_label(scr, "遙控器", 8, 5, u.TEXT, u.ZH)

    # ── 左欄：節點清單（選中 = 操作對象）──
    #   h 從 168 → 180：晶片開關搬走後，底列讓出來的空間還給清單。
    lx, lw = 4, 124
    _peer_list, _peer_btns = u.mk_list(scr, lx, 24, lw, 180, ["(尚無節點)"],
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
    #   h 從 42 → 58：容得下「最後 N ms 前」這種有單位、有標籤的完整句子。
    c3 = _panel(scr, rx, 150, rw, 58)
    _lb["sel"] = u.mk_label(c3, "—", 6, 3, u.TEXT, u.F_NUM_S)
    _lb["sel2"] = u.mk_label(c3, "選一個節點", 6, 22, u.TEXT3, u.F_NUM_S)
    _lb["sel3"] = u.mk_label(c3, "—", 6, 38, u.TEXT3, u.F_NUM_S)

    # ── 底列：動作按鈕（5 顆）──
    #   y 從 198 → 210：晶片開關（原本 224 起、還被切掉 8px）移除後往下挪，
    #   與加高後的左欄清單（24..204）留 6px 間距。
    bw, gap, bx = 60, 3, 4
    y = 210
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

    # ★ 這裡原本有兩顆晶片開關（Wi-Fi / ESP-NOW）。
    #   已移除：Wi-Fi 那份與 `settings.py` 重複；ESP-NOW 搬到 `now_setting.py`。
    #   原因與當年的排版 bug 記在本檔開頭的檔頭註解。

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
    #   ★ 晶片狀態放這一行（本頁只顯示、不控制）：`ch6` = ESP-NOW 開著且在 ch6。
    #     關掉時顯示 `ESP-NOW OFF`（開關在 now_setting.py；Wi-Fi 在 settings.py）。
    _set("cid", "cID {}  {}".format(
        "0x{:04X}".format(int(cid) & 0xFFFF) if cid is not None else "—",
        "ch{}".format(ch) if (_now_is_on() and ch is not None) else "ESP-NOW OFF"))
    # ★ Wi-Fi 是否啟用：本頁**只讀不寫**（開關已搬到 settings.py）。
    #   來源與 settings.py 相同 —— bus.shared["Network"]["wifi"]["enable"]。
    try:
        wifi_on = bool(int(((b.shared.get("Network") or {}).get("wifi") or {})
                           .get("enable", 0)))
    except Exception:
        wifi_on = False
    _set("mac", "MAC {}  {}".format(mac or "—", "Wi-Fi ON" if wifi_on else "Wi-Fi OFF"))
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
    #   ★ `age_ms` = 「距離最後一次收到它的時間」，不是延遲、也不是剩餘時間。
    #     舊版把裸數字直接印成 `12345ms`，沒有主詞也沒有方向 —— 看的人無從判斷
    #     那是「多久沒聽到」還是「回應多快」。現在拆成三行、每行都有標籤：
    #       sel  : 0x0002  test-peer      ← cID + slave_id
    #       sel2 : 最後 1234ms 前  ●在線   ← 時間 + 在線判準（RECENT_MS）
    #       sel3 : 總線 now                ← 從哪條管子聽到的
    p = _selected()
    if p is None:
        _set("sel", "—")
        _set("sel2", "選一個節點")
        _set("sel3", "—")
    else:
        age = p.get("age_ms")
        _set("sel", "{}  {}".format(
            "0x{:04X}".format(int(p["cid"]) & 0xFFFF) if p.get("cid") is not None else "-----",
            (p.get("slave_id") or "")[-8:]))
        if age is None:
            _set("sel2", "最後 從未聽過")
        else:
            _set("sel2", "最後 {}ms 前  {}".format(
                int(age), "●在線" if int(age) < RECENT_MS else "○不在線"))
        _set("sel3", "總線 {}".format(",".join(p.get("ifaces") or []) or "-"))
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
    # ★ 晶片開關的單向同步（wifi enable / NowBus.connected）已隨開關一起移除。
    #   那兩顆開關現在住在 `settings.py`（Wi-Fi）與 `now_setting.py`（ESP-NOW），
    #   同步邏輯跟著開關走 —— 留在這裡只會是沒有人呼叫的死碼。


def _now_is_on():
    """ESP-NOW 現在是不是真的開著（給頁面顯示用；**開關本身在 `now_setting.py`**）。

    ★ 判準是 `connected`，不是「服務存不存在」。
      `0x1301 NOW_INIT{action:2}` 關掉之後，`NowBus` 服務**仍然在 bus 上**
      （刻意的，見 now_actions.on_now_init）—— 只把 `connected` 變 False。
      所以用 `now is not None` 判斷的話，關掉之後會誤報成 ON。
    """
    now = _kv().get_service("NowBus")
    try:
        return bool(now is not None and now.connected)
    except Exception:
        return False


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
