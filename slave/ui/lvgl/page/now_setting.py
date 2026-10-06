# ui/lvgl/page/now_setting.py — ESP-NOW 設定頁（**傳輸層**）
#
# 為什麼要獨立一頁（todo/07_now_setting_ui.md §1）
# ══════════════════════════════════════════════════════════════════
# 舊版把「ESP-NOW 開關」跟「節點清單 ＋ 模式表」擠在 `remote.py` 一頁，
# 造成三個說不清楚的地方：
#
#   1. 限制說不清 —— 「已選 3/19」是**射頻**限制（ESP-NOW 硬體 peer 表 20 格），
#      不是「我最多能控 3 台」。UART/WS 上的節點根本不受這個約束。
#   2. FF 語意衝突 —— 射頻層的 FF 是「不指定對象，所有人收得到」；
#      應用層的「全選」是「我挑的每一台都送」。混在一張表必然誤解。
#   3. 持久化位置不同 —— 射頻清單是**這台裝置的無線電設定**（`@now.*`）；
#      遙控器清單是**我要控制誰**（`@node.targets`）。
#
# 所以：**本頁 = 「這顆無線電要對誰講話」**，`remote.py` = 「我要操作誰、播什麼」。
#
# 版面（320×240 橫屏）
# ══════════════════════════════════════════════════════════════════
#   y=5    標題 + 右側狀態訊息
#   y=22   ┌ 啟用 ┐ ┌ 頻道 ─────┐ ┌ 加密 ┐      h=46
#   y=72   「可選 (n)」        「已選 (m/19)」
#   y=88   ┌ 可選清單 ┐        ┌ 已選清單 ┐      h=114
#   y=208  [掃描][加入][移除][套用][清除]
#
# 兩張表（Windows 式，使用者定案）
# ══════════════════════════════════════════════════════════════════
#   可選 = 掃描到的 radio peer（`PeerRegistry.snapshot()`，任何總線上的都算）
#   已選 = 使用者挑的 MAC（`@now.selected`）＋ 永遠墊底的 FF 廣播
#
#   ★ **「套用」是唯一會動硬體 peer 表的按鈕**（`NowBus.apply_peers`）。
#     掃描只累積「可選」、加入/移除只改 UI 狀態 —— 都不偷偷佔格子。
#     刻意的：`poll()` 對每個講過話的 MAC 被動學習（MAC 無法枚舉），
#     那些是雜訊，不該在使用者決定之前就吃掉 20 格裡的位子。
#
#   ★ **滿了不自動淘汰**（使用者定案）：直接拒絕並提示「請先移除一些」。
#     舊版會自動踢掉最舊的 —— 那會讓使用者的清單**無聲地**變動。
#
# FF 與具體節點互斥（使用者定案）
# ══════════════════════════════════════════════════════════════════
#   已選裡有具體節點 → FF 退下；節點全部移除 → FF 自動回來。
#   理由：FF 的字面意義是「不指定對象」。既然已經指定了對象，
#   還留著 FF 只會讓每一次送出都順便吵到全場。
#   FF 不列在「可選」裡 —— 它不是掃到的、也不能被「加入」，它是預設值。
#
# MAC 的顯示格式
# ══════════════════════════════════════════════════════════════════
#   清單寬度只有 124px（可選）／184px（已選），塞不下 `AA:BB:CC:DD:EE:FF`。
#   兩張表**統一**只顯示後 3 bytes（`AA:BB:CC`）—— 同一個裝置在兩張表裡
#   長得一樣，才不會誤以為是兩個裝置。前三 bytes 是 OUI（廠商碼），
#   同批裝置本來就一樣，砍掉不損失辨識度。完整 MAC 在加入/移除時印到 console。
#
# 資料來源（本頁**不自己維護任何狀態**，只有「未套用的 UI 編輯」是本地態）
# ══════════════════════════════════════════════════════════════════
#   bus.shared["peers"]        ← PeerRegistry.snapshot()（可選的來源）
#   bus.shared["now_setting"]  ← ConfigManager.now_state()（已選/加密，@now.*）
#   bus.shared["Network"]      ← ESP_now.enable = **授權旗標**（不是射頻狀態）
#   bus.get_service("NowBus")  ← .connected = 射頻**實際**開著沒
#   bus.get_service("disp")    ← exec_cmd / make_cmd
import lvgl as lv
from ui.lvgl.registry import register
from ui.lvgl import ui_common as u
from ui.lvgl.nav import Nav, ITEM_LIST, ITEM_BUTTON, ITEM_SWITCH

