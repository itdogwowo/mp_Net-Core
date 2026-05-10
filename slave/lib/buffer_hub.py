import gc
import micropython

_IDLE = micropython.const(0)
_READY = micropython.const(1)
_READING = micropython.const(2)


@micropython.viper
def _viper_copy(dst, src, n: int):
    d = ptr8(dst)
    s = ptr8(src)
    for i in range(n):
        d[i] = s[i]


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
        self._last_read_idx = -1

        print("🚀 [BufferHub] Ready: {} KB total".format((size * num_buffers) // 1024))

    @property
    def dirty(self):
        return self._status[self._r_ptr] == _READY

    @micropython.native
    def write_from(self, source):
        ptr = self._w_ptr
        if self._status[ptr] != _IDLE:
            return False
        _viper_copy(self._views[ptr], source, self.size)
        self._status[ptr] = _READY
        self._w_ptr = (ptr + 1) % self.num_buffers
        return True

    @micropython.native
    def read_into(self, target):
        if self._last_read_idx != -1:
            self._status[self._last_read_idx] = _IDLE
            self._last_read_idx = -1
        ptr = self._r_ptr
        if self._status[ptr] != _READY:
            return False
        _viper_copy(target, self._views[ptr], self.size)
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
        ptr = self._w_ptr
        if self._status[ptr] != _IDLE:
            return None
        return self._views[ptr]

    @micropython.native
    def commit(self):
        ptr = self._w_ptr
        if self._status[ptr] == _IDLE:
            self._status[ptr] = _READY
            self._w_ptr = (ptr + 1) % self.num_buffers

    @micropython.native
    def get_read_view(self):
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

    def force_get_view(self):
        return self._views[self._r_ptr]
