"""
pixel_layout.py — 「混亂的群組選擇表」↔「整齊的硬體表」之間的快速對照表

兩張表：

  1. 混亂表（組合表）＝ 多套 mapping（map/*.json）+ 各 group 的 sel
     - 每個 mapping 用「有序段列表」選控制單元，書寫次序 = 像素次序，可交叉型別/反序
     - 群組引用 = mapping + group 兩段，各可用 id 或 name（可混用）
  2. 整齊表（硬體表）＝ PixelStreamer.big_buffer
     - 依 order（來自播放器，硬體真值）依序累加各型別所有 instance 的 count，
       形成全域 index 空間
     - 每顆 pixel 一個 RGBW cell（4 bytes：byte0=R, byte1=G, byte2=B, byte3=W）

本 lib 把「混亂表」預先展開成「全域 pixel index」，存 array('H')（uint16，上限
65535 顆）。執行期 scatter 用 @micropython.viper 的 ptr16(offs) + ptr8(buf) 做
零分配緊密迴圈；byte 落點在 viper 內 `index << 2` 求得（免費）。

實測（ESP32-S3，336 顆）：scatter 純 Python 10.37us/顆 → viper 0.43us/顆（約 24×）。
裝置效能測試見本檔 docstring 末的 bench 骨架。

不碰硬體、不碰 bus、不碰 pixel_stream。
"""

try:
    import micropython
    _MP = True
except ImportError:
    micropython = None
    _MP = False

from array import array as _array

_WRITE_CODES = {"rgb": 0, "w": 1, "ww": 2, "rgbw": 1}   # rgbw 單色階段 == w


def _clamp12(v):
    v = int(v)
    if v < 0:
        return 0
    if v > 4095:
        return 4095
    return v


def _parse_slice(s):
    """slice 字串 → (start, stop, step) 三元組。支援 '0:14' / '10:' / ':5' / ':' /
    '::2' / '1:9:3' / '-5:' / '::-1' / '15:10:-1'（Python 語義，end 不含）。

    刻意不用 slice()：部分 MicroPython build 不支援建立 slice 實例
    （實測 ESP32-S3 1.29 preview 報 can't create 'slice' instances）。
    """
    parts = s.split(":")

    def _i(x):
        x = x.strip()
        return None if x == "" else int(x)

    if len(parts) == 1:
        v = _i(parts[0])
        return (v, v + 1, None)
    if len(parts) == 2:
        return (_i(parts[0]), _i(parts[1]), None)
    if len(parts) == 3:
        return (_i(parts[0]), _i(parts[1]), _i(parts[2]))
    raise ValueError("非法 slice 字串: {!r}".format(s))


def _slice_indices(spec, count):
    """(start, stop, step) 三元組 + 長度 → index 列表（Python slice 語義）。"""
    start, stop, step = spec
    if step is None or step == 0:
        step = 1
    if step > 0:
        if start is None or start < 0:
            start = start + count if start is not None else 0
            if start < 0:
                start = 0
        if stop is None:
            stop = count
        elif stop < 0:
            stop = count + stop
        if stop > count:
            stop = count
        if start >= stop:
            return []
        return list(range(start, stop, step))
    # step < 0（反序）
    if start is None:
        start = count - 1
    elif start < 0:
        start = count + start
    if stop is None:
        stop = -1
    elif stop < 0:
        stop = count + stop
    if start <= stop:
        return []
    return list(range(start, stop, step))


def _expand_selector(spec, count):
    """一個選擇器 → 型別內 index 列表。spec：int（單顆）或 slice 字串。"""
    if isinstance(spec, int):
        idx = spec if spec >= 0 else count + spec
        return [idx] if 0 <= idx < count else []
    if isinstance(spec, str):
        return _slice_indices(_parse_slice(spec), count)
    raise ValueError("選擇器型別不受支援: {!r}".format(spec))