REFRESH_EVERY = 10        # 每 N 幀刷新一次顯示
SCAN_SPREAD_MS = 500      # 掃描抖動視窗（0 = 不抖動）。見 remote._do_scan 的長註解
CH_MIN, CH_MAX = 1, 13    # Wi-Fi 2.4G 的可用頻道（ESP-NOW 跟著 Wi-Fi 走）

BCAST_HEX = "FFFFFFFFFFFF"   # 廣播 MAC：永遠在「已選」墊底、不可加入/移除

nav = Nav()
scr = None
_opt_list = None          # 可選清單 widget
_sel_list = None          # 已選清單 widget
_opt_btns = []
_sel_btns = []
_opt_rows = []            # 可選的 MAC（大寫 hex，無冒號）
_sel_macs = []            # 已選的 MAC（大寫 hex，無冒號；**不含 FF**）
_oi = 0                   # 可選清單的游標
_si = 0                   # 已選清單的游標
_lb = {}
_last = {}
_sw_on = _sw_enc = None
_dirty = False            # 使用者改過「已選」但還沒按套用


# ══════════════════════════════════════════════════════════════════
#  取得服務 / 發指令（與 remote.py 同一套「兩個入口」，不要混用）
# ══════════════════════════════════════════════════════════════════
def _kv():
    from lib.sys.sys_bus import bus
    return bus


def _ctx():
    """handler 的 ctx。`app` 由 bus 服務取得（app.py 註冊）—— 不能傳 None。"""
    b = _kv()
    return {"app": b.get_service("app"), "transport": "ui"}


def _now():
    """NowBus 服務（**關掉之後它仍然在 bus 上**，用 .connected 判斷狀態）。"""
    return _kv().get_service("NowBus")


def _cfg():
    from lib.sys.ConfigManager import cfg_manager
    return cfg_manager


def _exec(cmd, args):
    """**內部執行**（走 disp.exec_cmd —— 不經 Router、保證不外送）。

    「開/關/換頻道」是**本機動作**（改的是自己的無線電），所以走這個入口，
    不是 `_tx()`。`_tx()` 是「我發起一幀給別人」。
    """
    b = _kv()
    disp = b.get_service("disp")
    if disp is None:
        print("[now_set] 無 disp 服務")
        return False
    return disp.exec_cmd(cmd, args, _ctx())


def _tx_bcast(cmd, args):
    """廣播發一幀（走 vBus → Router）。掃描用：我還不認識任何人。"""
    b = _kv()
    disp = b.get_service("disp")
    if disp is None:
        print("[now_set] 無 disp 服務 → 無法發射")
        return False
    frame = disp.make_cmd(cmd, args, 0xFFFF)
    if frame is None:
        return False
    from lib.sys.sys_bus import get_vbus
    vb = get_vbus()
    if vb is None:
        print("[now_set] 無 vBus → 無法發射")
        return False
    # make_cmd 回的是共享 buffer 的 view（下一次 pack 就覆蓋）→ inject 立即消費 ✓
    return bool(vb.inject(frame))


# ══════════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════════
def _short(mac):
    """`AABBCCDDEEFF` → `DD:EE:FF`（只留後 3 bytes，見檔頭「MAC 的顯示格式」）。"""
    m = (mac or "").upper()
    if len(m) < 12:
        return m or "??"
    return "{}:{}:{}".format(m[6:8], m[8:10], m[10:12])


def _full(mac):
    """`AABBCCDDEEFF` → `AA:BB:CC:DD:EE:FF`（印 console 用）。"""
    m = (mac or "").upper()
    if len(m) < 12:
        return m or "??"
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


