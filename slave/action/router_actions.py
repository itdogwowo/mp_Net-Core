# action/router_actions.py
# ═══════════════════════════════════════════════════════════════════════════
# 0x16xx — 訊號 Router 的執行期指令（P5）
#
# 設計與規則唯一真相 → doc/02_guides/16_signal_router.md（§12 指令表）
# 路由表本體         → lib/sys/signal_router.py
# 誰建立 router      → tasks/bus_decode.py（on_start 建立並註冊 "signal_router"）
#
# 這裡**不持有**任何路由狀態: 全部操作都打在 bus_decode 建立的那個實例上，
# 所以執行期改的路由立刻生效（不用重啟），ROUTER_SAVE 只是把它寫進 config.json。
#
#   ROUTER_STATUS    0x1601  → 0x1606  介面 / route 命中與丟棄計數 / load 錯誤
#   ROUTER_ROUTE_ADD 0x1602  → 0x1606  新增或覆寫一條 route（JSON 字串）
#   ROUTER_ROUTE_DEL 0x1603  → 0x1606  依 `in` 刪除一條 route
#   ROUTER_TABLE_GET 0x1604  → 0x1606  讀回目前路由表（分頁）
#   ROUTER_SAVE      0x1605  → 0x1606  存回 config.json（可順便切 enable）
#   ROUTER_ACK       0x1606  唯一回覆
#
# enable 位元組約定（沿用本專案既有慣例 SYS_CTRL/WIFI_CTRL 的 0xFF = 不變）:
#   0xFF = 只存檔、不改開關    0 = 關    1 = 開    其他 = 不變
#   ⚠️ 送 ROUTER_SAVE **一定要帶這 1 byte**；只按「存檔」請送 0xFF。
# ═══════════════════════════════════════════════════════════════════════════

import json

from lib.sys.proto import Proto
from lib.sys.schema_codec import SchemaCodec
from lib.sys.sys_bus import bus

CMD_ROUTER_STATUS = 0x1601
CMD_ROUTE_ADD = 0x1602
CMD_ROUTE_DEL = 0x1603
CMD_ROUTER_TABLE_GET = 0x1604
CMD_ROUTER_SAVE = 0x1605
CMD_ROUTER_ACK = 0x1606

# 路由表每頁條數（payload 上限 MAX_PAYLOAD=8192；實務上第一頁就全部回完）
PAGE_ROUTES = 32

# ROUTER_ACK.code
CODE_OK = 0
CODE_NO_ROUTER = 1        # BusDecodeTask 還沒建立 router
CODE_BAD_JSON = 2         # route_json 不是合法 JSON
CODE_BAD_ROUTE = 3        # route 本身驗證不通過（in/out 欄位問題）
CODE_SAVE_FAIL = 4        # 寫 config.json 失敗


def _router():
    """取回 BusDecodeTask 建立的那個 router（唯一實例）。"""
    return bus.get_service("signal_router")


def _brief(r):
    """小顆狀態（ADD/DEL/SAVE 的回覆帶這個就好，不必整張表）。"""
    return {"enable": 1 if r.enable else 0, "in": sorted(r.by_in.keys())}


def _reply(ctx, app, ok, message, data=None, code=CODE_OK):
    """統一回覆 0x1606 ROUTER_ACK。

    沒有 send（例如 ScheduleTask 從 vBus 注入的幀）就安靜跳過 —— 不 raise、
    不 print 噪音，維持「回程走來源通道」的既有行為（doc §6）。
    """
    send = ctx.get("send")
    if not send:
        return
    try:
        cmd_def = app.store.get(CMD_ROUTER_ACK)
        payload = SchemaCodec.encode(cmd_def, {
            "ok": 1 if ok else 0,
            "code": code,
            "message": message or "",
            "data_json": json.dumps(data) if data is not None else "",
        })
        send(Proto.pack(CMD_ROUTER_ACK, payload))
    except Exception as e:
        print("❌ [Router] 回覆失敗: {}".format(e))


def _no_router(ctx, app):
    return _reply(ctx, app, False,
                  "router 尚未啟動（BusDecodeTask 未建立 signal_router）",
                  code=CODE_NO_ROUTER)


def on_router_status(ctx, args):
    """0x1601: 介面狀態 + 每條 route 的命中/丟棄計數 + load 期間的錯誤。"""
    app = ctx["app"]
    r = _router()
    if r is None:
        return _no_router(ctx, app)
    return _reply(ctx, app, True, "", data=r.status())


