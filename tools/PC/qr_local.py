# qr_local.py — 純本地 QR Code 編碼器 (零依賴, 不連外網)
#
# 只支援「Byte mode + Error correction level M」, 版本 1~10 (涵蓋一般 URL/IP:port)。
# 輸出 SVG 字串, 瀏覽器原生渲染, 不依賴 PIL/PNG 或任何線上 QR API。
# 表值取自 ISO/IEC 18004 (版本 1~10, EC=M)。

# ── GF(256) 算術, 多項式 0x11D ──
def _build_gf():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _build_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(nsym):
    g = [1]
    for i in range(nsym):
        nxt = [0] * (len(g) + 1)
        for j in range(len(g)):
            nxt[j] ^= _gf_mul(g[j], _EXP[i])
            nxt[j + 1] ^= g[j]
        g = nxt
    return g


def _rs_encode(data, nsym):
    """回傳 data + ecc 的完整 codeword 清單。

    🔧 修正 (2026-09-10): 原本的實作把 generator 的次數順序搞反了 ——
    _rs_generator 回傳「最低次在前」, 但多項式除法要「最高次在前」(monic,
    gen[0]=1)。原寫法 factor 用 res.pop(0) 再對 res[i] 逐項消去, 等於用
    反序的 generator, 算出來的 ECC 全錯 → 掃碼器驗證失敗 (iPhone 直接
    無法識別)。資料段本身正確, 所以肉眼看不出問題。
    """
    gen = list(reversed(_rs_generator(nsym)))   # → 最高次在前, gen[0] = 1
    res = list(data) + [0] * nsym
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return data + res[len(data):]


# ── 版本表 (EC level M, 版本 1~10) ──
# (total_codewords, ec_per_block, [(blocks, data_per_block), ...])
_EC_M = {
    1:  (26, 10, [(1, 16)]),
    2:  (44, 16, [(1, 28)]),
    3:  (70, 26, [(1, 44)]),
    4:  (100, 18, [(2, 32)]),
    5:  (134, 24, [(2, 43)]),
    6:  (172, 16, [(4, 27)]),
    7:  (196, 18, [(4, 31)]),
    8:  (242, 22, [(2, 38), (2, 39)]),
    9:  (292, 22, [(3, 36), (2, 37)]),
    10: (346, 26, [(4, 43), (1, 44)]),
}

_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

_CHARCOUNT_BITS = {v: (8 if v <= 9 else 16) for v in range(1, 11)}


def _pick_version(nbytes):
    """依資料位元組數挑最小可用版本 (EC=M, byte mode)。"""
    for v in range(1, 11):
        _total, _ec, groups = _EC_M[v]
        data_cw = sum(blocks * dpb for blocks, dpb in groups)
        cc_bits = _CHARCOUNT_BITS[v]
        capacity_bytes = (data_cw * 8 - (4 + cc_bits)) // 8
        if nbytes <= capacity_bytes:
            return v
    raise ValueError("資料太長, 超過版本 10 容量 (EC=M)")


def _encode_codewords(data_bytes, version):
    """byte mode → 完整 codeword 位元序列 (mode + 長度 + 資料 + terminator + padding)。

    之後依版本切 block、Reed-Solomon 編碼、再 interleave 成最終 codeword 流。
    """
    cc_bits = _CHARCOUNT_BITS[version]
    total_cw, ec_per_block, groups = _EC_M[version]
    data_cw = sum(blocks * dpb for blocks, dpb in groups)
    capacity_bits = data_cw * 8

    bits = [0, 1, 0, 0]  # mode 0100 (byte)
    for i in range(cc_bits - 1, -1, -1):
        bits.append((len(data_bytes) >> i) & 1)
    for b in data_bytes:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)

    # terminator (最多 4 個 0, 但不超過剩餘容量)
    if len(bits) <= capacity_bits:
        bits += [0] * min(4, capacity_bits - len(bits))
    # 補齊到位元組邊界
    while len(bits) % 8 != 0:
        bits.append(0)
    # pad bytes: 交替 0xEC 0x11 直到填滿資料容量
    pad = [0xEC, 0x11]
    pi = 0
    while len(bits) < capacity_bits:
        for i in range(7, -1, -1):
            bits.append((pad[pi] >> i) & 1)
        pi ^= 1

    # 位元流 → 位元組
    data = []
    for i in range(0, len(bits), 8):
        b = 0
        for j in range(8):
            b = (b << 1) | bits[i + j]
        data.append(b)

    # 依版本切 block
    blocks = []
    pos = 0
    for blocks_count, data_per_block in groups:
        for _ in range(blocks_count):
            blocks.append(data[pos:pos + data_per_block])
            pos += data_per_block

    encoded = [_rs_encode(list(b), ec_per_block) for b in blocks]
    result = []
    # 資料 interleave
    max_data = max(len(b) for b in blocks)
    for i in range(max_data):
        for b in blocks:
            if i < len(b):
                result.append(b[i])
    # ECC interleave
    for i in range(ec_per_block):
        for b in encoded:
            result.append(b[len(b) - ec_per_block + i])
    while len(result) < total_cw:
        result.append(0)
    return result


