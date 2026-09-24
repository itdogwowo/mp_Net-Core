import struct
import sys

IS_MICROPYTHON = (sys.implementation.name == 'micropython')

if not IS_MICROPYTHON:
    class micropython:
        @staticmethod
        def viper(f): return f
        @staticmethod
        def native(f): return f
    ptr8 = bytes
    ptr16 = bytes
    int32 = int
    uint16 = int
else:
    import micropython


@micropython.viper
def _viper_decode(dispatch_buf, field_buf, cmd_id: int, payload, plen: int, out_buf):
    db = ptr8(dispatch_buf)
    fb = ptr8(field_buf)
    src = ptr8(payload)
    dst = ptr8(out_buf)

    nd = int(len(dispatch_buf)) >> 3
    fs = 0
    fc = 0
    found = 0
    for di in range(nd):
        doff = di << 3
        cid = db[doff] | (db[doff + 1] << 8)
        if cid == cmd_id:
            fs = db[doff + 2] | (db[doff + 3] << 8)
            fc = db[doff + 4]
            found = 1
            break

    if found == 0:
        fc = 0

    pos = 0
    for i in range(fc):
        fi = (fs + i) << 1
        tc = fb[fi]
        extra = fb[fi + 1]

        ooff = i << 3
        dst[ooff] = tc

        if pos >= plen and tc != 5:
            break

        if tc == 0:
            if pos < plen:
                dst[ooff + 2] = src[pos]
                pos += 1
        elif tc == 1:
            if pos + 1 < plen:
                dst[ooff + 2] = src[pos]
                dst[ooff + 3] = src[pos + 1]
                pos += 2
        elif tc == 2:
            if pos + 3 < plen:
                dst[ooff + 2] = src[pos]
                dst[ooff + 3] = src[pos + 1]
                dst[ooff + 4] = src[pos + 2]
                dst[ooff + 5] = src[pos + 3]
                pos += 4
        elif tc == 3:
            if pos + 1 < plen:
                slen = src[pos] | (src[pos + 1] << 8)
                pos += 2
                off = pos
                dst[ooff + 2] = off & 0xFF
                dst[ooff + 3] = (off >> 8) & 0xFF
                dst[ooff + 4] = (off >> 16) & 0xFF
                dst[ooff + 5] = (off >> 24) & 0xFF
                dst[ooff + 6] = slen & 0xFF
                dst[ooff + 7] = (slen >> 8) & 0xFF
                pos += slen
        elif tc == 4:
            off = pos
            dst[ooff + 2] = off & 0xFF
            dst[ooff + 3] = (off >> 8) & 0xFF
            dst[ooff + 4] = (off >> 16) & 0xFF
            dst[ooff + 5] = (off >> 24) & 0xFF
            dst[ooff + 6] = extra & 0xFF
            dst[ooff + 7] = (extra >> 8) & 0xFF
            pos += extra
        elif tc == 5:
            off = pos
            remain = plen - pos
            dst[ooff + 2] = off & 0xFF
            dst[ooff + 3] = (off >> 8) & 0xFF
            dst[ooff + 4] = (off >> 16) & 0xFF
            dst[ooff + 5] = (off >> 24) & 0xFF
            dst[ooff + 6] = remain & 0xFF
            dst[ooff + 7] = (remain >> 8) & 0xFF
            pos = plen

    dst[int(len(out_buf)) - 2] = fc & 0xFF
    dst[int(len(out_buf)) - 1] = (fc >> 8) & 0xFF