def _cid_txt(row):
    """peer 的 cid → `0x0002`；還不知道（掃到但沒回過 cid）→ `----`。

    ⚠️ 不能寫成 `row.get("cid") and "0x..." or "----"` —— cid **合法值包含 0**，
    而 0 是 falsy，會讓「cid 真的是 0」被誤顯示成「不知道」。
    """
    cid = row.get("cid")
    if cid is None:
        return "----"
    try:
        return "0x{:04X}".format(int(cid) & 0xFFFF)
    except Exception:
        return "----"


def _cap():
    """目前加密設定下的「已選」上限（不含廣播那一格）。取不到服務時用 19。"""
    now = _now()
    try:
        return int(now.peer_cap()) if now is not None else 19
    except Exception:
        return 19


def _radio_on():
    """射頻**實際**開著沒（`connected`，不是「服務存不存在」）。"""
    now = _now()
    try:
        return bool(now is not None and now.connected)
    except Exception:
        return False


def _flag_on():
    """`Network.ESP_now.enable` —— **授權旗標**，不是射頻狀態。

    兩者的差別是本頁最常見的誤判來源：
      旗標 = 0 → `on_now_init` 會**拒絕**開（不偷偷開），開關怎麼撥都沒用
      旗標 = 1 但 connected = False → 有授權，只是還沒開起來
    """
    nw = _kv().shared.get("Network") or {}
    try:
        return bool(int((nw.get("ESP_now") or {}).get("enable", 0)))
    except Exception:
        return False


def _channel():
    now = _now()
    try:
        return now._channel() if now is not None else None
    except Exception:
        return None


def _set(key, txt):
    if _last.get(key) == txt:
        return
    _last[key] = txt
    lb = _lb.get(key)
    if lb is None:
        return
    try:
        lb.set_text(txt)
    except Exception:
        pass


def _msg(txt):
    print("[now_set]", txt)
    _set("msg", txt)


# ══════════════════════════════════════════════════════════════════
#  動作
# ══════════════════════════════════════════════════════════════════
def _do_scan():
    """廣播 `0x100D` 點名；回覆由 PeerRegistry 自動登記 → 變成「可選」。

    ★ 掃描**不寫方向**（`0x1016` 才是唯一決定方向的指令）。
      舊版 `on_identify_req` 會把 reply_cid 記成 master_cid，所以一次掃描
      = 射程內所有對端都把本板認成 master（無聲搶奪）。那個寫入已移除。

    ★ `timeout_ms` = 抖動視窗：廣播點名會讓所有對端**同一瞬間**回話，
      在空中是硬碰撞、會整批掉。帶上這個值讓各對端隨機延遲 [0, 500]ms 才回。
      真的被撞掉的節點：再按一次掃描即可（不做自動重試）。
    """
    b = _kv()
    ok = _tx_bcast(0x100D, {"reply_cid": int(getattr(b, "cid", 0xFFFF)) & 0xFFFF,
                            "timeout_ms": SCAN_SPREAD_MS})
    _msg("掃描 {}".format("已送出" if ok else "失敗"))
    _refresh_opt(force=True)


def _do_add():
    """可選 → 已選（**只改 UI 狀態**，硬體表要按「套用」才會動）。"""
    global _dirty
    if not (0 <= _oi < len(_opt_rows)):
        _msg("沒有可加入的項目")
        return
    mac = _opt_rows[_oi]
    if mac in _sel_macs:
        _msg("{} 已在已選".format(_short(mac)))
        return
    if len(_sel_macs) >= _cap():
        # ★ 不自動淘汰（使用者定案）：講清楚要做什麼，不要幫使用者決定踢掉誰
        _msg("已滿 {}/{} → 請先移除一些".format(len(_sel_macs), _cap()))
        return
    _sel_macs.append(mac)
    _dirty = True
    print("[now_set] + {}".format(_full(mac)))
    _msg("加入 {} （記得按套用）".format(_short(mac)))
    _refresh_sel(force=True)


