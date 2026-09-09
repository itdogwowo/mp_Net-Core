# -*- coding: utf-8 -*-
"""SD 卡中央儲存管理器 — 檔案級別 API

內部管理 sector 與 alloc.json，外部只需檔名。
同時最多一個讀取 + 一個寫入 (兩個指標)。

用法:
  from lib.sys.fast_io import Storage

  s = Storage()

  s.write_begin("frame.jpk", total_bytes=102400)
  s.write(header_bytes)
  s.write(body_bytes)
  s.write_end()

  s.read_begin("frame.jpk")
  buf = bytearray(16384)
  while True:
      n = s.read_into(buf)
      if n == 0: break
      process(buf[:n])
  s.read_end()

  data = s.read_all("frame.jpk")
  s.list_files()
  s.remove("frame.jpk")
"""

import gc, _thread, time, ubinascii, json, os
from lib.sys.buffer_hub import alloc_dma, free_dma

BUF_SIZE = 16384
_sd_lock = _thread.allocate_lock()


class Allocator:
    def __init__(self, path="/sd/alloc.json", offset=None, sector_size=512):
        self._p = path; self._ss = sector_size
        self._off = offset or 0
        self._e = {}; self._d = False
        self._load()

    @classmethod
    def format(cls, sd, fat_mb=32):
        c = sd.info()[0]; ss = sd.info()[1]
        off = (fat_mb * 1048576) // ss
        import vfs_fat
        vfs_fat.mkfs(sd); os.mount(sd, "/sd")
        a = cls(offset=off); a.save()
        return a

    def append(self, name, cnt):
        tail = self._off
        for _, v in self._e.items(): tail = max(tail, v[0] + v[1])
        self._e[name] = (tail, cnt); self._d = True
        return tail

    def trim_from(self, name):
        if name not in self._e: return []
        s = self._e[name][0]; rem = []
        for n, v in list(self._e.items()):
            if v[0] >= s: del self._e[n]; rem.append(n)
        self._d = True; return rem

    def find(self, name):
        e = self._e.get(name)
        if e: return e
        for k, v in self._e.items():
            if k.endswith(name) or name.endswith(k): return v
        return None

    def list_files(self):
        return {k: {"sector": v[0], "count": v[1], "bytes": v[1] * self._ss}
                for k, v in self._e.items()}

    def save(self):
        if self._d: self._save()

    def _load(self):
        try:
            with open(self._p) as f: r = json.load(f)
            if "_offset" in r: self._off = r["_offset"]
            for k, v in r.items():
                if k.startswith("_") and not isinstance(v, list): continue
                self._e[k] = tuple(v[:3]) if len(v) >= 3 else (v[0], v[1])
        except Exception as e:
            print("alloc load err:", e)

    def _save(self):
        try:
            r = {"_version": 1, "_offset": self._off}
            for k, v in self._e.items(): r[k] = list(v)
            with open(self._p, "w") as f:
                json.dump(r, f); f.flush()
            self._d = False
        except Exception as e:
            print("save:", e)


def _sd():
    from lib.sys.sys_bus import bus
    s = bus.get_service("sd_raw")
    if s is None:
        raise RuntimeError("sd_raw not on bus")
    return s


