try:
    import ujson as json
except Exception:
    import json

FONT_5X7 = bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x5f\x00\x00\x00\x07\x00\x07\x00\x14\x7f\x14\x7f\x14\x24\x2a\x7f\x2a\x12\x23\x13\x08\x64\x62\x36\x49\x55\x22\x50\x00\x05\x03\x00\x00\x00\x1c\x22\x41\x00\x00\x41\x22\x1c\x00\x08\x2a\x1c\x2a\x08\x08\x08\x3e\x08\x08\x00\x50\x30\x00\x00\x08\x08\x08\x08\x08\x00\x60\x60\x00\x00\x20\x10\x08\x04\x02\x3e\x51\x49\x45\x3e\x00\x42\x7f\x40\x00\x42\x61\x51\x49\x46\x21\x41\x45\x4b\x31\x18\x14\x12\x7f\x10\x27\x45\x45\x45\x39\x3c\x4a\x49\x49\x30\x01\x71\x09\x05\x03\x36\x49\x49\x49\x36\x06\x49\x49\x29\x1e\x00\x36\x36\x00\x00\x00\x56\x36\x00\x00\x00\x08\x14\x22\x41\x00\x14\x14\x14\x14\x14\x00\x41\x22\x14\x08\x00\x02\x01\x51\x09\x06\x32\x49\x79\x41\x3e\x7e\x11\x11\x11\x7e\x7f\x49\x49\x49\x36\x3e\x41\x41\x41\x22\x7f\x41\x41\x22\x1c\x7f\x49\x49\x49\x41\x7f\x09\x09\x01\x01\x3e\x41\x41\x49\x7a\x7f\x08\x08\x08\x7f\x00\x41\x7f\x41\x00\x20\x40\x41\x3f\x01\x7f\x08\x14\x22\x41\x7f\x40\x40\x40\x40\x7f\x02\x04\x02\x7f\x7f\x04\x08\x10\x7f\x3e\x41\x41\x41\x3e\x7f\x09\x09\x09\x06\x3e\x41\x51\x21\x5e\x7f\x09\x19\x29\x46\x46\x49\x49\x49\x31\x01\x01\x7f\x01\x01\x3f\x40\x40\x40\x3f\x1f\x20\x40\x20\x1f\x7f\x20\x18\x20\x7f\x63\x14\x08\x14\x63\x07\x08\x70\x08\x07\x61\x51\x49\x45\x43\x00\x7f\x41\x41\x00\x02\x04\x08\x10\x20\x00\x41\x41\x7f\x00\x04\x02\x01\x02\x04\x40\x40\x40\x40\x40\x00\x01\x02\x04\x00\x20\x54\x54\x54\x78\x7f\x48\x44\x44\x38\x38\x44\x44\x44\x20\x38\x44\x44\x48\x7f\x38\x54\x54\x54\x18\x08\x7e\x09\x01\x02\x08\x54\x54\x54\x3c\x7f\x08\x04\x04\x78\x00\x44\x7d\x40\x00\x20\x40\x44\x3d\x00\x7f\x10\x28\x44\x00\x00\x41\x7f\x40\x00\x7c\x04\x18\x04\x78\x7c\x08\x04\x04\x78\x38\x44\x44\x44\x38\x7c\x14\x14\x14\x08\x08\x14\x14\x18\x7c\x7c\x08\x04\x04\x08\x48\x54\x54\x54\x20\x04\x3f\x44\x40\x20\x3c\x40\x40\x20\x7c\x1c\x20\x40\x20\x1c\x3c\x40\x30\x40\x3c\x44\x28\x10\x28\x44\x0c\x50\x50\x50\x3c\x44\x64\x54\x4c\x44\x00\x08\x36\x41\x00\x00\x00\x7f\x00\x00\x00\x41\x36\x08\x00\x08\x04\x08\x10\x08\x00')


def _rgb565_from_hex(hex_color):
    c = str(hex_color or "#000000").lstrip("#")
    if len(c) < 6:
        c = c.ljust(6, "0")
    r = int(c[0:2], 16) >> 3
    g = int(c[2:4], 16) >> 2
    b = int(c[4:6], 16) >> 3
    return (int(r) << 11) | (int(g) << 5) | int(b)


def _pack_rgb565_le(rgb565):
    return bytes([rgb565 & 0xFF, (rgb565 >> 8) & 0xFF])


