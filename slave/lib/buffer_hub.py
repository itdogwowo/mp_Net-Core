import gc
import micropython

_IDLE = micropython.const(0)
_READY = micropython.const(1)
_READING = micropython.const(2)


@micropython.viper
def viper_copy(dst, src, src_off: int, n: int):
    pd = ptr8(dst)
    ps = ptr8(src)
    for i in range(n):
        pd[i] = ps[src_off + i]


@micropython.viper
def viper_copy_full(dst, src, n: int):
    pd = ptr8(dst)
    ps = ptr8(src)
    for i in range(n):
        pd[i] = ps[i]


class AtomicStreamHub:
    IDLE = _IDLE
    READY = _READY
    READING = _READING

    def __init__(self, size, num_buffers=3):
        self._bufs = [bytearray(size) for _ in range(num_buffers)]
        self._views = [memoryview(b) for b in self._bufs]

        self._status = [_IDLE] * num_buffers
        self._w_ptr = 0
        self._r_ptr = 0

        self.size = size
        self.num_buffers = num_buffers
        self._last_read_idx = None

        print("🚀 [BufferHub] Ready: {} KB total".format((size * num_buffers) // 1024))

    @property
    def dirty(self):
        """
        兼容舊 API：檢查是否有數據可讀
        """
        return self._status[self._r_ptr] == _READY

    @micropython.native
    def write_from(self, source):
        """
        將數據寫入 HUB (複製模式)
        ───────────────────────────────────────────────
        :param source: 來源數據 (bytes/bytearray/memoryview)
        :return: bool (True: 寫入成功, False: 緩衝區已滿)
        """
        ptr = self._w_ptr
        
        # 檢查當前指標指向的槽位是否可寫入
        if self._status[ptr] != _IDLE:
            return False
        
        # 執行高效內存拷貝 (viper 級優化)
        n = len(source)
        if n > self.size:
            n = self.size
        viper_copy_full(self._views[ptr], source, n)
        
        # 更新狀態與指標
        self._status[ptr] = _READY
        self._w_ptr = (ptr + 1) % self.num_buffers
        
        return True

    @micropython.native
    def read_into(self, target):
        """
        將數據從 HUB 讀出 (複製模式)
        ───────────────────────────────────────────────
        :param target: 目標緩衝區 (必須預分配好)
        :return: bool (True: 讀取成功, False: 無數據可讀)
        """
        # 如果之前有 buffer 處於 READING 狀態，先釋放
        if self._last_read_idx is not None:
             self._status[self._last_read_idx] = _IDLE
             self._last_read_idx = None

        ptr = self._r_ptr
        
        # 檢查當前指標指向的槽位是否有數據
        if self._status[ptr] != _READY:
            return False
            
        # 執行高效內存拷貝
        n = len(target)
        if n > self.size:
            n = self.size
        viper_copy_full(target, self._views[ptr], n)
        
        # 釋放槽位並更新指標
        self._status[ptr] = _IDLE
        self._r_ptr = (ptr + 1) % self.num_buffers
        
        return True

    @micropython.native
    def flush(self):
        """
        快速重設 HUB 狀態
        ───────────────────────────────────────────────
        不動作內存擦除，僅重設指針與狀態機，耗時極短
        """
        # range 在 native 中有優化
        for i in range(self.num_buffers):
            self._status[i] = _IDLE
            
        self._w_ptr = 0
        self._r_ptr = 0
        self._last_read_idx = None

    def get_fill_level(self):
        """
        當前積壓的緩衝數量 (調試用)
        """
        count = 0
        for s in self._status:
            if s == _READY:
                count += 1
        return count

    # --- 兼容舊 API ---

    @micropython.native
    def get_write_view(self):
        """
        獲取寫入視圖 (零拷貝模式)
        注意：如果緩衝區滿，返回 None
        """
        ptr = self._w_ptr
        if self._status[ptr] != _IDLE:
            return None
        return self._views[ptr]

    @micropython.native
    def commit(self):
        """
        提交寫入
        """
        ptr = self._w_ptr
        # 只有在 IDLE 狀態下才能提交 (防止重複提交或錯誤調用)
        if self._status[ptr] == _IDLE:
            self._status[ptr] = _READY
            self._w_ptr = (ptr + 1) % self.num_buffers
    
    @micropython.native
    def get_read_view(self):
        """
        獲取讀取視圖 (零拷貝模式)
        會鎖定緩衝區直到下一次調用 get_read_view 或 read_into
        """
        # 1. 釋放上一個 READING 的 buffer
        if self._last_read_idx is not None:
            self._status[self._last_read_idx] = _IDLE
            self._last_read_idx = None

        # 2. 檢查是否有新數據
        ptr = self._r_ptr
        if self._status[ptr] == _READY:
            self._status[ptr] = _READING
            self._last_read_idx = ptr
            self._r_ptr = (ptr + 1) % self.num_buffers
            return self._views[ptr]
        
        return None
    
    def force_get_view(self):
        """
        強制獲取當前讀取指針的視圖 (不論狀態)
        用於調試或特殊場景
        """
        return self._views[self._r_ptr]