def _is_function(r, c, size, version):
    """判斷 (r, c) 是否為 function module (不承載資料)。"""
    # Finder patterns + separators
    if r <= 8 and c <= 8:
        return True
    if r <= 8 and c >= size - 8:
        return True
    if r >= size - 8 and c <= 8:
        return True
    # Timing
    if r == 6 or c == 6:
        return True
    # Alignment patterns (5x5), 排除與 finder 重疊的三個角
    align = _ALIGN[version]
    if align:
        first = align[0]
        last = align[-1]
        for ar in align:
            for ac in align:
                if (ar == first and ac == first) or (ar == first and ac == last) or (ar == last and ac == first):
                    continue
                if abs(r - ar) <= 2 and abs(c - ac) <= 2:
                    return True
    # Format info areas (兩處)
    if r == 8 and (c <= 8 or c >= size - 8):
        return True
    if c == 8 and (r <= 8 or r >= size - 8):
        return True
    # Dark module (版本 >= 2)
    if r == 4 * version + 9 and c == 8:
        return True
    return False


def _draw_function(m, size, version):
    """畫 finder/timing/alignment 圖樣, 回傳 final matrix (未放資料/未 mask)。"""
    def set_region(r, c, w, h, val):
        for i in range(r, r + h):
            for j in range(c, c + w):
                if 0 <= i < size and 0 <= j < size:
                    m[i][j] = val

    # Finder (含 separator)
    for (r, c) in [(0, 0), (0, size - 7), (size - 7, 0)]:
        set_region(r, c, 7, 7, True)
        set_region(r + 1, c + 1, 5, 5, False)
        set_region(r + 2, c + 2, 3, 3, True)
    # Timing
    for i in range(8, size - 8):
        m[6][i] = m[i][6] = (i % 2 == 0)
    # Alignment (跳過與 finder 重疊的三個角)
    align = _ALIGN[version]
    if align:
        first, last = align[0], align[-1]
        for ar in align:
            for ac in align:
                if (ar == first and ac == first) or (ar == first and ac == last) or (ar == last and ac == first):
                    continue
                set_region(ar - 2, ac - 2, 5, 5, True)
                set_region(ar - 1, ac - 1, 3, 3, False)
                m[ar][ac] = True
    # Dark module
    if version >= 2:
        m[4 * version + 9][8] = True


def _place_data(m, size, version, bits):
    """把資料位元擺進非 function 模組 (標準 zig-zag, 上下交替)。"""
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if not _is_function(r, c, size, version):
                    if bit_idx < len(bits):
                        m[r][c] = bool(bits[bit_idx])
                        bit_idx += 1
                    else:
                        m[r][c] = False
        upward = not upward
        col -= 2


_MASK_FNS = {
    0: lambda r, c: (r + c) % 2 == 0,
    1: lambda r, c: r % 2 == 0,
    2: lambda r, c: c % 3 == 0,
    3: lambda r, c: (r + c) % 3 == 0,
    4: lambda r, c: (r // 2 + c // 3) % 2 == 0,
    5: lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    6: lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    7: lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
}


def _format_info(mask):
    """EC level M (00) + mask → 15-bit BCH format string。"""
    data = (0b00 << 3) | mask
    g = 0b10100110111
    d = data << 10
    for i in range(14, 9, -1):
        if d & (1 << i):
            d ^= g << (i - 10)
    return ((data << 10) | d) ^ 0b101010000010010


def _apply_mask_and_format(m, size, version, mask):
    fn = _MASK_FNS[mask]
    out = [row[:] for row in m]
    for r in range(size):
        for c in range(size):
            if not _is_function(r, c, size, version):
                out[r][c] = out[r][c] ^ fn(r, c)

    fmt = _format_info(mask)
    fmt_bits = [(fmt >> i) & 1 for i in range(14, -1, -1)]
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for i, (r, c) in enumerate(coords1):
        out[r][c] = bool(fmt_bits[i])
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
               (size - 5, 8), (size - 6, 8), (size - 7, 8), (8, size - 8),
               (8, size - 7), (8, size - 6), (8, size - 5), (8, size - 4),
               (8, size - 3), (8, size - 2), (8, size - 1)]
    for i, (r, c) in enumerate(coords2):
        out[r][c] = bool(fmt_bits[i])
    return out


def _penalty(m, size):
    score = 0
    for r in range(size):
        run = 1
        for c in range(1, size):
            if m[r][c] == m[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
    for c in range(size):
        run = 1
        for r in range(1, size):
            if m[r][c] == m[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2
    return score


def _choose_mask(m, size, version):
    best, best_score = None, None
    for mask in range(8):
        out = _apply_mask_and_format(m, size, version, mask)
        s = _penalty(out, size)
        if best_score is None or s < best_score:
            best_score, best = s, out
    return best


def qr_svg(data, module=4, quiet=4):
    """把 data (str) 編成 QR SVG 字串。module = 每格像素, quiet = 邊白格數。"""
    data_bytes = data.encode("utf-8")
    version = _pick_version(len(data_bytes))
    codewords = _encode_codewords(data_bytes, version)
    size = 17 + 4 * version

    m = [[False] * size for _ in range(size)]
    _draw_function(m, size, version)
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    _place_data(m, size, version, bits)
    final = _choose_mask(m, size, version)

    total = size + 2 * quiet
    dim = total * module
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">',
             f'<rect width="100%" height="100%" fill="#fff"/>',
             f'<path fill="#000" d="']
    for r in range(size):
        for c in range(size):
            if final[r][c]:
                x = (c + quiet) * module
                y = (r + quiet) * module
                parts.append(f"M{x} {y}h{module}v{module}h-{module}z")
    parts.append('"/></svg>')
    return "".join(parts)


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.8.150:8080"
    print(qr_svg(url))
