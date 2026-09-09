import micropython

_IDLE = micropython.const(0)
_READY = micropython.const(1)
_READING = micropython.const(2)

_HEAP_CAPS_AVAILABLE = False
_DMA_ALLOC = None
_DMA_FREE = None


def _import_heap_caps():
    """懶載入並快取 heap_caps 模組（全專案唯一碰 heap_caps 的地方）。
    成功後 _DMA_ALLOC / _DMA_FREE 指向 CAP_DMA 的 malloc/free。"""
    global _HEAP_CAPS_AVAILABLE, _DMA_ALLOC, _DMA_FREE
    if _HEAP_CAPS_AVAILABLE:
        return True
    try:
        import heap_caps
        _HEAP_CAPS_AVAILABLE = True
        _DMA_ALLOC = lambda sz: heap_caps.malloc(sz, heap_caps.CAP_DMA)
        _DMA_FREE = heap_caps.free
        return True
    except ImportError:
        _HEAP_CAPS_AVAILABLE = False
        _DMA_ALLOC = None
        _DMA_FREE = None
        return False


def alloc_dma(size):
    """優先 heap_caps CAP_DMA（內部 SRAM，跨 core / 週邊 DMA 一致性佳），
    不可用則 fallback bytearray。回傳 (buf, is_dma)：
    - buf 永遠可用（不是 None）
    - is_dma=True 表示需用 free_dma() 釋放；False 表示交給 GC 即可。

    這是全專案取得「DMA 記憶體」的統一入口（等價於 ring 的 try_dma 需求）。
    """
    if _import_heap_caps():
        try:
            buf = _DMA_ALLOC(size)
            if buf is not None:
                return buf, True
        except Exception:
            pass
    return bytearray(size), False


def free_dma(buf, is_dma):
    """釋放 alloc_dma 配出的 buf。is_dma=False 時 no-op（bytearray 交給 GC）。"""
    if buf is not None and is_dma and _DMA_FREE:
        try:
            _DMA_FREE(buf)
        except Exception:
            pass


class AtomicStreamHub:
    """單寫入者 / 單讀取者 (SPSC) 無鎖環形緩衝，供雙 CPU 以 TX/RX 一對 ring 交換資料。

    兩種使用模式（涵蓋所有場景）：
      • copy 模式：write_from(src) → read_into(dst)   — 整塊資料塞入/取出
      • view 模式：get_write_view()→commit()          — 直接在 slot 裡操作，零拷貝
                  get_read_view()→release_read()

    try_dma=True 時 slot 配在內部 SRAM（CAP_DMA），跨 core 讀寫更快且無 cache
    一致性問題；否則用普通 bytearray。
    """
    IDLE = _IDLE
    READY = _READY
    READING = _READING

    def __init__(self, size, num_buffers=3, try_dma=False):
        self._dma_bufs = None      # 僅記錄需 free_dma 的 slot（is_dma=True 者）
        self._bufs = []            # 底層 buffer（DMA 或 bytearray，一致為 memoryview）
        self._views = []

        dma_used = 0
        for i in range(num_buffers):
            buf, is_dma = alloc_dma(size) if try_dma else (bytearray(size), False)
            if is_dma:
                if self._dma_bufs is None:
                    self._dma_bufs = []
                self._dma_bufs.append(buf)
                dma_used += 1
            self._bufs.append(buf)
            self._views.append(memoryview(buf))

        self._status = [_IDLE] * num_buffers
        self._w_ptr = 0
        self._r_ptr = 0

        self.size = size
        self.num_buffers = num_buffers
        self._last_read_idx = -1
        self._dma_count = dma_used

        tag = " [DMA:{}]".format(dma_used) if dma_used > 0 else ""
        print("🚀 [BufferHub] Ready: {} KB total{}".format((size * num_buffers) // 1024, tag))

    @property
    def dirty(self):
        return self._status[self._r_ptr] == _READY

    @micropython.native
    def write_from(self, source):
        """copy 模式寫入：把 source 整塊複製進當前 slot。滿了回 False。"""
        ptr = self._w_ptr
        if self._status[ptr] != _IDLE:
            return False
        self._views[ptr][:] = source
        self._status[ptr] = _READY
        self._w_ptr = (ptr + 1) % self.num_buffers
        return True

    @micropython.native
    def read_into(self, target):
        """copy 模式讀取：把當前 slot 整塊複製進 target。空了回 False。"""
        if self._last_read_idx != -1:
            self._status[self._last_read_idx] = _IDLE
            self._last_read_idx = -1
        ptr = self._r_ptr
        if self._status[ptr] != _READY:
            return False
        target[:] = self._views[ptr]
        self._status[ptr] = _IDLE
        self._r_ptr = (ptr + 1) % self.num_buffers
        return True

    @micropython.native
    def flush(self):
        for i in range(self.num_buffers):
            self._status[i] = _IDLE
        self._w_ptr = 0
        self._r_ptr = 0
        self._last_read_idx = -1

    def get_fill_level(self):
        count = 0
        for s in self._status:
            if s == _READY:
                count += 1
        return count

    @micropython.native
    def get_write_view(self):
        """view 模式寫入：取出當前 slot 的 memoryview 直接寫，完事呼叫 commit()。
        滿了回 None。"""
        ptr = self._w_ptr
        if self._status[ptr] != _IDLE:
            return None
        return self._views[ptr]

    @micropython.native
    def commit(self):
        """把 get_write_view() 取出的 slot 標記為 READY 並推進寫指標。"""
        ptr = self._w_ptr
        if self._status[ptr] == _IDLE:
            self._status[ptr] = _READY
            self._w_ptr = (ptr + 1) % self.num_buffers

    @micropython.native
    def get_read_view(self):
        """view 模式讀取：取出當前 READY slot 的 memoryview 直接讀，完事呼叫
        release_read()。空了回 None。"""
        if self._last_read_idx != -1:
            self._status[self._last_read_idx] = _IDLE
            self._last_read_idx = -1
        ptr = self._r_ptr
        if self._status[ptr] == _READY:
            self._status[ptr] = _READING
            self._last_read_idx = ptr
            self._r_ptr = (ptr + 1) % self.num_buffers
            return self._views[ptr]
        return None

    @micropython.native
    def release_read(self):
        """歸還 get_read_view() 取出的 slot。"""
        if self._last_read_idx != -1:
            self._status[self._last_read_idx] = _IDLE
            self._last_read_idx = -1

    def force_get_view(self):
        return self._views[self._r_ptr]

    @property
    def dma_count(self):
        return self._dma_count

    def close(self):
        if self._dma_bufs:
            for b in self._dma_bufs:
                free_dma(b, True)
            self._dma_bufs = None
        self._bufs = []
        self._views = []
        self._status = []
        self._w_ptr = 0
        self._r_ptr = 0
        self._last_read_idx = -1
