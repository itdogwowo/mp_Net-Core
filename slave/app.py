# app.py
from lib.sys.schema_loader import SchemaStore
from lib.sys.dispatch import Dispatcher
from lib.sys.proto import StreamParser, MAX_PAYLOAD, ADDR_BROADCAST
# ★ Router 關卡的判定常數（純常數，模組本身零裝置依賴）
from lib.sys.signal_router import V_OK, V_EXECUTE
# from lib.file_rx import FileRx # 已移除
from action.registry import register_all
from lib.sys.sys_bus import bus

import sys
IS_MICROPYTHON = (sys.implementation.name == 'micropython')
if not IS_MICROPYTHON:
    # CPython 相容: native 退化為無作用 stub (僅 py_compile / 離線測試用)
    class micropython:
        @staticmethod
        def native(f): return f
else:
    import micropython


class App:
    def __init__(self):
        # 1. 核心組件
        self.store = SchemaStore()
        self.store.load_dir("/schema")
        self.store.finalize()
        self.disp = Dispatcher(self.store)

        # 3. 註冊行為
        register_all(self)

        # 4. 全局模式貫通層（gmode）：MODE_SET/STOP 的單一事實來源。
        #    模式池 = pixel_maps（PixelTask 載入）+ /audio/modes（惰性合併）。
        try:
            from lib.sys.global_mode import GlobalMode
            bus.register_service("gmode", GlobalMode())
        except Exception:
            pass

        # 5. 素描紀錄（peers）：0x100D 掃到誰、從哪條線、用什麼位址再找到他。
        #    面板做「定向查詢」的前提（doc/03_notes/18_pixel_panel_control_path.md §7.2）。
        #    只建立 + 載入，學習由 net_actions（0x100E）與 bus_decode（被動）餵。
        try:
            from lib.sys.peer_registry import PeerRegistry
            reg = PeerRegistry()
            reg.load()
            bus.register_service("peers", reg)
        except Exception as e:
            print("[App] peer registry init failed:", e)

    def create_parser(self):
        # 協議負載上限統一由 lib.proto.MAX_PAYLOAD 決定 (純 payload, 不含 header/CRC)。
        # StreamParser 內部會自動加 9B header + 4B CRC 建立緩衝, 這裡不需再乘 2。
        return StreamParser(max_len=MAX_PAYLOAD)

    @micropython.native
    def handle_stream(self, parser, data, transport_name="Bus", send_func=None, extra_ctx=None,
                      router=None, src_bus=None):
        """
        處理數據流，並確保解析出當前 buffer 內所有的封包

        router / src_bus（P3，選填）:
          解碼鏈上的 Router 關卡。`src_bus` 是這一幀從哪個 bus 進來的（NowBus /
          NetBus / CircuitBus），Router 用它查路由表。兩者都給 None 時行為與
          未導入 Router 前**完全相同**。

          ★ gate() 放在 ADDR 過濾**之前**:
            被轉送的幀不必是「給本機」的 —— 這正是「Remote 的指令轉給下層節點」
            的用途（doc §1）。cID 未指派時 bus.cid = 0xFFFF = 廣播，全網都收，
            所以位置不改也不影響常見情境；改放前面則多支援「過路轉運」。
            enable=0 時 gate() 立刻回 V_OK，下面每一行與舊版一模一樣。
        """
        parser.feed(data)
        
        ctx = {
            "app": self,
            "transport": transport_name,
            "send": send_func
        }
        if extra_ctx:
            ctx.update(extra_ctx)
        
        # 🛠️ 關鍵：這是一個生成器，必須用 for 跑完
        # ADDR 過濾: 只收廣播 (ADDR_BROADCAST) 或定址到本機 cID 的幀。
        # cID 直接讀 bus.cid (ConfigManager 於 T0 建立並推動, 不在此重算)。
        # ADDR 是幀頭欄位, 在 payload 解碼前已由 StreamParser 取出; viper 只
        # 解 payload 欄位, 看不到 addr, 故過濾不進 viper。
        my_cid = bus.cid
        disp = self.disp
        packet_found = False
        while True:
            r = parser.pop_frame()
            if r is None:
                break
            _ver, addr, cmd, payload = r

            # ★ Router 關卡（唯一的判定點）: 轉送在此發生, verdict 決定要不要本地執行。
            #   注意 `addr` 已帶進 gate()，但目前的政策與 addr 無關（in/out 決定一切）。
            if router is not None:
                verdict = router.gate(src_bus, addr, cmd, payload)
                if verdict != V_OK and not (verdict & V_EXECUTE):
                    # 只轉發（V_FORWARD）或沒配對（V_DROP）→ 不進本地解碼鏈
                    continue

            if addr != ADDR_BROADCAST and addr != my_cid:
                continue
            packet_found = True
            disp.dispatch(cmd, payload, ctx)
        if packet_found:
            # 收到有效通訊 → 刷新提速的 COMMITTED 層 idle 倒數（通訊空閒超時重置）
            try:
                from lib.sys import bus_speed
                bus_speed.bus_speed_touch()
            except Exception:
                pass
            # 收到有效通訊 → 刷新 WDT 測試模式的「有人操作」倒數（同執行緒，見 watchdog.py）
            try:
                from lib.sys import watchdog
                watchdog.touch()
            except Exception:
                pass
        return packet_found