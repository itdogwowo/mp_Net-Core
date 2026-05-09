def read_u32_le(buf, off):
    return buf[off + 0] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def write_u32_le(buf, off, v):
    buf[off + 0] = v & 255
    buf[off + 1] = (v >> 8) & 255
    buf[off + 2] = (v >> 16) & 255
    buf[off + 3] = (v >> 24) & 255