class Storage:
    def __init__(self, sd=None, buf_size=BUF_SIZE):
        self._sd = sd or _sd()
        self._ss = self._sd.info()[1]
        self._alloc = Allocator()
        self._chunk = buf_size
        self._buf_bytes = buf_size
        self._io_buf = None
        self._io_buf_hc = False
        for sz in (buf_size, buf_size // 2 if buf_size >= 32768 else 0, 16384):
            if sz == 0: continue
            buf, is_dma = alloc_dma(sz)
            if is_dma:
                self._io_buf = buf; self._buf_bytes = sz; self._io_buf_hc = True; break
        if self._io_buf is None:
            self._io_buf = bytearray(buf_size)
        self._spc = self._buf_bytes // self._ss
        self._buf_size = self._buf_bytes
        # ── 固定 function 引用: 讀寫迴圈不再每次做 self._sd.xxx 屬性查找 ──
        self._wb = self._sd.writeblocks
        self._rb = self._sd.readblocks
        self._c = False; self._w_open = False; self._r_open = False
        self._w_file = None; self._w_sector = 0; self._w_cnt = 0
        self._w_byte = 0; self._w_total = 0
        self._r_file = None; self._r_sector = 0; self._r_cnt = 0
        self._r_byte = 0

    def write_begin(self, name, total_bytes):
        if self._w_open: raise RuntimeError("already writing")
        if self._r_open and self._r_file == name:
            raise RuntimeError("cannot write while reading same file")
        self._w_open = True; self._w_file = name; self._w_total = total_bytes
        self._w_cnt = (total_bytes + self._ss - 1) // self._ss
        self._w_byte = 0; self._w_crc = 0
        self._w_sector = self._alloc.append(self._w_file, self._w_cnt)
        self._alloc._e[self._w_file] = (self._w_sector, self._w_cnt, "FFFFFFFF")
        self._alloc.save()

    def write(self, data):
        if not self._w_open: raise RuntimeError("no active write")
        src = memoryview(data); total = len(src); p = 0
        buf = self._io_buf
        self._w_crc = ubinascii.crc32(src, self._w_crc)
        while p < total:
            n = min(total - p, self._buf_size)
            buf[:n] = src[p:p + n]
            sector = self._w_sector + self._w_byte // self._ss
            with _sd_lock: self._wb(sector, buf)
            self._w_byte += n; p += n
        if self._w_byte >= self._w_total: self.write_end()
        return p

    def write_end(self):
        if not self._w_open: return
        if self._w_sector == 0: self._w_open = False; return
        if self._w_byte > self._w_total:
            actual_cnt = (self._w_byte + self._ss - 1) // self._ss
            if actual_cnt > self._w_cnt:
                self._w_cnt = actual_cnt
                self._alloc._e[self._w_file] = (self._w_sector, self._w_cnt, "FFFFFFFF")
        crc_hex = "{:08X}".format(self._w_crc)
        self._alloc._e[self._w_file] = (self._w_sector, self._w_cnt, crc_hex)
        self._alloc.save(); self._w_open = False; self._w_file = None

    def read_begin(self, name):
        if self._r_open: raise RuntimeError("already reading")
        entry = self._alloc.find(name)
        if entry is None:
            raise RuntimeError("file not found: {}".format(name))
        # 先做所有可能丟例外的檢查，通過後才動狀態——避免失敗時 _r_open 卡 True
        crc = entry[2] if len(entry) >= 3 else None
        if crc == "FFFFFFFF":
            raise RuntimeError("file {} is incomplete (write interrupted)".format(name))
        self._r_open = True; self._r_file = name
        self._r_sector = entry[0]; self._r_cnt = entry[1]
        self._r_crc = crc
        self._r_byte = 0
        return self._r_cnt * self._ss

    def read_into(self, buf, off=0):
        if not self._r_open: return 0
        max_bytes = len(buf) - off
        if max_bytes <= 0: return 0
        remaining = self._r_cnt * self._ss - self._r_byte
        if remaining <= 0: return 0
        sector = self._r_sector + self._r_byte // self._ss
        n_sectors = min(self._spc, (remaining + self._ss - 1) // self._ss)
        with _sd_lock: self._rb(sector, self._io_buf)
        n_bytes = min(remaining, n_sectors * self._ss)
        n_bytes = min(n_bytes, max_bytes)
        buf[off:off + n_bytes] = self._io_buf[:n_bytes]
        self._r_byte += n_bytes
        return n_bytes

    def seek(self, offset):
        """設定讀取位置 (會對齊 sector 邊界)"""
        if not self._r_open: return
        max_byte = self._r_cnt * self._ss
        if offset < 0: offset = 0
        if offset > max_byte: offset = max_byte
        self._r_byte = (offset // self._ss) * self._ss

    def tell(self):
        """回傳目前讀取位置 (byte offset)"""
        if not self._r_open: return 0
        return self._r_byte

    def read_end(self):
        self._r_open = False; self._r_file = None

    def read_all(self, name):
        size = self.read_begin(name)
        data = bytearray(size); off = 0
        while True:
            n = self.read_into(data, off)
            if n == 0: break
            off += n
        self.read_end()
        return data[:off] if off < size else data

    def list_files(self):
        return self._alloc.list_files()

    def remove(self, name):
        self._alloc.trim_from(name); self._alloc.save()

    def write_file(self, name, data):
        self.write_begin(name, len(data)); self.write(data)

    def close(self):
        if self._c: return
        if self._w_open: self.write_end()
        if self._r_open: self.read_end()
        if self._io_buf is not None:
            # 只有 heap_caps 分配的才需 free；fallback bytearray 留給 GC
            free_dma(self._io_buf, getattr(self, "_io_buf_hc", False))
            self._io_buf = None
        self._c = True

    def __del__(self): self.close()


class StreamReader:
    def __init__(self, sd=None, buf_size=16384, n_bufs=2):
        from lib.sys.sys_bus import bus
        self._sd = sd or bus.get_service("sd_raw")
        self._ss = self._sd.info()[1]
        # ── 固定 function 引用: feed 迴圈不再每次屬性查找 ──
        self._rb = self._sd.readblocks
        dma = [None] * n_bufs
        hc = [False] * n_bufs
        for i in range(n_bufs):
            buf, is_dma = alloc_dma(buf_size)
            dma[i] = buf; hc[i] = is_dma
        self._bufs = dma; self._hc = hc; self._n = n_bufs
        self._w_idx = 0; self._r_idx = 0
        self._stat = [0] * n_bufs
        self._buf_size = buf_size; self._spc = buf_size // self._ss
        self._r_sector = 0; self._r_cnt = 0; self._r_byte = 0
        self._eof = False; self._started = False

    @property
    def chunk_sectors(self): return self._spc
    @property
    def chunk_bytes(self): return self._buf_size

    def start(self, alloc, name):
        e = alloc.find(name)
        if e is None: raise RuntimeError("file not found")
        self._r_sector = e[0]; self._r_cnt = e[1]
        self._r_byte = 0; self._eof = False; self._started = True

    def feed(self, sector):
        if self._eof: return False
        if self._stat[self._w_idx] != 0: return False
        self._rb(sector, self._bufs[self._w_idx])
        self._stat[self._w_idx] = 1
        self._w_idx = (self._w_idx + 1) % self._n
        return True

    def feed_all(self):
        sec = self._r_sector; rem = self._r_cnt
        while rem > 0:
            n = min(self._spc, rem)
            while not self.feed(sec): time.sleep_ms(1)
            sec += n; rem -= n
        self._eof = True

    def feed_done(self): self._eof = True

    def next(self):
        if not self._started: return None
        if self._stat[self._r_idx] != 1: return None
        self._r_byte += self._buf_size
        return memoryview(self._bufs[self._r_idx])

    def release(self):
        self._stat[self._r_idx] = 0
        self._r_idx = (self._r_idx + 1) % self._n

    def read_into(self, buf, off=0):
        v = self.next()
        if v is None: return 0
        n = min(len(v), len(buf) - off)
        buf[off:off + n] = v[:n]
        self.release(); return n

    def close(self):
        self._started = False
        bufs = getattr(self, "_bufs", None)
        if bufs:
            # 釋放 DMA 槽（bytearray 槽交給 GC）；清空後冪等
            for i in range(len(bufs)):
                free_dma(bufs[i], self._hc[i])
            self._bufs = []; self._hc = []
    def __del__(self): self.close()