class JpegHookRunner:
    def __init__(self):
        self._config = {}
        self._overlay_cache = {}

    def set_config(self, config_json):
        try:
            self._config = json.loads(config_json) if isinstance(config_json, str) else (config_json or {})
        except Exception:
            self._config = {}

    def set_overlay(self, overlay_id, pixel_data):
        self._overlay_cache[str(overlay_id)] = memoryview(pixel_data)[:]

    def _is_le(self, info):
        pf = str(info.get("pixel_format") or "RGB565_LE")
        return pf.endswith("_LE")

    def __call__(self, payload, info):
        ops = self._config.get("ops") if isinstance(self._config, dict) else None
        if not ops:
            return None
        bpp = int(info.get("bpp", 2) or 2)
        w = int(info.get("w", 0) or 0)
        h = int(info.get("h", 0) or 0)
        is_le = self._is_le(info)
        for op in ops:
            op_type = str(op.get("type") or "")
            if op_type == "fill_rect":
                self._op_fill_rect(payload, w, h, bpp, is_le, op)
            elif op_type == "text":
                self._op_text(payload, w, h, bpp, is_le, op)
            elif op_type == "blend":
                self._op_blend(payload, w, h, bpp, is_le, op)
        return None

    def _write_pixel(self, payload, px, py, w, bpp, is_le, rgb565):
        if px < 0 or py < 0 or px >= w:
            return
        off = (py * w + px) * bpp
        if off < 0 or off + bpp > len(payload):
            return
        if bpp == 2:
            if is_le:
                payload[off] = rgb565 & 0xFF
                payload[off + 1] = (rgb565 >> 8) & 0xFF
            else:
                payload[off] = (rgb565 >> 8) & 0xFF
                payload[off + 1] = rgb565 & 0xFF
        elif bpp == 3:
            r = ((rgb565 >> 11) & 0x1F) << 3
            g = ((rgb565 >> 5) & 0x3F) << 2
            b = (rgb565 & 0x1F) << 3
            payload[off] = r
            payload[off + 1] = g
            payload[off + 2] = b

    def _read_pixel(self, payload, px, py, w, bpp, is_le):
        if px < 0 or py < 0 or px >= w:
            return 0
        off = (py * w + px) * bpp
        if off < 0 or off + bpp > len(payload):
            return 0
        if bpp == 2:
            lo = payload[off]
            hi = payload[off + 1]
            return (int(hi) << 8) | int(lo) if is_le else (int(lo) << 8) | int(hi)
        elif bpp == 3:
            r = payload[off] >> 3
            g = payload[off + 1] >> 2
            b = payload[off + 2] >> 3
            return (r << 11) | (g << 5) | b
        return 0

    def _blend_pixel(self, bg, fg, alpha):
        a = max(0, min(255, int(alpha)))
        if a == 0:
            return bg
        if a >= 255:
            return fg
        bg_r = (bg >> 11) & 0x1F
        bg_g = (bg >> 5) & 0x3F
        bg_b = bg & 0x1F
        fg_r = (fg >> 11) & 0x1F
        fg_g = (fg >> 5) & 0x3F
        fg_b = fg & 0x1F
        r = (bg_r * (255 - a) + fg_r * a) // 255
        g = (bg_g * (255 - a) + fg_g * a) // 255
        b = (bg_b * (255 - a) + fg_b * a) // 255
        return (int(r) << 11) | (int(g) << 5) | int(b)

    def _op_fill_rect(self, payload, w, h, bpp, is_le, op):
        rx = int(op.get("x", 0) or 0)
        ry = int(op.get("y", 0) or 0)
        rw = int(op.get("w", w) or w)
        rh = int(op.get("h", h) or h)
        color = _rgb565_from_hex(op.get("color", "#000000"))
        for py in range(max(0, ry), min(h, ry + rh)):
            for px in range(max(0, rx), min(w, rx + rw)):
                self._write_pixel(payload, px, py, w, bpp, is_le, color)

    def _op_text(self, payload, w, h, bpp, is_le, op):
        text = str(op.get("text", "") or "")
        if not text:
            return
        tx = int(op.get("x", 0) or 0)
        ty = int(op.get("y", 0) or 0)
        color = _rgb565_from_hex(op.get("color", "#FFFFFF"))
        char_w = 5
        char_h = 7
        spacing = 1
        for ci, ch in enumerate(text):
            cx = tx + ci * (char_w + spacing)
            self._draw_char(payload, w, h, bpp, is_le, ch, cx, ty, color, char_w, char_h)

    def _draw_char(self, payload, buf_w, buf_h, bpp, is_le, ch, cx, cy, color, char_w, char_h):
        idx = ord(ch)
        if idx < 32 or idx > 126:
            return
        font_idx = (idx - 32) * char_w
        for fy in range(char_h):
            row = FONT_5X7[font_idx + fy] if font_idx + fy < len(FONT_5X7) else 0
            for fx in range(char_w):
                if row & (1 << (char_w - 1 - fx)):
                    self._write_pixel(payload, cx + fx, cy + fy, buf_w, bpp, is_le, color)

    def _op_blend(self, payload, w, h, bpp, is_le, op):
        overlay_id = str(op.get("src", "") or "")
        overlay = self._overlay_cache.get(overlay_id)
        if overlay is None or not len(overlay):
            return
        dx = int(op.get("dx", 0) or 0)
        dy = int(op.get("dy", 0) or 0)
        alpha = int(op.get("alpha", 255) or 255)
        ow = int(op.get("ow", w) or w)
        oh = int(op.get("oh", h) or h)
        for py in range(max(0, dy), min(h, dy + oh)):
            for px in range(max(0, dx), min(w, dx + ow)):
                ox = px - dx
                oy = py - dy
                if ox < 0 or oy < 0 or ox >= ow:
                    continue
                fg = self._read_pixel(overlay, ox, oy, ow, bpp, is_le)
                if fg == 0:
                    continue
                bg = self._read_pixel(payload, px, py, w, bpp, is_le)
                blended = self._blend_pixel(bg, fg, alpha)
                self._write_pixel(payload, px, py, w, bpp, is_le, blended)


_jpeg_hook_runner = None


def get_hook_runner():
    global _jpeg_hook_runner
    if _jpeg_hook_runner is None:
        _jpeg_hook_runner = JpegHookRunner()
    return _jpeg_hook_runner