def _do_remove():
    """已選 → 拿掉（**只改 UI 狀態**）。"""
    global _dirty
    if not (0 <= _si < len(_sel_macs)):
        _msg("廣播不可移除（它是預設）" if not _sel_macs else "沒有可移除的項目")
        return
    mac = _sel_macs.pop(_si)
    _dirty = True
    print("[now_set] - {}".format(_full(mac)))
    _msg("移除 {} （記得按套用）".format(_short(mac)))
    _refresh_sel(force=True)


def _do_apply():
    """★ 唯一會動硬體 peer 表的按鈕。

    順序很重要：**先** `apply_peers()` 真的改硬體，成功才 `save_now()` 落盤。
    反過來的話，硬體失敗（例如超過上限）會留下一份「DB 說有、硬體沒有」的
    清單 —— 開機時又會照著那份清單重建一次失敗的狀態。
    """
    global _dirty
    now = _now()
    if now is None:
        _msg("無 NowBus 服務 → 無法套用")
        return
    added, removed, failed = now.apply_peers(list(_sel_macs))
    if failed and not added and not removed:
        # apply_peers 的「超過上限」也是走這條（回 0,0,N）
        _msg("套用失敗（{} 台）→ 請先移除一些".format(failed))
        return
    st = _cfg().now_state()
    st["selected"] = list(_sel_macs)
    _cfg().save_now(st)
    _dirty = False
    _msg("套用：+{} -{} 失敗{} 現有{}".format(
        added, removed, failed, getattr(now, "peer_count", "?")))


def _do_clear():
    """清除所有記錄 —— **只清 ESP-NOW 的**（使用者定案）。

    硬體 peer 表（`clear_peers`）＋ `@now.*`（`clear_now`）。
    `@peer.*`（節點記錄）與 `@node.*`（配對方向）**不動** ——
    清無線電設定不該把「我控制誰」也一起忘掉。
    """
    global _sel_macs, _dirty
    now = _now()
    n = 0
    if now is not None:
        try:
            n = now.clear_peers()
        except Exception as e:
            print("[now_set] clear_peers 失敗:", e)
    _cfg().clear_now()
    _sel_macs = []
    _dirty = False
    _msg("清除：射頻 {} 筆（節點記錄/方向保留）".format(n))
    _refresh_sel(force=True)


def _toggle_enable():
    """啟用開關 → `0x1301 NOW_INIT{action}`（0=查詢 1=開 2=關）。

    ⚠️ **必須明確送 action=1/2**：`0x1301` 不給參數（或送 0）是**查詢**，
    不會開啟 ESP-NOW（欄位型別的原生預設值就是 0）。
    """
    want = not u.sw_get(_sw_on)
    u.sw_set(_sw_on, want)
    _exec(0x1301, {"action": 1 if want else 2})
    _sync_switches()          # 以**實際**狀態回寫（可能與剛才撥的不同）
    if want and not _radio_on():
        _msg("開不起來：授權旗標 = 0" if not _flag_on()
             else "開不起來：射頻未就緒")
    else:
        _msg("ESP-NOW {}".format("ON" if _radio_on() else "OFF"))


def _toggle_encrypt():
    """加密開關 → `NowBus.set_encrypt()`。

    ★ 沒有金鑰**不給開**（`set_encrypt` 自己會擋）：`encrypt=True` 硬體收得下，
      但對端沒有同一組 PMK/LMK 就解不開 ——「加得進去、卻什麼都收不到」
      是最難查的那種失敗。寧可擋在這裡並說清楚。

    金鑰（16-byte hex）目前只能從 `@now.pmk` / `@now.lmk` 來；
    LVGL 沒有鍵盤 → 匯入方式見 todo/07 §2.5（網頁 UI 或 /now_keys.json）。
    """
    global _dirty
    want = not u.sw_get(_sw_enc)
    now = _now()
    if now is None:
        u.sw_set(_sw_enc, False)
        _msg("無 NowBus 服務")
        return
    st = _cfg().now_state()
    if want and not (st.get("pmk") and st.get("lmk")):
        u.sw_set(_sw_enc, False)
        _msg("沒有金鑰（pmk/lmk）→ 不開加密")
        return
    ok = now.set_encrypt(want, st.get("pmk") or None, st.get("lmk") or None)
    u.sw_set(_sw_enc, bool(ok))
    if ok:
        st["encrypt"] = 1 if want else 0
        _cfg().save_now(st)
        # 上限從 19 變 6（或反之）→ 現有清單可能超標，但**不自動淘汰**
        _dirty = True
        _msg("加密 {} （上限 {}）{}".format(
            "ON" if want else "OFF", _cap(),
            "！已選超標，請移除" if len(_sel_macs) > _cap() else ""))
        _refresh_sel(force=True)
    else:
        _msg("加密切換失敗")


