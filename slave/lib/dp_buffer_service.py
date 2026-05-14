try:
    import ustruct as _struct
except Exception:
    import struct as _struct

from lib.buffer_hub import AtomicStreamHub


HDR_OUT = 32
OUT_FMT = "<IHHhhHHHH"


def _pack_into(buf, offset, *args):
    _struct.pack_into(OUT_FMT, buf, offset, *args)


def _unpack_from(buf, offset=0):
    return _struct.unpack_from(OUT_FMT, buf, offset)


def _u32(buf, off):
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def _u16(buf, off):
    return buf[off] | (buf[off + 1] << 8)


def _i16(buf, off):
    v = buf[off] | (buf[off + 1] << 8)
    return v - 0x10000 if v >= 0x8000 else v


def unpack_out_header_into(buf, out):
    out[0] = _u32(buf, 0)
    out[1] = _u16(buf, 4)
    out[2] = _u16(buf, 6)
    out[3] = _i16(buf, 8)
    out[4] = _i16(buf, 10)
    out[5] = _u16(buf, 12)
    out[6] = _u16(buf, 14)
    out[7] = _u16(buf, 16)
    out[8] = _u16(buf, 18)


def ensure_dp_buffer_service(bus, name="dp_buffer"):
    svc = bus.get_service(name)
    if svc is not None:
        return svc

    svc = {
        "api": 1,
        "enable": True,
        "pixel_format": "RGB565_BE",
        "max_frame_bytes": 0,
        "jpeg_out": None,
        "out_hub": None,
        "pending": None,
        "hook": None,
        "hook_enable": False,
        "cfg_epoch": 0,
        "last_err": "",
        "last_ms": 0,
        "frames": 0,
        "last_done": None,
    }
    bus.register_service(name, svc)
    try:
        bus.register_provider("dp_frames", lambda: int(svc.get("frames", 0) or 0))
    except Exception:
        pass
    return svc


def configure_for_layout(bus, layout, *, pixel_format="RGB565_LE", num_buffers=3, name="dp_buffer"):
    svc = ensure_dp_buffer_service(bus, name=name)
    max_frame_bytes = 0
    for it in layout or []:
        try:
            w = int(it.get("w", 0) or 0)
            h = int(it.get("h", 0) or 0)
            bpp = int(it.get("bpp", 2) or 2)
            n = w * h * bpp
            if n > max_frame_bytes:
                max_frame_bytes = n
        except Exception:
            pass
    if max_frame_bytes <= 0:
        max_frame_bytes = 240 * 240 * 2
    svc["pixel_format"] = str(pixel_format or "RGB565_LE")
    svc["max_frame_bytes"] = int(max_frame_bytes)
    hub_size = HDR_OUT + int(max_frame_bytes)
    svc["jpeg_out"] = AtomicStreamHub(hub_size, num_buffers=int(num_buffers))
    if str(svc["pixel_format"]).startswith("RGB888"):
        svc["out_hub"] = None
    else:
        svc["out_hub"] = AtomicStreamHub(hub_size, num_buffers=int(num_buffers))
    svc["pending"] = None
    svc["cfg_epoch"] = (int(svc.get("cfg_epoch", 0) or 0) + 1) & 0xFFFF
    return svc


def pack_out_header(buf, payload_len, *, seq=0, label_id=0, x=0, y=0, w=0, h=0, flags=0, fmt_code=0):
    _pack_into(buf, 0, int(payload_len), int(seq), int(label_id), int(x), int(y), int(w), int(h), int(flags), int(fmt_code))


def unpack_out_header(buf):
    return _unpack_from(buf, 0)