class SchemaCodec:
    @staticmethod
    def decode(cmd_def, payload, store=None):
        out = {"_name": cmd_def.get("name"), "_cmd": cmd_def.get("cmd")}
        raw_cmd = cmd_def.get("cmd", 0)
        if isinstance(raw_cmd, str):
            raw_cmd = raw_cmd.strip()
            cmd_id = int(raw_cmd, 16) if raw_cmd.startswith("0x") or raw_cmd.startswith("0X") else int(raw_cmd)
        else:
            cmd_id = int(raw_cmd or 0)

        if store is None or not store.dispatch_buf:
            return out

        names = store.field_names.get(cmd_id)
        if not names:
            return out

        # 🔧 CPython 相容: viper/ptr8 只在 MicroPython 上可用,
        # PC 端 (NetBusMaster 等) 走純 Python 解碼, 語意與 _viper_decode 一致。
        if not IS_MICROPYTHON:
            return SchemaCodec._decode_py(store, cmd_id, names, payload, out)

        out_buf = bytearray(len(names) * 8 + 2)
        _viper_decode(store.dispatch_buf, store.field_buf, cmd_id, payload, len(payload), out_buf)
        fc = out_buf[-2] | (out_buf[-1] << 8)

        for i in range(fc):
            off = i * 8
            tc = out_buf[off]
            name = names[i]
            val = out_buf[off + 2] | (out_buf[off + 3] << 8) | (out_buf[off + 4] << 16) | (out_buf[off + 5] << 24)
            ext = out_buf[off + 6] | (out_buf[off + 7] << 8)
            if tc == 0:
                out[name] = val & 0xFF
            elif tc == 1:
                out[name] = val & 0xFFFF
            elif tc == 2:
                out[name] = val
            elif tc == 3:
                out[name] = bytes(payload[val:val + ext]).decode("utf-8")
            elif tc == 4:
                out[name] = bytes(payload[val:val + ext])
            elif tc == 5:
                out[name] = memoryview(payload)[val:]

        return out

    @staticmethod
    def _decode_py(store, cmd_id, names, payload, out):
        """CPython 純 Python 解碼 (與 _viper_decode 相同欄位語意)。

        dispatch_buf: 每筆 8B = cmd_id(u16) + field_start(u16) + count(u8) + pad(3)
        field_buf   : 每欄 2B = type_code(u8) + extra(u8)
        type: 0=u8 1=u16 2=u32 3=str_u16len 4=bytes_fixed 5=bytes_rest
        """
        dispatch = store.dispatch_buf
        fields = store.field_buf
        plen = len(payload)
        fs = 0
        fc = 0
        nd = len(dispatch) // 8
        for di in range(nd):
            doff = di * 8
            cid = dispatch[doff] | (dispatch[doff + 1] << 8)
            if cid == cmd_id:
                fs = dispatch[doff + 2] | (dispatch[doff + 3] << 8)
                fc = dispatch[doff + 4]
                break

        pos = 0
        for i in range(fc):
            fi = (fs + i) * 2
            tc = fields[fi]
            extra = fields[fi + 1]
            name = names[i]
            if pos >= plen and tc != 5:
                break
            if tc == 0:
                out[name] = payload[pos] if pos < plen else 0
                pos += 1
            elif tc == 1:
                if pos + 1 < plen:
                    out[name] = payload[pos] | (payload[pos + 1] << 8)
                else:
                    out[name] = 0
                pos += 2
            elif tc == 2:
                if pos + 3 < plen:
                    out[name] = (payload[pos] | (payload[pos + 1] << 8) |
                                 (payload[pos + 2] << 16) | (payload[pos + 3] << 24))
                else:
                    out[name] = 0
                pos += 4
            elif tc == 3:
                if pos + 1 < plen:
                    slen = payload[pos] | (payload[pos + 1] << 8)
                    pos += 2
                    out[name] = bytes(payload[pos:pos + slen]).decode("utf-8")
                    pos += slen
                else:
                    out[name] = ""
            elif tc == 4:
                out[name] = bytes(payload[pos:pos + extra])
                pos += extra
            elif tc == 5:
                out[name] = memoryview(payload)[pos:]
                pos = plen
        return out

    @staticmethod
    def encode(cmd_def, obj):
        buf = bytearray()
        plist = cmd_def.get("payload", [])
        for i, f in enumerate(plist):
            t = f["type"]
            name = f["name"]
            val = obj.get(name)
            try:
                if t == "u8":
                    buf.append(int(val or 0) & 0xFF)
                # ── 定寬整數:超出型別域 → 夾住（不是丟例外）──────────────
                #   為什麼要夾:struct.pack 超界會 raise,而下面的 except 只印一行
                #   就繼續 → **該欄位整欄消失,payload 從此錯位**(後面所有欄位前移),
                #   呼叫端拿到的是「長度不對但看起來正常」的幀。
                #   例:u16 start_delay_ms=70000 原本產出 3B(應 5B),收端解成
                #   「延遲 0、亮度 255」,兩邊都不報錯。
                #   u8 不在此列 —— 它用 & 0xFF(取模)且長度永遠正確,維持現狀。
                #   合法值完全不受影響(夾的作用只在超界時發生)。
                elif t == "u16":
                    buf.extend(struct.pack("<H", max(0, min(0xFFFF, int(val or 0)))))
                elif t == "u32":
                    buf.extend(struct.pack("<I", max(0, min(0xFFFFFFFF, int(val or 0)))))
                elif t == "i16":
                    buf.extend(struct.pack("<h", max(-0x8000, min(0x7FFF, int(val or 0)))))
                elif t == "i32":
                    buf.extend(struct.pack("<i", max(-0x80000000, min(0x7FFFFFFF, int(val or 0)))))
                elif t == "str_u16len":
                    s = str(val or "").encode("utf-8")
                    buf.extend(struct.pack("<H", len(s)))
                    buf.extend(s)
                elif t == "bytes_fixed":
                    flen = int(f["len"])
                    b = val if val is not None else b"\x00" * flen
                    if len(b) > flen:
                        b = b[:flen]
                    if len(b) < flen:
                        b = b + b"\x00" * (flen - len(b))
                    buf.extend(b)
                elif t == "bytes_rest":
                    if val is not None:
                        if isinstance(val, (bytes, bytearray, memoryview)):
                            buf.extend(val)
                        else:
                            buf.extend(bytes(val))
            except Exception as e:
                print(f"❌ [Codec] Encode field '{name}' failed: {e}")
        return bytes(buf)
