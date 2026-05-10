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
                elif t == "u16":
                    buf.extend(struct.pack("<H", int(val or 0)))
                elif t == "u32":
                    buf.extend(struct.pack("<I", int(val or 0)))
                elif t == "i16":
                    buf.extend(struct.pack("<h", int(val or 0)))
                elif t == "i32":
                    buf.extend(struct.pack("<i", int(val or 0)))
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
