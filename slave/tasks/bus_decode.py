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
        """把 Router 自動補出來的 route 寫回 config.json（= 自動註冊）。

        為什麼掛在這裡: 「自動註冊」是 Router 的行為，但**寫檔是 ConfigManager 的
        職責** —— 所以走 `bus.shared` + `save_from_bus`（單一寫入者），不自己開檔。

        只在真的補了新線路時才寫一次（`take_autofill()` 取走即清空）→ 開機後不會
        反覆寫檔；寫失敗也不影響記憶體裡已經生效的路由表。
        """
        r = self.router
        if r is None:
            return
        added = r.take_autofill()
        if not added:
            return
        try:
            from lib.sys.ConfigManager import cfg_manager
            bus.shared["Router"] = r.snapshot()
            cfg_manager.save_from_bus(update_key="Router")
            get_log().info("🔀 [Router] 自動註冊 {} 條線路到 config.json: {}".format(
                len(added), added))
        except Exception as e:
            get_log().warn("[Router] 自動註冊寫入 config 失敗（記憶體仍生效）: {}".format(e))

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
            # 各 Task 上線順序不定（vBus 惰性建立、ESP-NOW 可能較晚才 init）
            # → 順路把新上線的通道補進 Router（重複呼叫是 no-op），
            #   並把「自動註冊」補出來的新線路寫回 config.json。
            if self.router is not None:
                self.router.sync_ifaces(bus)
                self._persist_autofill()
        if self._buses:
            self._drain()
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
        for b in self._buses:
            hub = getattr(b, "rx_hub", None)
            if hub is None:
                continue
            p = self._parsers.get(id(b))
            if p is None:
                p = self.app.create_parser()
                self._parsers[id(b)] = p
            ctx_extra = getattr(b, "_decode_ctx", None) or {}
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