# === scatter 熱路徑（viper / 純 Python 雙路徑）==============
#
# 值流語義（array('H')，0-4095，通道流）——操作模式 = 值流的消費形狀，不猜設備：
#   單通道（每顆 1 值，獨立操作單一通道，跟單色燈同語義）：
#     r / g / b / w : 只寫對應通道，其餘通道「不修改」（保留原值，可累加組合）
#     ww            : cell (0,0,low8,high4)，12-bit 完整
#   多通道（每顆多值，各通道獨立）：
#     rgb : 3 值（R,G,B）→ cell (R,G,B,0)，全部 >>4
#     rgbw: 4 值（R,G,B,W）→ cell (R,G,B,W)，全部 >>4
#   wwww : 每顆 1 值 → 一個數值代表整顆 pixel，4 個 byte 全寫同值（>>4）。
#          「全部給我更新」：scatter 不做通道語義，設備自行限制範圍
#          （PixelController._convert 依 _tid 取它要的 byte）。
# 保底（對齊舊專案 idx % len）：
#   值流不足 → 取模循環重用；值流過長 → 只取前 nv 個（多餘丟棄）
#   值流空（nv=0）→ 對應通道寫 0（避免 %0）

if _MP:

    @micropython.viper
    def _scatter_r(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            # 只寫 R，其餘通道不修改（保留原值，可與 g/b 累加）
            if nv < 1:
                pb[o] = 0
            else:
                pb[o] = pv[k % nv] >> 4

    @micropython.viper
    def _scatter_g(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            # 只寫 G，其餘通道不修改
            if nv < 1:
                pb[o + 1] = 0
            else:
                pb[o + 1] = pv[k % nv] >> 4

    @micropython.viper
    def _scatter_b(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            # 只寫 B，其餘通道不修改
            if nv < 1:
                pb[o + 2] = 0
            else:
                pb[o + 2] = pv[k % nv] >> 4

    @micropython.viper
    def _scatter_w(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            # 只寫 W，其餘通道不修改
            if nv < 1:
                pb[o + 3] = 0
            else:
                pb[o + 3] = pv[k % nv] >> 4

    @micropython.viper
    def _scatter_ww(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            pb[o] = 0
            pb[o + 1] = 0
            if nv < 1:
                pb[o + 2] = 0
                pb[o + 3] = 0
            else:
                v = pv[k % nv]
                pb[o + 2] = v & 0xFF
                pb[o + 3] = v >> 8

    @micropython.viper
    def _scatter_rgb(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            if nv < 1:
                pb[o] = 0
                pb[o + 1] = 0
                pb[o + 2] = 0
                pb[o + 3] = 0
            else:
                pb[o] = pv[(3 * k) % nv] >> 4
                pb[o + 1] = pv[(3 * k + 1) % nv] >> 4
                pb[o + 2] = pv[(3 * k + 2) % nv] >> 4
                pb[o + 3] = 0

    @micropython.viper
    def _scatter_rgbw(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            if nv < 1:
                pb[o] = 0
                pb[o + 1] = 0
                pb[o + 2] = 0
                pb[o + 3] = 0
            else:
                pb[o] = pv[(4 * k) % nv] >> 4
                pb[o + 1] = pv[(4 * k + 1) % nv] >> 4
                pb[o + 2] = pv[(4 * k + 2) % nv] >> 4
                pb[o + 3] = pv[(4 * k + 3) % nv] >> 4

    @micropython.viper
    def _scatter_wwww(buf, offs, vals, n: int, nv: int):
        pb = ptr8(buf)
        po = ptr16(offs)
        pv = ptr16(vals)
        for k in range(n):
            o = int(po[k]) << 2
            if nv < 1:
                pb[o] = 0
                pb[o + 1] = 0
                pb[o + 2] = 0
                pb[o + 3] = 0
            else:
                v8 = pv[k % nv] >> 4
                pb[o] = v8
                pb[o + 1] = v8
                pb[o + 2] = v8
                pb[o + 3] = v8

else:

    def _scatter_r(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            # 只寫 R，其餘通道不修改
            buf[o] = (vals[k % nv] >> 4) if nv > 0 else 0

    def _scatter_g(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            # 只寫 G，其餘通道不修改
            buf[o + 1] = (vals[k % nv] >> 4) if nv > 0 else 0

    def _scatter_b(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            # 只寫 B，其餘通道不修改
            buf[o + 2] = (vals[k % nv] >> 4) if nv > 0 else 0

    def _scatter_w(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            # 只寫 W，其餘通道不修改
            buf[o + 3] = (vals[k % nv] >> 4) if nv > 0 else 0

    def _scatter_ww(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            buf[o] = 0
            buf[o + 1] = 0
            if nv < 1:
                buf[o + 2] = 0
                buf[o + 3] = 0
            else:
                v = vals[k % nv]
                buf[o + 2] = v & 0xFF
                buf[o + 3] = v >> 8

    def _scatter_rgb(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            if nv < 1:
                buf[o] = 0
                buf[o + 1] = 0
                buf[o + 2] = 0
                buf[o + 3] = 0
            else:
                buf[o] = vals[(3 * k) % nv] >> 4
                buf[o + 1] = vals[(3 * k + 1) % nv] >> 4
                buf[o + 2] = vals[(3 * k + 2) % nv] >> 4
                buf[o + 3] = 0

    def _scatter_rgbw(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            if nv < 1:
                buf[o] = 0
                buf[o + 1] = 0
                buf[o + 2] = 0
                buf[o + 3] = 0
            else:
                buf[o] = vals[(4 * k) % nv] >> 4
                buf[o + 1] = vals[(4 * k + 1) % nv] >> 4
                buf[o + 2] = vals[(4 * k + 2) % nv] >> 4
                buf[o + 3] = vals[(4 * k + 3) % nv] >> 4

    def _scatter_wwww(buf, offs, vals, n, nv):
        for k in range(n):
            o = offs[k] << 2
            if nv < 1:
                buf[o] = 0
                buf[o + 1] = 0
                buf[o + 2] = 0
                buf[o + 3] = 0
            else:
                v8 = vals[k % nv] >> 4
                buf[o] = v8
                buf[o + 1] = v8
                buf[o + 2] = v8
                buf[o + 3] = v8


_SCATTER = {"r": _scatter_r, "g": _scatter_g, "b": _scatter_b,
            "w": _scatter_w, "ww": _scatter_ww,
            "rgb": _scatter_rgb, "rgbw": _scatter_rgbw,
            "wwww": _scatter_wwww}


def _encode_cell(buf, byte_off, value, write):
    """值 → RGBW cell（單顆，供 set_value 用）。

    r/g/b/w/ww/wwww 接受單值；rgb 接受 3 值 (R,G,B)；rgbw 接受 4 值 (R,G,B,W)。
    wwww 的單值 = 一整個 pixel，4 個 byte 全寫同值（>>4）。
    """
    if write == "rgb":
        r, g, b = (_clamp12(x) for x in value)
        buf[byte_off] = r >> 4
        buf[byte_off + 1] = g >> 4
        buf[byte_off + 2] = b >> 4
        buf[byte_off + 3] = 0
    elif write == "rgbw":
        r, g, b, w = (_clamp12(x) for x in value)
        buf[byte_off] = r >> 4
        buf[byte_off + 1] = g >> 4
        buf[byte_off + 2] = b >> 4
        buf[byte_off + 3] = w >> 4
    elif write == "r":
        # 只寫 R，其餘通道不修改
        buf[byte_off] = _clamp12(value) >> 4
    elif write == "g":
        # 只寫 G，其餘通道不修改
        buf[byte_off + 1] = _clamp12(value) >> 4
    elif write == "b":
        # 只寫 B，其餘通道不修改
        buf[byte_off + 2] = _clamp12(value) >> 4
    elif write == "w":
        # 只寫 W，其餘通道不修改
        buf[byte_off + 3] = _clamp12(value) >> 4
    elif write == "ww":
        v = _clamp12(value)
        buf[byte_off] = 0
        buf[byte_off + 1] = 0
        buf[byte_off + 2] = v & 0xFF
        buf[byte_off + 3] = v >> 8
    elif write == "wwww":
        v8 = _clamp12(value) >> 4
        buf[byte_off] = v8
        buf[byte_off + 1] = v8
        buf[byte_off + 2] = v8
        buf[byte_off + 3] = v8
    else:
        raise ValueError("未知 write: {!r}".format(write))


def _decode_cell(buf, byte_off, write):
    """RGBW cell → 值（供 get_value 用）。rgb 回傳 3 值，rgbw 回傳 4 值，
    r/g/b/w/ww/wwww 回傳單值。"""
    if write == "rgb":
        return ((buf[byte_off] & 0xFF) << 4,
                (buf[byte_off + 1] & 0xFF) << 4,
                (buf[byte_off + 2] & 0xFF) << 4)
    elif write == "rgbw":
        return ((buf[byte_off] & 0xFF) << 4,
                (buf[byte_off + 1] & 0xFF) << 4,
                (buf[byte_off + 2] & 0xFF) << 4,
                (buf[byte_off + 3] & 0xFF) << 4)
    elif write == "r":
        return (buf[byte_off] & 0xFF) << 4
    elif write == "g":
        return (buf[byte_off + 1] & 0xFF) << 4
    elif write == "b":
        return (buf[byte_off + 2] & 0xFF) << 4
    elif write == "w":
        return (buf[byte_off + 3] & 0xFF) << 4
    elif write == "ww":
        return (buf[byte_off + 2] & 0xFF) | ((buf[byte_off + 3] & 0x0F) << 8)
    elif write == "wwww":
        return (buf[byte_off] & 0xFF) << 4
    raise ValueError("未知 write: {!r}".format(write))


class PixelLayout:
    """兩張表的橋樑：多套 mapping（群組選擇）→ big_buffer 落點 + 快速散射。

    硬體骨架（order/counts/type_offsets）全域唯一，所有 mapping 共用同一張
    index 空間（單一真源）；每個 mapping 各自展開 groups，書寫次序 = 像素次序。
    群組引用 = mapping + group 兩段，各可用 id 或 name（可混用）。
    """

    def __init__(self, order, counts, instance_counts=None):
        """
        order  : 型別名列表（來自播放器 PixelStreamer.controllers，硬體真值）
        counts : {型別名: 該型別總像素數}（所有 instance 的 Q 加總）
        instance_counts : {型別名: [各 instance 的像素數, ...]}（可選，供 controller 對照）
        """
        self.order = list(order)

        if instance_counts is None:
            self.instance_counts = {t: [int(counts.get(t, 0))] for t in self.order}
        else:
            self.instance_counts = {}
            for t in self.order:
                lst = [int(x) for x in instance_counts.get(t, [])]
                self.instance_counts[t] = lst if lst else [int(counts.get(t, 0))]

        self.counts = {t: sum(self.instance_counts[t]) for t in self.order}

        self.type_offsets = {}
        off = 0
        for t in self.order:
            self.type_offsets[t] = off
            off += self.counts[t]
        self.total_pixels = off

        self._mappings = {}  # mid -> {"name", "groups": {gid: {"name","indices"}}, "gnames": {name: gid}}
        self._mnames = {}    # mapping name -> mid

    @classmethod
    def from_registry(cls, registry, order, counts, instance_counts=None):
        """載入 registry（{"mappings": [...]}）+ 硬體 order/counts，註冊全部 mapping。

        任一 mapping 重複 / 群組重複 → raise ValueError（呼叫端決定跳過或中止）。
        """
        lay = cls(order, counts, instance_counts)
        for m in registry.get("mappings", []):
            lay.register_mapping(m["id"], m["name"], m.get("groups", []))
        return lay

    def register_mapping(self, mid, name, groups):
        """註冊一套 mapping：展開其 groups → 全域 index（array('H')）。回傳總像素數。

        groups = [{"id", "name", "sel": [{"type", "sel"}, ...]}, ...]
        群組 id/name 在此 mapping 內必須唯一（重複 raise，不註冊）。
        sel 是「有序的段列表」：段依書寫順序拼接，可交叉型別、反序、重疊。
        引用硬體不存在的型別（如 pwm / uartMotor1 未進播放器）→ 該段為空，不 raise。
        """
        mid = int(mid)
        if mid in self._mappings:
            raise ValueError("MAPPING ID CONFLICT: id={}".format(mid))
        if name in self._mnames:
            raise ValueError("MAPPING NAME CONFLICT: name={}".format(name))

        gtable = {}
        gnames = {}
        for g in groups:
            gid = int(g["id"])
            gname = g["name"]
            if gid in gtable:
                raise ValueError("GROUP ID CONFLICT: mapping={} group id={}".format(name, gid))
            if gname in gnames:
                raise ValueError("GROUP NAME CONFLICT: mapping={} group name={}".format(name, gname))

            global_idx = []
            for seg in g["sel"]:
                t = seg["type"]
                if isinstance(t, int):
                    t = self.order[t]
                if t not in self.counts:
                    continue  # 硬體無此型別 → 該段為空（誠實反映，warn 由載入端做）
                base = self.type_offsets[t]
                cnt = self.counts[t]
                for local in _expand_selector(seg["sel"], cnt):
                    global_idx.append(base + local)

            if global_idx and max(global_idx) > 0xFFFF:
                raise ValueError("全域 pixel index 超過 65535，改用 array('I')")

            gtable[gid] = {"name": gname, "indices": _array('H', global_idx)}
            gnames[gname] = gid

        self._mappings[mid] = {"name": name, "groups": gtable, "gnames": gnames}
        self._mnames[name] = mid
        return sum(len(g["indices"]) for g in gtable.values())

    def _mid(self, ref):
        return self._mnames[ref] if isinstance(ref, str) else int(ref)

    def _gid(self, mref, gref):
        m = self._mappings[self._mid(mref)]
        return m["gnames"][gref] if isinstance(gref, str) else int(gref)

    def _indices(self, mref, gref):
        return self._mappings[self._mid(mref)]["groups"][self._gid(mref, gref)]["indices"]

    def resolve_group(self, mref, gref):
        """mapping + group（id/name 可混用）→ (mid, gid)。找不到 raise KeyError/ValueError。"""
        mid = self._mid(mref)
        return mid, self._gid(mid, gref)

    # ── 定址查詢（除錯用，回傳 list）──────────────

    def mappings(self):
        """已註冊 mapping：{mid: name}。"""
        return {mid: m["name"] for mid, m in self._mappings.items()}

    def groups(self, mref):
        """某 mapping 的群組：{gid: name}。"""
        m = self._mappings[self._mid(mref)]
        return {gid: g["name"] for gid, g in m["groups"].items()}

    def global_indices(self, mref, gref):
        """mapping + group（各可用 id 或 name）→ 全域 pixel index 列表（除錯用）。"""
        return list(self._indices(mref, gref))

    def byte_offsets(self, mref, gref):
        """mapping + group → big_buffer 的 byte 落點列表（除錯用）。"""
        return [i << 2 for i in self._indices(mref, gref)]

    def pixel_count(self, mref, gref):
        """mapping + group → 選出的像素數（應 == effect 的 pixel_n）。"""
        return len(self._indices(mref, gref))

    def sub_offsets(self, mref, gref, spec):
        """群組內範圍（slice 字串，Python 語義，end 不含）→ 子 offsets array('H')。

        範圍是「群組選出次序」的相對範圍（對齊 set_value/get_value 的 k 語義），
        供 mode map 條目的 "range" 使用：同一群組可拆成多段配不同效果。
        """
        idx = self._indices(mref, gref)
        sel = _slice_indices(_parse_slice(str(spec)), len(idx))
        return _array('H', (idx[k] for k in sel))

    # ── 執行期操作──────────────

    def scatter(self, big_buffer, mref, gref, values, write):
        """把效果通道流依 write 散射進 big_buffer（每幀的緊密熱路徑）。

        mref / gref : mapping + group（各可用 id 或 name，可混用）
        values 必須是 array('H') 或 memoryview（viper 需要 buffer 協議）。
        操作模式 = 值流消費形狀（不猜設備）：
          r/g/b/w/ww 1 值/顆、rgb 3 值/顆、rgbw/wwww 4 值/顆。
          r/g/b/w 只寫對應通道，其餘「不修改」。
        保底：值流不足 → 取模循環；過長 → 多餘丟棄；空 → 對應通道寫 0。
        """
        self.scatter_offs(big_buffer, self._indices(mref, gref), values, write)

    def scatter_offs(self, big_buffer, offs, values, write):
        """用預先算好的 offsets 散射（mode range 子範圍用；offs 須為 array('H')）。"""
        n = len(offs)
        if _MP and not (isinstance(values, _array) or isinstance(values, memoryview)):
            raise TypeError("scatter 需要 array('H') 或 memoryview，而非 list")
        _SCATTER[write](big_buffer, offs, values, n, len(values))

    def set_value(self, big_buffer, mref, gref, k, value, write):
        """設群組內第 k 顆的值（直接寫 big_buffer）。k 以群組選出次序為準。

        rgb 需傳 3 值 (R,G,B)；rgbw 傳 4 值；r/g/b/w/ww/wwww 傳單值。
        r/g/b/w 只寫對應通道，其餘通道不修改。
        """
        offs = self._indices(mref, gref)
        if not 0 <= k < len(offs):
            raise IndexError("群組內 index 越界")
        _encode_cell(big_buffer, offs[k] << 2, value, write)

    def get_value(self, big_buffer, mref, gref, k, write):
        """讀群組內第 k 顆的值（依 write 解碼）。rgb 回傳 3 值，w/ww 回傳單值。"""
        offs = self._indices(mref, gref)
        if not 0 <= k < len(offs):
            raise IndexError("群組內 index 越界")
        return _decode_cell(big_buffer, offs[k] << 2, write)

    # ── 整齊表：controller 對照（PixelStreamer 取 offsets 的單一真源）──────────────

    def controller_offset(self, type_name, instance_idx):
        """某型別第 instance_idx 個 instance 的全域起點（pixel index）。"""
        if type_name not in self.instance_counts:
            raise ValueError("未知型別: {!r}".format(type_name))
        return self.type_offsets[type_name] + sum(
            self.instance_counts[type_name][:instance_idx])

    def controller_offsets(self, specs):
        """specs = [(型別名, instance_idx), ...] 依 controller 順序 → 各全域起點。"""
        return [self.controller_offset(t, i) for t, i in specs]


if __name__ == "__main__":
    # ── PC 快速自檢（不依賴硬體）────────────────────────
    registry = {
        "version": 1,
        "mappings": [
            {"id": 1, "name": "gundam", "groups": [
                {"id": 1, "name": "gundam_body", "sel": [
                    {"type": "pwm",    "sel": "10:15"},
                    {"type": "ws2812", "sel": "40:200"},
                    {"type": "ws2812", "sel": ":10"},
                    {"type": "pwm",    "sel": "15:10:-1"},
                ]},
                {"id": 2, "name": "motors", "sel": [
                    {"type": "uartMotor1", "sel": ":"},
                ]},
            ]},
            {"id": 2, "name": "test", "groups": [
                {"id": 1, "name": "full", "sel": [
                    {"type": "apa102",    "sel": ":"},
                    {"type": "ws2812",    "sel": ":"},
                    {"type": "pca9685",   "sel": ":"},
                    {"type": "pwm",       "sel": ":"},
                    {"type": "uartMotor1", "sel": ":"},
                ]},
            ]},
        ],
    }
    order = ["apa102", "ws2812", "pca9685", "pwm", "uartMotor1"]  # 來自播放器（硬體真值）
    counts = {"apa102": 100, "ws2812": 200, "pca9685": 16,
              "pwm": 20, "uartMotor1": 4}
    instance_counts = {"apa102": [60, 40], "ws2812": [120, 80]}

    lay = PixelLayout.from_registry(registry, order, counts, instance_counts)

    # 整齊表：型別 offset 依 order 累加 count
    assert lay.type_offsets == {"apa102": 0, "ws2812": 100, "pca9685": 300,
                                "pwm": 316, "uartMotor1": 336}
    assert lay.total_pixels == 340

    # controller 對照（單一真源）：apa102 兩顆 instance 的起點
    assert lay.controller_offset("apa102", 0) == 0
    assert lay.controller_offset("apa102", 1) == 60
    assert lay.controller_offsets([("apa102", 0), ("apa102", 1)]) == [0, 60]

    # 多 mapping + 複合引用（mapping.group，id/name 可混用）
    assert lay.mappings() == {1: "gundam", 2: "test"}
    assert lay.groups("gundam") == {1: "gundam_body", 2: "motors"}
    assert lay.groups(2) == {1: "full"}

    assert lay.pixel_count("gundam", "gundam_body") == 180   # 5 + 160 + 10 + 5
    assert lay.pixel_count("gundam", 2) == 4                 # motors
    assert lay.pixel_count(1, "motors") == 4                 # 混用
    assert lay.pixel_count(2, 1) == 340                      # test.full
    assert lay.pixel_count("test", "full") == 340

    gi = lay.global_indices(1, "gundam_body")
    assert gi[0:5] == [326, 327, 328, 329, 330]
    assert gi[5] == 140 and gi[164] == 299
    assert gi[165] == 100 and gi[174] == 109
    assert gi[175:180] == [331, 330, 329, 328, 327]

    # 重複檢查：同 mapping 內 group id/name 重複、mapping id/name 重複 → raise
    def _raises(fn):
        try:
            fn()
        except ValueError:
            return True
        return False

    assert _raises(lambda: lay.register_mapping(9, "dup", [
        {"id": 1, "name": "a", "sel": []},
        {"id": 1, "name": "b", "sel": []}]))                      # group id 重複
    assert _raises(lambda: lay.register_mapping(9, "dup2", [
        {"id": 1, "name": "a", "sel": []},
        {"id": 2, "name": "a", "sel": []}]))                      # group name 重複
    assert _raises(lambda: lay.register_mapping(1, "x", []))      # mapping id 重複
    assert _raises(lambda: lay.register_mapping(9, "gundam", [])) # mapping name 重複

    # 未知型別（硬體無此型別）→ 空段不 raise（誠實反映）
    lay2 = PixelLayout.from_registry({"mappings": [
        {"id": 1, "name": "ghost", "groups": [
            {"id": 1, "name": "g", "sel": [{"type": "pwm", "sel": ":"}]},
        ]},
    ]}, ["ws2812"], {"ws2812": 10})
    assert lay2.pixel_count(1, 1) == 0

    # ── scatter 通道流語義 ──
    # rgb：每顆 3 值 (R,G,B)；w / ww：每顆 1 值
    buf = bytearray(lay.total_pixels * 4)

    # rgb 精確通道流（gundam.motors 4 顆 → 12 個通道值）
    lay.scatter(buf, "gundam", "motors",
                _array('H', [255, 0, 0, 0, 255, 0, 0, 0, 255, 4095, 2048, 1024]), "rgb")
    assert buf[1344:1348] == bytes([15, 0, 0, 0])      # 第 0 顆 R=255
    assert buf[1348:1352] == bytes([0, 15, 0, 0])      # 第 1 顆 G=255
    assert buf[1352:1356] == bytes([0, 0, 15, 0])      # 第 2 顆 B=255
    assert buf[1356:1360] == bytes([255, 128, 64, 0])  # 第 3 顆 (4095,2048,1024)

    # w：每顆 1 值
    lay.scatter(buf, 1, 2, _array('H', [4095, 2048, 100, 0]), "w")
    assert buf[1344 + 3] == 255
    assert buf[1348 + 3] == 128
    assert buf[1352 + 3] == 6
    assert buf[1356 + 3] == 0

    # ww：12-bit 完整
    lay.scatter(buf, "gundam", "motors", _array('H', [4095, 0, 0, 0]), "ww")
    assert buf[1344] == 0 and buf[1345] == 0
    assert buf[1346] == 255 and buf[1347] == 15

    # rgbw：4 顆 → 16 通道值，各 >>4 進 R,G,B,W
    lay.scatter(buf, "gundam", "motors",
                _array('H', [255, 0, 0, 4095,
                             0, 255, 0, 4095,
                             0, 0, 255, 4095,
                             4095, 2048, 1024, 512]), "rgbw")
    assert buf[1344:1348] == bytes([15, 0, 0, 255])
    assert buf[1348:1352] == bytes([0, 15, 0, 255])
    assert buf[1352:1356] == bytes([0, 0, 15, 255])
    assert buf[1356:1360] == bytes([255, 128, 64, 32])

    # wwww：1 值/顆，一個數值代表整顆 pixel，4 個 byte 全寫同值（>>4）
    lay.scatter(buf, "gundam", "motors", _array('H', [4095, 2048, 100, 0]), "wwww")
    assert buf[1344:1348] == bytes([255, 255, 255, 255])
    assert buf[1348:1352] == bytes([128, 128, 128, 128])
    assert buf[1352:1356] == bytes([6, 6, 6, 6])
    assert buf[1356:1360] == bytes([0, 0, 0, 0])

    # r/g/b/w：只寫對應通道，其餘通道「不修改」（保留原值，可累加組合）
    # 先寫一個完整 cell（RGBW），再用 r/g/b/w 覆寫單通道，驗證其他通道保留
    lay.scatter(buf, "gundam", "motors", _array('H', [255, 255, 255, 255,
                                                      0, 0, 0, 0,
                                                      0, 0, 0, 0,
                                                      0, 0, 0, 0]), "rgbw")
    assert buf[1344:1348] == bytes([15, 15, 15, 15])   # 第 0 顆完整 (R,G,B,W)

    lay.scatter(buf, "gundam", "motors", _array('H', [4095, 0, 0, 0]), "r")
    assert buf[1344:1348] == bytes([255, 15, 15, 15])  # 只改 R，G/B/W 保留
    lay.scatter(buf, "gundam", "motors", _array('H', [2048, 0, 0, 0]), "g")
    assert buf[1344:1348] == bytes([255, 128, 15, 15]) # 只改 G
    lay.scatter(buf, "gundam", "motors", _array('H', [100, 0, 0, 0]), "b")
    assert buf[1344:1348] == bytes([255, 128, 6, 15])  # 只改 B
    lay.scatter(buf, "gundam", "motors", _array('H', [512, 0, 0, 0]), "w")
    assert buf[1344:1348] == bytes([255, 128, 6, 32])  # 只改 W

    # ── 保底機制（不足 / 過長 / 空）──────────────
    # 不足：motors 4 顆但只給 2 值 → 取模循環
    lay.scatter(buf, "gundam", "motors", _array('H', [4095, 0]), "w")
    assert buf[1344 + 3] == 255 and buf[1348 + 3] == 0
    assert buf[1352 + 3] == 255 and buf[1356 + 3] == 0

    # 過長：motors 4 顆需 12 個 rgb 通道值，給 14 個 → 只取前 12，多餘丟棄
    lay.scatter(buf, "gundam", "motors",
                _array('H', [100, 200, 300, 400, 500, 600, 700, 800,
                             900, 1000, 1100, 1200, 9999, 8888]), "rgb")
    assert buf[1344:1348] == bytes([100 >> 4, 200 >> 4, 300 >> 4, 0])
    assert buf[1348:1352] == bytes([400 >> 4, 500 >> 4, 600 >> 4, 0])
    assert buf[1352:1356] == bytes([700 >> 4, 800 >> 4, 900 >> 4, 0])
    assert buf[1356:1360] == bytes([1000 >> 4, 1100 >> 4, 1200 >> 4, 0])  # 9999/8888 丟棄

    # 空：0 值 → 全寫 0
    lay.scatter(buf, "gundam", "motors", _array('H', []), "rgb")
    assert buf[1344:1360] == bytes(16)

    # ── set_value / get_value（單顆操作面）────────
    lay.set_value(buf, "gundam", "motors", 0, 2048, "w")
    assert lay.get_value(buf, "gundam", "motors", 0, "w") == 2048
    lay.set_value(buf, "gundam", "motors", 1, 4095, "ww")
    assert lay.get_value(buf, "gundam", "motors", 1, "ww") == 4095
    lay.set_value(buf, "gundam", "motors", 2, (100, 200, 300), "rgb")
    assert lay.get_value(buf, "gundam", "motors", 2, "rgb") == (96, 192, 288)  # >>4 量化
    lay.set_value(buf, "gundam", "motors", 3, (100, 200, 300, 400), "rgbw")
    assert lay.get_value(buf, "gundam", "motors", 3, "rgbw") == (96, 192, 288, 400)
    lay.set_value(buf, "gundam", "motors", 0, 2048, "wwww")
    assert lay.get_value(buf, "gundam", "motors", 0, "wwww") == 2048
    lay.set_value(buf, "gundam", "motors", 1, 1000, "r")
    assert lay.get_value(buf, "gundam", "motors", 1, "r") == 992   # 1000>>4=62, 62<<4=992
    lay.set_value(buf, "gundam", "motors", 2, 2000, "g")
    assert lay.get_value(buf, "gundam", "motors", 2, "g") == 2000  # 2000>>4=125, 125<<4=2000
    lay.set_value(buf, "gundam", "motors", 3, 3000, "b")
    assert lay.get_value(buf, "gundam", "motors", 3, "b") == 2992  # 3000>>4=187, 187<<4=2992

    # slice 語法單元
    assert _expand_selector("::-1", 3) == [2, 1, 0]
    assert _expand_selector("1:9:3", 10) == [1, 4, 7]
    assert _expand_selector("-5:", 10) == [5, 6, 7, 8, 9]
    assert _expand_selector("15:10:-1", 20) == [15, 14, 13, 12, 11]

    print("OK — PixelLayout（多 mapping + 複合引用 + 重複檢查 + scatter 保底 + set/get）驗證通過")


# ── 裝置效能測試骨架（PC 跑不了 viper，請 flash 到 ESP32 測）──────────────
#
#   注意：scatter 現在是「通道流」語義（rgb 3 值/顆），且簽名含 nv（保底）。
#   bench 統一用 w（1 值/顆）來對照，最接近舊的單值測法。
#
#   from array import array
#   import time, gc, micropython
#   N = 336
#   offs = array('H', [i % N for i in range(N)])
#   vals = array('H', [i & 0xFFF for i in range(N)])
#   buf  = bytearray(N * 4)
#
#   def bench(fn, loops=1000):
#       gc.collect()
#       t0 = time.ticks_us()
#       for _ in range(loops):
#           fn(buf, offs, vals, N, N)
#       return time.ticks_diff(time.ticks_us(), t0) / loops
#
#   # 純 Python 基線（對照，w 語義）
#   def scatter_py(buf, offs, vals, n, nv):
#       for k in range(n):
#           v8 = vals[k % nv] >> 4
#           o = offs[k] << 2
#           buf[o] = 0; buf[o+1] = 0; buf[o+2] = 0; buf[o+3] = v8
#
#   print("py", bench(scatter_py))
#   print("vp", bench(_scatter_w))
#
# 實測（舊單值版）：py 10.37us/顆 → vp 0.43us/顆（24×）