def _ch_step(d):
    """頻道 ◀ / ▶ → `0x1301{action:3, channel}`（執行期可換，**不需重啟**）。

    真機 10/10 驗過（changelog §33）。Master 換頻道不必重啟；
    Slave 由 Master 用 `0x1016` 覆蓋即可 —— 這是使用者定案的語意。
    """
    ch = _channel()
    if ch is None:
        ch = CH_MIN
    nch = ch + d
    if nch < CH_MIN:
        nch = CH_MAX
    elif nch > CH_MAX:
        nch = CH_MIN
    _exec(0x1301, {"action": 3, "channel": nch})
    _set("ch", "ch{}".format(_channel() if _channel() is not None else nch))


def _on_opt_move(dd):
    """可選清單編輯態：encoder 移動游標。"""
    global _oi
    n = len(_opt_btns)
    if n:
        _oi = (_oi + dd) % n
        u.list_select(_opt_btns, _oi, color=u.PRIMARY)


def _on_sel_move(dd):
    """已選清單編輯態：encoder 移動游標（第 0 列是 FF 時不可移除）。"""
    global _si
    n = len(_sel_btns)
    if n:
        _si = (_si + dd) % n
        u.list_select(_sel_btns, _si, color=u.PRIMARY)


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
    try:
        c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    except Exception:
        pass
    return c


def _fill(lst, labels):
    """重建清單內容。`mk_list` 只 append，所以要先 `clean()`。"""
    try:
        lst.clean()
    except Exception:
        pass
    out = []
    for txt in labels:
        try:
            btn = lst.add_text(txt)
            if u.F_NUM_S:
                btn.set_style_text_font(u.F_NUM_S, 0)
            out.append(btn)
        except Exception:
            continue
    return out


@register(id="now_setting", title="ESP-NOW", icon="sensors",
          desc="頻道·加密·已選節點", order=2, accent=0x7B1FA2)