def on_route_add(ctx, args):
    """0x1602: 新增或覆寫一條 route。payload = route 的單行 JSON 字串。"""
    app = ctx["app"]
    r = _router()
    if r is None:
        return _no_router(ctx, app)

    raw = args.get("route_json") or ""
    try:
        route = json.loads(raw)
    except Exception as e:
        return _reply(ctx, app, False, "route_json 不是合法 JSON: {}".format(e),
                      code=CODE_BAD_JSON)

    ok, msg = r.route_add(route)
    if not ok:
        # 驗證邏輯與開機 load() 完全相同（同一份 _add_route），錯誤訊息直接透傳
        print("❌ [Router] ROUTE_ADD 失敗: {}".format(msg))
        return _reply(ctx, app, False, msg, code=CODE_BAD_ROUTE)
    if not r.enable:
        msg += "（目前 enable=0，尚未生效；ROUTER_SAVE 帶 enable=1 可立即開啟）"
    return _reply(ctx, app, True, msg, data=_brief(r))


def on_route_del(ctx, args):
    """0x1603: 依 `in` 刪除一條 route。"""
    app = ctx["app"]
    r = _router()
    if r is None:
        return _no_router(ctx, app)
    ok, msg = r.route_del(args.get("in_name") or "")
    if not ok:
        print("❌ [Router] ROUTE_DEL 失敗: {}".format(msg))
    return _reply(ctx, app, ok, msg, data=_brief(r),
                  code=CODE_OK if ok else CODE_BAD_ROUTE)


def on_router_table_get(ctx, args):
    """0x1604: 讀回目前路由表（分頁；`page` 0-based）。"""
    app = ctx["app"]
    r = _router()
    if r is None:
        return _no_router(ctx, app)
    return _reply(ctx, app, True, "", data=r.table(args.get("page", 0), PAGE_ROUTES))


def on_router_save(ctx, args):
    """0x1605: 把「目前生效的設定」寫回 config.json（選填順便切 enable）。

    寫回的是**記憶體裡的路由表**（＝執行期 ADD/DEL 後的結果），不是開機時讀到的舊值。

    ⚠️ 原子性: enable 的切換**只在存檔成功後**才生效。存檔失敗 → 記憶體與
       bus.shared 都回復原狀，回 ok=0。理由: 「說存檔失敗但開關已經翻了」是最
       難排查的一種狀態（不確定就出聲，也不要留下半套結果）。
    """
    app = ctx["app"]
    r = _router()
    if r is None:
        return _no_router(ctx, app)

    en = args.get("enable", 0xFF)
    want = en if (en == 0 or en == 1) else None      # 其他值（含 0xFF）= 不改開關

    # ⚠️ 順序很重要: ConfigManager 必須**先 import**（它 import 時會跑 load_setup()，
    #    把 config.json 的內容 update 進 bus.shared）—— 若在寫入 bus.shared["Router"]
    #    之後才 import，load_setup 會把剛寫的值蓋回檔案裡的舊值，存檔就存到舊設定。
    #    （真機 boot.py 已在 T0 import 過，所以這條路只在「首次用到」時才會踩到；
    #      這是 router_board_test.py §3 真的在板上抓到的 bug。）
    try:
        from lib.sys.ConfigManager import cfg_manager
    except Exception as e:
        print("❌ [Router] SAVE 失敗: {}".format(e))
        return _reply(ctx, app, False,
                      "存檔失敗，設定未變更: {}".format(e), code=CODE_SAVE_FAIL)

    prev = bus.shared.get("Router")
    snap = r.snapshot()
    if want is not None:
        snap["enable"] = want
    bus.shared["Router"] = snap

    try:
        cfg_manager.save_from_bus(update_key="Router")
    except Exception as e:
        if prev is None:
            bus.shared.pop("Router", None)
        else:
            bus.shared["Router"] = prev
        print("❌ [Router] SAVE 失敗: {}".format(e))
        return _reply(ctx, app, False,
                      "存檔失敗，設定未變更: {}".format(e), code=CODE_SAVE_FAIL)

    if want is not None:
        r.set_enable(want)

    return _reply(ctx, app, True,
                  "已存入 config.json（{} 條 route, enable={}）".format(
                      len(snap["routes"]), snap["enable"]),
                  data=_brief(r))


def register(app):
    """註冊 0x16xx 到分發器。"""
    app.disp.on(CMD_ROUTER_STATUS, on_router_status)
    app.disp.on(CMD_ROUTE_ADD, on_route_add)
    app.disp.on(CMD_ROUTE_DEL, on_route_del)
    app.disp.on(CMD_ROUTER_TABLE_GET, on_router_table_get)
    app.disp.on(CMD_ROUTER_SAVE, on_router_save)
    print("✅ [Action] Router actions registered (0x16xx)")
