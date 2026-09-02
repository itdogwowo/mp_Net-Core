# app.py
from lib.sys.schema_loader import SchemaStore
from lib.sys.dispatch import Dispatcher
from lib.sys.proto import StreamParser, MAX_PAYLOAD, ADDR_BROADCAST
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

    def create_parser(self):
        # 協議負載上限統一由 lib.proto.MAX_PAYLOAD 決定 (純 payload, 不含 header/CRC)。
        # StreamParser 內部會自動加 9B header + 4B CRC 建立緩衝, 這裡不需再乘 2。
        return StreamParser(max_len=MAX_PAYLOAD)

    @micropython.native
    def handle_stream(self, parser, data, transport_name="Bus", send_func=None, extra_ctx=None):
        """
        處理數據流，並確保解析出當前 buffer 內所有的封包
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