def build():
    global scr, _opt_list, _sel_list, _opt_btns, _sel_btns
    global _lb, _last, _sw_on, _sw_enc, _oi, _si, _dirty
    nav.reset()
    _lb = {}
    _last = {}
    _oi = _si = 0
    _dirty = False
    _sel_macs = list((_cfg().now_state().get("selected") or []))

    scr = lv.obj(None)
    scr.set_style_bg_color(u.C(u.BG), 0)

    u.mk_label(scr, "ESP-NOW 設定", 8, 5, u.TEXT, u.ZH)
    _lb["msg"] = u.mk_label(scr, "", 122, 6, u.TEXT3, u.ZH)

    # ── 三張卡：啟用 / 頻道 / 加密 ──
    #   ★ 卡片內的標籤與元件都要留垂直間距：卡片高 46，標題佔 4..20，
    #     元件從 y=20 起、高 24 → 20..44 ✓。舊版 remote.py 就是沒算這個
    #     （開關 44×24 擺在 y=224，224+24=248 > 240，下緣被螢幕切掉）。
    #   ★ 橫向也要算：開關 44 寬，擺在 x=6 → 佔到 50，右邊只剩「卡寬-50」。
    #     卡寬是照「最長的那串字」訂的，不是平均分：
    #       啟用卡 112 → 52..112 剛好放得下「未授權」3 個中文字
    #       頻道卡 104 → 26+6 + 標籤 36 + 26 的右鈕，收到 98
    #       加密卡  88 → 52..88 放「19格」
    #     三張卡合計 112+4+104+4+88 = 312，左右各留 4（螢幕 320）。
    c_on = _panel(scr, 4, 22, 112, 46)
    u.mk_label(c_on, "啟用", 6, 4, u.TEXT3, u.ZH)
    _sw_on = u.mk_switch(c_on, 6, 20, on=False)
    nav.add(_sw_on, ITEM_SWITCH, on_change=_toggle_enable)
    _lb["flag"] = u.mk_label(c_on, "—", 52, 24, u.TEXT3, u.ZH)

    c_ch = _panel(scr, 120, 22, 104, 46)
    u.mk_label(c_ch, "頻道", 6, 4, u.TEXT3, u.ZH)
    b_l = u.mk_btn(c_ch, "<", 6, 22, 26, 20, "secondary")
    nav.add(b_l, ITEM_BUTTON, on_change=lambda: _ch_step(-1))
    _lb["ch"] = u.mk_label(c_ch, "ch—", 36, 24, u.TEXT, u.F_NUM_S)
    b_r = u.mk_btn(c_ch, ">", 72, 22, 26, 20, "secondary")
    nav.add(b_r, ITEM_BUTTON, on_change=lambda: _ch_step(1))

    c_enc = _panel(scr, 228, 22, 88, 46)
    u.mk_label(c_enc, "加密", 6, 4, u.TEXT3, u.ZH)
    _sw_enc = u.mk_switch(c_enc, 6, 20, on=False)
    nav.add(_sw_enc, ITEM_SWITCH, on_change=_toggle_encrypt)
    _lb["cap"] = u.mk_label(c_enc, "—", 52, 24, u.TEXT3, u.ZH)

    # ── 兩張表 ──
    #   「可選」不含 FF：它不是掃到的、也不能被加入，它是已選的預設值。
    u.mk_label(scr, "可選", 6, 72, u.TEXT3, u.ZH)
    _lb["optn"] = u.mk_label(scr, "(0)", 40, 72, u.TEXT2, u.ZH)
    _opt_list, _opt_btns = u.mk_list(scr, 4, 88, 124, 114, ["(按掃描)"],
                                     font=u.F_NUM_S)
    nav.add(_opt_list, ITEM_LIST, on_change=_on_opt_move)

    u.mk_label(scr, "已選", 134, 72, u.TEXT3, u.ZH)
    _lb["seln"] = u.mk_label(scr, "(0/19)", 168, 72, u.TEXT2, u.ZH)
    _sel_list, _sel_btns = u.mk_list(scr, 132, 88, 184, 114, ["FF  廣播"],
                                     font=u.F_NUM_S)
    nav.add(_sel_list, ITEM_LIST, on_change=_on_sel_move)

    # ── 底列：5 顆按鈕（60*5 + 3*4 = 312）──
    bw, gap, bx, y = 60, 3, 4, 208
    b1 = u.mk_btn(scr, "掃描", bx, y, bw, 22, "primary")
    nav.add(b1, ITEM_BUTTON, on_change=_do_scan)
    b2 = u.mk_btn(scr, "加入", bx + (bw + gap), y, bw, 22, "secondary")
    nav.add(b2, ITEM_BUTTON, on_change=_do_add)
    b3 = u.mk_btn(scr, "移除", bx + 2 * (bw + gap), y, bw, 22, "secondary")
    nav.add(b3, ITEM_BUTTON, on_change=_do_remove)
    b4 = u.mk_btn(scr, "套用", bx + 3 * (bw + gap), y, bw, 22, "primary")
    nav.add(b4, ITEM_BUTTON, on_change=_do_apply)
    b5 = u.mk_btn(scr, "清除", bx + 4 * (bw + gap), y, bw, 22, "danger")
    nav.add(b5, ITEM_BUTTON, on_change=_do_clear)

    u.fade_in(_opt_list, dy=5, time_ms=280, delay_ms=40)
    u.fade_in(_sel_list, dy=5, time_ms=280, delay_ms=120)

    _refresh_opt(force=True)
    _refresh_sel(force=True)
    _sync_switches()
    nav.paint()
    return scr


