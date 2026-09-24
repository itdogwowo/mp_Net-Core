import time

from lib.sys.sys_bus import bus
from lib.sys.schema_codec import SchemaCodec
from lib.sys.proto import Proto, ADDR_BROADCAST

def dprint(msg, level=1):
    """
    統一的簡短日誌入口 (Dispatcher.log 的別名)
    Usage: dprint("Hello")
    """
    if Dispatcher.debug_level >= level:
        print(msg)

class Dispatcher:
    # 調試等級：0: 關閉, 1: 僅指令, 2: 完整 Payload
    # 可由 config.json 的 System.debug_level 覆寫（boot 載入 config 後設定）
    debug_level = 1

    @staticmethod
    def configure(level):
        """從 config 設定調試等級（0/1/2）。"""
        try:
            Dispatcher.debug_level = int(level)
        except Exception:
            pass

    @staticmethod
    def log(msg, level=1):
        """統一的日誌入口，取代 debugPrint"""
        if Dispatcher.debug_level >= level:
            print(msg)

    def __init__(self, store):
        self.store = store
        self.handlers = {}

    def on(self, cmd_int, handler):
        self.handlers[cmd_int] = handler

    # ══════════════════════════════════════════════════════════════════
    # 指令的三個出入口（線上一進一出 ＋ 內部直接執行）
    #
    #   dispatch(cmd, payload_bytes, ctx)   收 bytes → 解碼 → exec_cmd    ← 線上
    #   exec_cmd(cmd, args, ctx)           args 已是 dict → 直接派發       ← 內部
    #   make_cmd(cmd, args, addr)          args → 產生 bytes（NC4 幀）     ← 產生
    #
    #   為什麼要有 exec_cmd：內部觸發（按鈕／排程／頁面／task）手上本來就是
    #   dict，走 dispatch() 會多一趟 encode→decode 的來回 —— 而那一趟是為線上
    #   路徑而生的（見 signal_router 的說明：內部直呼的語意是「呼叫一個函式」，
    #   不是「假裝收到一幀」）。
    #   檢查／log／try／計時全部收在 exec_cmd，讓兩條路的觀測性一致。
    # ══════════════════════════════════════════════════════════════════

    def dispatch(self, cmd_int, payload_bytes, ctx):
        """線上路徑：payload_bytes → 解碼 → exec_cmd。

        行為與導入 exec_cmd 之前完全相同（訊息文字、順序、debug_level 語意皆同）。
        未知指令／無 handler 的檢查在此先做一次，理由是：
          - 無 handler 時不必做白工的解碼；
          - 只有這條路知道 bytes 長度（診斷訊息要印 Len）。
        """
        cmd_def = self.store.get(cmd_int)

        # 1. 基礎診斷
        if not cmd_def:
            if self.debug_level > 0:
                print(f"❓ [Unknown] 0x{cmd_int:04X} | Len: {len(payload_bytes)}")
            return

        handler = self.handlers.get(cmd_int)
        if not handler:
            if self.debug_level > 0:
                print(f"⚠️  [No-Handler] {cmd_def['name']} (0x{cmd_int:04X})")
            return

        # 2. 解析數據
        try:
            args = SchemaCodec.decode(cmd_def, payload_bytes, self.store)
        except Exception as e:
            print(f"❌ [Error] {cmd_def['name']}: {e}")
            return

        # 3. 派發（與內部路徑共用同一段檢查／log／try／計時）
        return self.exec_cmd(cmd_int, args, ctx, _cmd_def=cmd_def)

    def exec_cmd(self, cmd_int, args, ctx, _cmd_def=None):
        """內部路徑：args 已經是 dict（欄位名 → 值）→ 直接派發，不做編解碼。

        args 的「型別與範圍」由呼叫端負責給準 —— 協議本身在 schema 標明型別；
        編碼器另有超界保護，但那是最後一道網，不該當常態依賴。

        **args 的鍵 = 呼叫端真的給了什麼（不補 0）** —— 這與 dispatch() 是**同一個
        規則**：decode 對「payload 裡沒有的欄位」也不會放進 args（短 payload → 短
        args，見 SchemaCodec.decode 的 `pos >= plen → break`）。差別只在 encode()
        會把缺的欄位補 0，所以「自己送給自己」時欄位永遠齊全。
        對 handler 的意義：缺欄位 = 用它自己的預設值。例如 waiting_to_trash.on_ctl
        的 brightness 預設 0xFF（＝不改）—— 內部路徑**省略 brightness 就是「不改」**；
        而線上路徑要表達「不改」必須明確送 0xFF（補 0 會被當成「設成 0」）。

        與 dispatch() 另一個觀測差異：SchemaCodec.decode() 會在 args 裡多放
        `_name` / `_cmd` 兩個資訊鍵，exec_cmd 不會（args 原樣交給 handler）。
        已確認全樹沒有任何 handler 讀這兩個鍵（只用在 debug 顯示）。
        """
        cmd_def = _cmd_def if _cmd_def is not None else self.store.get(cmd_int)

        if not cmd_def:
            if self.debug_level > 0:
                print(f"❓ [Unknown] 0x{cmd_int:04X}")
            return

        handler = self.handlers.get(cmd_int)
        if not handler:
            if self.debug_level > 0:
                print(f"⚠️  [No-Handler] {cmd_def['name']} (0x{cmd_int:04X})")
            return

        try:
            # 調試輸出面板 (現代化風格)
            if self.debug_level >= 1:
                source = ctx.get("transport", "Unknown")
                print(f"🔹 [{source}] {cmd_def['name']} (0x{cmd_int:04X})")
                if self.debug_level >= 2:
                    print(f"   ﹂ Args: {args}")

            # 執行與性能監控
            start_t = time.ticks_us()
            handler(ctx, args)
            end_t = time.ticks_us()

            if self.debug_level >= 2:
                print(f"   ﹂ ✅ Exec Time: {end_t - start_t} us")

        except Exception as e:
            print(f"❌ [Error] {cmd_def['name']}: {e}")

    def make_cmd(self, cmd_int, args, addr=ADDR_BROADCAST):
        """產生一個 NC4 訊框（args dict → bytes）—— 與 dispatch() 對稱的一環。

        ⚠️ 生命週期：回傳值是**指向共享 buffer 的 memoryview**（Proto.pack 的
           契約）—— 下一次 Proto.pack() 會覆蓋它。呼叫端必須「立即消費」
           （送出／寫入）；若要收集多筆，請自行 bytes() 複製。

        未知指令 → 印訊息並回 None（診斷風格與 dispatch 一致）。
        """
        cmd_def = self.store.get(cmd_int)
        if cmd_def is None:
            if self.debug_level > 0:
                print(f"❓ [make_cmd] 未知指令 0x{cmd_int:04X}")
            return None
        return Proto.pack(cmd_int, SchemaCodec.encode(cmd_def, args), addr)
