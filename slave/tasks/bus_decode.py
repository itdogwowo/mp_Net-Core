import time
from lib.sys.task import Task
from lib.sys.sys_bus import bus
from lib.sys.signal_router import SignalRouter
from lib.sys.log_service import get_log


def _router_log(msg):
    """signal_router 的輸出轉進統一日誌（沒有 log 服務時退回 print）。"""
    try:
        get_log().warn(msg)
    except Exception:
        print(msg)


class BusDecodeTask(Task):
    def __init__(self, name, ctx):
        super().__init__(name, ctx)
        self.app = ctx["app"]
        self._buses = []
        self._parsers = {}
        # ★ Router（P3）: 解碼鏈上的關卡，不是另一個讀 rx_hub 的消費者（doc §2.1）。
        #   None = 尚未建立或建立失敗 → handle_stream 走完全原本的路徑。
        self.router = None
        self._housekeep = None
        # 開機結算是否已做（finalize：結算 pending + 補預設，只做一次）
        self._router_ready = False

    def on_start(self):
        super().on_start()
        self._buses = []
        self._parsers = {}
        self._src_ts = 0
        buf_cfg = bus.shared.get("Buffer") or {}
        self._max_slots = int(buf_cfg.get("decode_budget_slots", 32) or 0)
        if self._max_slots <= 0:
            self._max_slots = 1
        self._router_setup()

    # ── Router: 建立一次、註冊成服務、載入設定 ─────────────────────────
    def _router_setup(self):
        """建立（或取回）Router 並載入 config.json 的 Router 區塊。

        建立後註冊為 bus 服務 `signal_router`，讓 action 層（0x16xx，P5）拿到
        **同一個實例** —— 執行期 ROUTE_ADD 改的就是這裡的路由表。

        enable=0（預設）時 gate() 立刻回 V_OK，解碼路徑一行都不進 ——
        這是「升級不破壞」的保證，也是 P3 回歸驗收的核心。
        """
        self.router = None
        self._housekeep = None
        try:
            r = bus.get_service("signal_router")
            if r is None:
                r = SignalRouter(log=_router_log)
                bus.register_service("signal_router", r)
            n = r.load(bus.shared.get("Router") or {})
            r.sync_ifaces(bus)
            self.router = r
            self._housekeep = r.housekeep
            # 壞設定開機就講清楚（不猜、不靜默 —— doc §5）
            for e in r.errors():
                print("❌ [Router] {}".format(e))
            # ★ 時機 ①「開機那一次」：autofill ＋ 寫回 config。
            #   此刻看到的是 **boot.py 建的通道**（uartN 等）；
            #   Task 內部才註冊的（now / net / udp / vbus）還沒上線，
            #   那些由時機 ②「任務開始那一次」補上（見 loop()）。
            #   兩次都會寫檔 → 第一次寫的是部分集合，第二次才是完整集合。
            self.finalize_router()
            if r.enable:
                get_log().info("🔀 [Router] enable=1, {} route(s), ifaces={}".format(
                    n, sorted(r.ifaces.keys())))
        except Exception as e:
            self.router = None
            self._housekeep = None
            get_log().error("[BusDecode] router init failed: {}".format(e))

    def finalize_router(self):
        """★ **Router 的主動函數** —— 補預設（autofill）＋ 把表寫回 config.json。

        **autofill 預設就是開的，沒有開關**（使用者：『我應該是不會禁止的』）。
        唯一的例外是 **`self → self`** —— `self` 的預設 `out` 是 `[]`（不指向自己），
        因為 vBus 注入的幀「已經在本地」，`out` 再寫 `self` 等於多 dispatch 一次。
        要覆蓋就自己寫 `{ "in": "self", "out": ["self"] }`。

        使用者的時機規格（2026-09）：
          「這應該是一個**主動的函數**而不是自動的函數 …… **開機的時候執行一次**、
            **任務開始的時候那一次**，之後就幾乎沒有主動要求他執行的時機了，
            除非我日後用其他方法、其他時機想這樣做。」

        所以它**只被呼叫兩次**（都在開機過程中，見 `_router_setup()` 與 `loop()`），
        不對任何事件自動反應。**要再跑一次就再呼叫它。**

        做的事：
          ① `router.finalize()` —— 結算 pending（使用者寫了、通道現在存在的
             route，用**他的內容**註冊）＋ autofill（只補缺口，不動使用者寫過的）
          ② 把整張表寫回 config.json（＝「幫我註冊」）

        ⚠️ `_router_ready` 只是「任務開始那次別重複跑」的守衛，不是自動機制；
           外部呼叫這個方法不受它限制。
        """
        r = self.router
        if r is None:
            return []
        added = r.finalize()
        self._persist_router_table()
        get_log().info(
            "🔀 [Router] finalize：{} route(s), ifaces={}, 本次補預設={}".format(
                len(r.by_in), sorted(r.ifaces.keys()), added))
        return added

    def _persist_router_table(self):
        """把目前生效的路由表寫回 config.json（＝使用者要的「幫我註冊進 config」）。

        使用者的規格：
          「所有通訊通道都會被註冊到 config 當中，**如果沒有註冊的通道就會幫用戶
            自行註冊目標是 self**」

        ★ 只在 `finalize_router()` 裡被呼叫（時機 ①「開機」與 ②「任務開始」各一次）。
          **不在通道上線時寫** —— 那會在使用者還沒輪到結算之前就覆蓋他寫的東西。

        寫什麼：`router.snapshot()` ＝ **目前生效的整張表**（含使用者寫的）。
          所以刻意刪掉的條目，下次開機結算會被補回來並寫進檔案 ——
          這是「幫我註冊」的預期行為。**要關掉某條通道請寫 `out: []`，不要刪。**
        """
        r = self.router
        if r is None:
            return
        added = r.take_autofill()        # 取走即清空（介面契約保留）
        try:
            from lib.sys.ConfigManager import cfg_manager
            bus.shared["Router"] = r.snapshot()
            cfg_manager.save_from_bus(update_key="Router")
            get_log().info("🔀 [Router] 路由表寫回 config.json：{} 條（本次補 {}）".format(
                len(r.by_in), added))
        except Exception as e:
            get_log().warn("[Router] 寫回 config 失敗（記憶體仍生效）: {}".format(e))

    def _refresh_sources(self):
        sources = bus.get_service("bus_sources")
        if sources:
            self._buses = list(sources.list() or [])
            return
        self._buses = []
        ctrl = bus.get_service("net_bus_ctrl")
        discv = bus.get_service("net_bus_discovery")
        if ctrl:
            self._buses.append(ctrl)
        if discv:
            self._buses.append(discv)
        circuit_list = bus.get_service("circuit_bus_list")
        if circuit_list:
            for cb in circuit_list:
                self._buses.append(cb)

    def loop(self):
        if not self.running:
            return

        now = time.ticks_ms()
        if time.ticks_diff(now, self._src_ts) > 100:
            self._src_ts = now
            self._refresh_sources()

        # ── Router：通道註冊不靠這裡輪詢（事件驅動，見 SysBus.register_service）
        #
        #   ★ 時機 ②「任務開始那一次」 —— 主動呼叫 finalize_router()，**只做一次**。
        #     （時機 ① 是 _router_setup() 的「開機」那一次。）
        #     這次看到的是**完整集合**：Task 內部註冊的 now / net / udp / vbus
        #     都已經上線了（它們排在 layer 0，會即時透過 register_service hook
        #     補進 Router）。
        #     之後**幾乎沒有主動要求它執行的時機** —— 除非日後用別的方法
        #     在某個時機想再跑一次，那就直接呼叫 `self.finalize_router()`。
        if self.router is not None and not self._router_ready:
            self._router_ready = True
            self.finalize_router()

        if self._buses:
            self._drain()
        # 素描紀錄（peers）：節流把累積的變更寫回 /peers.json
        #   （學習本身在 handle_stream 的來源標記與 net_actions 的 0x100E handler）
        reg = bus.get_service("peers")
        if reg is not None:
            try:
                reg.housekeep()
            except Exception:
                pass
        # 家事掛尾端（doc §8）: 目前是 no-op 的介面契約，之後的非同步重送／佇列
        # 會掛在這裡，呼叫端不用改。快取 bound method，熱路徑上只是一個函式呼叫。
        hk = self._housekeep
        if hk is not None:
            hk()

    def _drain(self):
        """poll 各 bus → rx_hub → 解幀 → handle_stream（含 Router 關卡）。

        獨立成一個方法只是為了讓上面的 housekeep() 在配額用盡提早結束時
        仍然會被呼叫（原本的 `return` 會直接跳過尾端）。
        """
        used = 0
        router = self.router
        peers = bus.get_service("peers")
        for b in self._buses:
            hub = getattr(b, "rx_hub", None)
            if hub is None:
                continue
            p = self._parsers.get(id(b))
            if p is None:
                p = self.app.create_parser()
                self._parsers[id(b)] = p
            ctx_extra = getattr(b, "_decode_ctx", None) or {}
            # 被動素描：這一幀從哪個射頻位址來（只有 NowBus 這類會填 _peer_mac）
            peer_mac = ctx_extra.get("_peer_mac")
            while True:
                if used >= self._max_slots:
                    return
                # view 模式: 直接讀 slot 的 memoryview, 省掉 read_into 的 target[:] 複製。
                # handle_stream -> parser.feed 是「立即複製進 parser._buf」, 不持有 view,
                # 所以 finally 裡 release_read() 安全。
                view = hub.get_read_view()
                if view is None:
                    break
                try:
                    ln = view[0] | (view[1] << 8)
                    if ln > 0:
                        data = view[2:2 + ln]
                        # 被動素描：只在新位址時登記（已知的走 0 成本路徑，
                        # 不做逐幀 learn —— 否則 hits/log 會被灌爆）
                        if peers is not None and peer_mac and not peers.knows(peer_mac):
                            try:
                                peers.learn_from_frame(b, peer_mac, 0,
                                                       ctx_extra)
                            except Exception:
                                pass
                        self.app.handle_stream(
                            p,
                            data,
                            getattr(b, "label", "Bus"),
                            b.write,
                            ctx_extra,
                            router,      # ★ 解碼鏈上的 Router 關卡（None = 不作用）
                            b,           # ★ 這一幀的來源 bus（Router 查路由表用）
                        )
                finally:
                    hub.release_read()
                self.success += 1
                used += 1

    def on_stop(self):
        super().on_stop()
        self._buses = []
        self._parsers = {}