# ══════════════════════════════════════════════════════════════════
#  刷新
# ══════════════════════════════════════════════════════════════════
def _refresh_opt(force=False):
    """「可選」← `PeerRegistry.snapshot()`（任何總線上的節點都算）。

    ★ 只有帶 `mac` 的才進得來 —— 射頻 peer 表是 MAC 索引的，
      沒有 MAC 的節點（例如只從 UDP 聽到的）在這裡沒有意義。
    """
    global _opt_rows, _opt_btns, _oi
    reg = _kv().get_service("peers")
    rows = []
    if reg is not None:
        try:
            rows = [r for r in reg.snapshot() if r.get("mac")]
        except Exception:
            rows = []
    macs = [str(r["mac"]).upper() for r in rows]
    labels = ["{}  {}".format(_short(m), _cid_txt(r))
              for m, r in zip(macs, rows)] or ["(按掃描)"]
    if not force and labels == _last.get("opt"):
        _opt_rows = macs
        return
    _last["opt"] = labels
    _opt_rows = macs
    _opt_btns = _fill(_opt_list, labels)
    _oi = max(0, min(_oi, max(0, len(_opt_btns) - 1)))
    if _opt_btns:
        u.list_select(_opt_btns, _oi, color=u.PRIMARY)
    _set("optn", "({})".format(len(macs)))


def _refresh_sel(force=False):
    """「已選」← `@now.selected` ＋ FF 墊底。

    ★ FF 與具體節點**互斥**（使用者定案）：有節點 → FF 退下；
      節點全移除 → FF 自動回來。見檔頭。
    """
    global _sel_btns, _si
    labels = ["FF  廣播  ←預設"] if not _sel_macs else \
             ["{}".format(_short(m)) for m in _sel_macs]
    if not force and labels == _last.get("sel"):
        return
    _last["sel"] = labels
    _sel_btns = _fill(_sel_list, labels)
    _si = max(0, min(_si, max(0, len(_sel_btns) - 1)))
    if _sel_btns:
        u.list_select(_sel_btns, _si, color=u.PRIMARY)
    over = len(_sel_macs) > _cap()
    _set("seln", "({}/{}){}".format(len(_sel_macs), _cap(),
                                    " 超標!" if over else (" *" if _dirty else "")))


def _sync_switches():
    """單向同步（服務 → UI）：UI 只是顯示器，狀態來源是服務/設定。

    不這樣做的話，頁面會顯示「上次點擊的狀態」而不是實際狀態 ——
    最常見的就是「旗標 = 0 → on_now_init 拒絕開」時開關還停在 ON（騙人）。
    """
    try:
        on = _radio_on()
        if u.sw_get(_sw_on) != on:
            u.sw_set(_sw_on, on)
    except Exception:
        pass
    st = _kv().shared.get("now_setting") or {}
    now = _now()
    try:
        enc = bool(now.encrypt) if now is not None else bool(st.get("encrypt"))
    except Exception:
        enc = bool(st.get("encrypt"))
    try:
        if u.sw_get(_sw_enc) != enc:
            u.sw_set(_sw_enc, enc)
    except Exception:
        pass
    ch = _channel()
    _set("ch", "ch{}".format(ch) if ch is not None else "ch—")
    _set("cap", "{}格".format(_cap()))
    _set("flag", "授權" if _flag_on() else "未授權")


def update(run):
    """每幀被 app 呼叫。只每 REFRESH_EVERY 幀做一次。"""
    global _sel_macs
    if run % REFRESH_EVERY:
        return
    _refresh_opt()
    # 使用者改過但還沒套用 → 不要用 DB 蓋掉他的編輯
    if not _dirty:
        st = _kv().shared.get("now_setting") or {}
        cur = [str(x).upper() for x in (st.get("selected") or [])]
        if cur and cur != _sel_macs:
            _sel_macs = cur
            _refresh_sel(force=True)
    _sync_switches()


# ══════════════════════════════════════════════════════════════════
#  頁面接口（app.py 呼叫）
# ══════════════════════════════════════════════════════════════════
def on_enter():
    _refresh_opt(force=True)
    _refresh_sel(force=True)
    _sync_switches()


def on_leave():
    pass


def on_enc(d):
    nav.enc(d)


def on_confirm():
    nav.confirm()


def on_exit():
    return nav.exit()
