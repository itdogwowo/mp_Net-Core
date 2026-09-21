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
            self._persist_autofill()
            if r.enable:
                get_log().info("🔀 [Router] enable=1, {} route(s), ifaces={}".format(
                    n, sorted(r.ifaces.keys())))
        except Exception as e:
            self.router = None
            self._housekeep = None
            get_log().error("[BusDecode] router init failed: {}".format(e))

    def _persist_autofill(self):
        """把「自動註冊的結果寫回 config.json」**停用**（2026-09）。

        為什麼停用（原本是 Router P4 的「自動註冊」設計）：
          自動註冊會在使用者沒寫的情況下補 route，並把補出來的結果寫進
          config.json。結果是**使用者的設定檔會自己長出他沒寫過的東西**：

            - 使用者把某條 route 刪掉 → 下次開機被補回來並寫回檔案
            - 使用者寫了但通道不存在（規格：不註冊、跳過）→ 通道上線後
              被 autofill 用預設值取代，還寫回檔案

        現在的行為：
          - autofill **只在記憶體裡建表**，不碰 config.json
          - 使用者要落盤請明確動作：`ROUTER_SAVE`（0x1605）
          - `take_autofill()` 仍然取走並清空（保持介面契約），只是不再寫檔

        ⚠️ 這裡刻意**不再呼叫** `cfg_manager.save_from_bus`。
           要恢復舊行為就是把下面那三行接回去。
        """
        r = self.router
        if r is None:
            return
        added = r.take_autofill()        # 取走即清空（不寫檔，只留記錄）
        if added:
            get_log().info("🔀 [Router] 自動註冊 {} 條線路（僅記憶體，未寫回 config）: {}".format(
                len(added), added))

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

        # ── Router：通道註冊已經不靠這裡輪詢 ───────────────────────
        #   原本每 100ms 呼叫 router.sync_ifaces()，理由是「各 Task 上線
        #   順序不定，錯過了要等下一輪」。那其實是症狀的解法：真正原因是
        #   BusDecodeTask.on_start 跑在 NowTask / NetworkTask 之前，
        #   第一輪 sync 時 NowBus 還不存在。
        #   現在改成 **事件驅動**：sys_bus.register_service() 在通道註冊的
        #   那一刻直接通知 Router（見 SysBus.register_service），
        #   所以在這裡不必再輪詢。
        #
        #   ⚠️ 唯一保留的一次性動作：開機後第一次 finalize()
        #      （結算 pending 的使用者 route + 補預設值）。
        #      為什麼不能更早：boot 期的通道是陸續註冊的，太早結算會
        #      把「還沒上線」誤判成「不存在」。
        if self.router is not None and not self._router_ready:
            self._router_ready = True
            added = self.router.finalize()
            self._persist_autofill()
            get_log().info(
                "🔀 [Router] 開機結算：{} route(s), ifaces={}, 補預設={}".format(
                    len(self.router.by_in),
                    sorted(self.router.ifaces.keys()), added))

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
