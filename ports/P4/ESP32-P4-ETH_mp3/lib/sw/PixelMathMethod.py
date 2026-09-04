"""
PixelMathMethod.py — 12-bit 整數波形數學核心（免查表多項式逼近）

三條硬約束：
  1. 核心用 @micropython.viper 加速
  2. 全程整數運算，無浮點、無 math.sin / math.pi、無查表
  3. 數值域固定 12-bit（0-4095），輸出 buffer 用 array('H')

波形逼近：拋物線基底 + 二次修正項（922*(y^2-y)>>12），把 0-65535 相位映射到
0-4095（或 -4096..4096）的正弦，取代舊的 65536 點查表。

效能技巧（對齊舊專案）：
  - 預先儲存重複計算：compile() 把 program 編譯成段描述 tuple，除法/位移/clamp
    只在編譯時算一次，value_at 熱路徑不再 dict.get / int / // / clamp。
  - 乘數變加數：Effect.frame 用 g += spacing 累加，取代 i*spacing 乘法。

決定性（無狀態）：value_at(comp, g) 給全域幀 g 直接回傳單值，
是 effect 的 restart / seek 的基石（相位不藏在 generator 狀態裡）。
"""

try:
    import micropython
    _MP = True
except ImportError:
    _MP = False
    micropython = None


if _MP:

    @micropython.viper
    def _wave01_q12(phase: int) -> int:
        # 0-4095 單週期正弦（相位 0-65535）
        p = phase & 65535
        if p < 32768:
            x = p
            sgn = 1
        else:
            x = p - 32768
            sgn = -1
        s = (x * (32768 - x)) >> 16
        s2 = (s * s) >> 12
        s = s + ((922 * (s2 - s)) >> 12)
        if sgn < 0:
            s = -s
        w = (s + 4096) >> 1
        if w > 4095:
            w = 4095
        return w

    @micropython.viper
    def _sin_q12(phase: int) -> int:
        # -4096..4096 有號正弦（相位 0-65535），供符號判斷用
        p = phase & 65535
        if p < 32768:
            x = p
            sgn = 1
        else:
            x = p - 32768
            sgn = -1
        y = (x * (32768 - x)) >> 16
        y2 = (y * y) >> 12
        y = y + ((922 * (y2 - y)) >> 12)
        return y if sgn > 0 else -y

else:
    # PC 對照版（無 micropython），語義與 viper 版一致
    def _wave01_q12(phase):
        p = phase & 65535
        if p < 32768:
            x = p
            sgn = 1
        else:
            x = p - 32768
            sgn = -1
        s = (x * (32768 - x)) >> 16
        s2 = (s * s) >> 12
        s = s + ((922 * (s2 - s)) >> 12)
        if sgn < 0:
            s = -s
        w = (s + 4096) >> 1
        return 4095 if w > 4095 else w

    def _sin_q12(phase):
        p = phase & 65535
        if p < 32768:
            x = p
            sgn = 1
        else:
            x = p - 32768
            sgn = -1
        y = (x * (32768 - x)) >> 16
        y2 = (y * y) >> 12
        y = y + ((922 * (y2 - y)) >> 12)
        return y if sgn > 0 else -y


def _clamp12(v):
    v = int(v)
    if v < 0:
        return 0
    if v > 4095:
        return 4095
    return v


# 波形段 type → 小整數（熱路徑用 int 分派，比 str 比較快）
_KIND = {"keep": 0, "math_now": 1, "square_wave_now": 2,
         "pulse_wave": 3, "pulse": 4, "starter": 5}


class PixelMathMethod:
    """12-bit 整數波形數學核心。無狀態、決定性。"""

    def compile(self, program):
        """預編譯 program → 段描述 tuple 列表（重複計算只算一次）。

        每段 tuple：
          (start, end, kind, l_range, l_lim, phi4, step_phase, pulse, gap, width)
          start/end    : 段在全域時間軸的起/止幀（end 為累加 end_Time）
          kind         : 段 type 的小整數（_KIND）
          l_range      : clamp(l_max) - clamp(l_lim)
          l_lim        : clamp(l_lim)
          phi4         : phi << 4（對齊舊 is_math_pattern_next）
          step_phase   : (65536*F)//10//fs，相位每幀增量（預算好，省除法）
          pulse        : pulse_wave 的門檻（原始值）
          gap / width  : pulse 型用（gap = fs//F，width = pulse % gap）
        """
        comp = []
        prev = 0
        for seg in program:
            end = int(seg.get("end_Time", 0))
            fs = end - prev
            if fs < 1:
                fs = 1
            l_max = _clamp12(seg.get("l_max", 4095))
            l_lim = _clamp12(seg.get("l_lim", 0))
            l_range = l_max - l_lim
            F = int(seg.get("F", 1))
            step_phase = (65536 * F) // 10 // fs
            phi4 = int(seg.get("phi", 0)) << 4
            kind = _KIND.get(seg.get("type", "keep"), 0)
            pulse = int(seg.get("pulse", 2047))
            gap = fs // F if F > 0 else 1
            if gap < 1:
                gap = 1
            width = pulse % gap
            comp.append((prev, end, kind, l_range, l_lim, phi4, step_phase, pulse, gap, width))
            prev = end
        return comp

    def value_at(self, comp, g):
        """compiled + 全域幀 g → 單值（0-4095）。決定性、無狀態、熱路徑。"""
        if not comp:
            return 0
        g %= comp[-1][1]
        for seg in comp:
            start, end, kind, l_range, l_lim, phi4, step_phase, pulse, gap, width = seg
            if g < end:
                if kind == 0:           # keep
                    return l_range + l_lim
                if kind == 5:           # starter
                    return 0
                rel = g - start
                if kind == 4:           # pulse（不經正弦）
                    return l_lim + (l_range if (rel + phi4) % gap <= width else 0)
                ph = (phi4 + step_phase * rel) & 65535
                if kind == 1:           # math_now
                    v = (_wave01_q12(ph) * l_range) >> 12
                elif kind == 2:         # square_wave_now
                    v = l_range if _wave01_q12(ph) >= 2048 else 0
                else:                   # pulse_wave (kind == 3)
                    v = l_range if _wave01_q12(ph) >= pulse else 0
                return v + l_lim
        return 0

    def pattern_value_at(self, program, t):
        """相容包裝：直接吃原始 program dict 列表（測試/除錯用，非熱路徑）。"""
        return self.value_at(self.compile(program), int(t))


# 模組級單例（所有 effect 共享一份，零重複建構）
mt = PixelMathMethod()


# ══════════════════════════════════════════════════════════════
# HSV ↔ RGB 色彩轉換（bulk 批次，全整數，無浮點）
#
# 設計原則：不逐 pixel 呼叫，一次處理整條 buffer（viper 用 ptr 掃整 buffer）。
# 兩套位深、各自雙向：
#   8-bit（0-255） : 輸入 h(0-360) s/v(0-255)，RGB 輸出 bytearray（3B/px）
#   12-bit（0-4095）: 輸入 h(0-360) s/v(0-4095)，RGB 輸出 array('H')（3 值/px）
#
# 修掉舊專案 mp_LEDController 的 bug：
#   1. 輸出順序 RGB（舊 _hsv2grb 寫 G,R,B 是給 WS2812 GRB 用；新 RGBW cell 是 R,G,B,W）
#   2. rgb→hsv 飽和度：舊 delta//255//max_val 幾乎恆 0 → 改 delta*SCALE//max_val
#   3. 色相 offset：舊 +(120//65535)=+0 把 +120/+240 吞掉 → 改正確 offset
#   4. 12-bit 用 >>12 位移、8-bit 用 //255；h % 360 正規化、s clamp 到 SCALE
#
# 約定：h 一律用 array('H')（0-359，16-bit 存，因為 0-360 > 255）。
# 暫時包裝：本輪只提供接口，未接 scatter/effect/controller（未來彩色 effect 再接）。
# ══════════════════════════════════════════════════════════════

if _MP:

    @micropython.viper
    def hsv_to_rgb8_buf(h_buf, s_buf, v_buf, out, n: int):
        ph = ptr16(h_buf)
        ps = ptr16(s_buf)
        pv = ptr16(v_buf)
        po = ptr8(out)
        for i in range(n):
            h = int(ph[i]) % 360
            s = int(ps[i])
            v = int(pv[i])
            if s > 255:
                s = 255
            if v > 255:
                v = 255
            if s == 0:
                po[i * 3] = v
                po[i * 3 + 1] = v
                po[i * 3 + 2] = v
            else:
                region = h // 60
                rem = (h - region * 60) * 255 // 60
                p = (v * (255 - s)) // 255
                q = (v * (255 - ((s * rem) // 255))) // 255
                t = (v * (255 - ((s * (255 - rem)) // 255))) // 255
                if region == 0:
                    po[i * 3] = v
                    po[i * 3 + 1] = t
                    po[i * 3 + 2] = p
                elif region == 1:
                    po[i * 3] = q
                    po[i * 3 + 1] = v
                    po[i * 3 + 2] = p
                elif region == 2:
                    po[i * 3] = p
                    po[i * 3 + 1] = v
                    po[i * 3 + 2] = t
                elif region == 3:
                    po[i * 3] = p
                    po[i * 3 + 1] = q
                    po[i * 3 + 2] = v
                elif region == 4:
                    po[i * 3] = t
                    po[i * 3 + 1] = p
                    po[i * 3 + 2] = v
                else:
                    po[i * 3] = v
                    po[i * 3 + 1] = p
                    po[i * 3 + 2] = q

    @micropython.viper
    def rgb_to_hsv8_buf(rgb, h_out, s_out, v_out, n: int):
        pr = ptr8(rgb)
        ph = ptr16(h_out)
        ps = ptr16(s_out)
        pv = ptr16(v_out)
        for i in range(n):
            o = i * 3
            r = int(pr[o])
            g = int(pr[o + 1])
            b = int(pr[o + 2])
            mx = r
            if g > mx:
                mx = g
            if b > mx:
                mx = b
            mn = r
            if g < mn:
                mn = g
            if b < mn:
                mn = b
            delta = mx - mn
            pv[i] = mx
            if delta == 0:
                ph[i] = 0
                ps[i] = 0
            else:
                ps[i] = (delta * 255) // mx
                if mx == r:
                    h = (60 * (g - b)) // delta
                    if h < 0:
                        h += 360
                    ph[i] = h
                elif mx == g:
                    h = (60 * (b - r)) // delta + 120
                    if h < 0:
                        h += 360
                    ph[i] = h
                else:
                    h = (60 * (r - g)) // delta + 240
                    if h < 0:
                        h += 360
                    ph[i] = h

    @micropython.viper
    def hsv_to_rgb12_buf(h_buf, s_buf, v_buf, out, n: int):
        ph = ptr16(h_buf)
        ps = ptr16(s_buf)
        pv = ptr16(v_buf)
        po = ptr16(out)
        for i in range(n):
            h = int(ph[i]) % 360
            s = int(ps[i])
            v = int(pv[i])
            if s > 4095:
                s = 4095
            if v > 4095:
                v = 4095
            if s == 0:
                po[i * 3] = v
                po[i * 3 + 1] = v
                po[i * 3 + 2] = v
            else:
                region = h // 60
                rem = (h - region * 60) * 4095 // 60
                p = (v * (4095 - s)) >> 12
                q = (v * (4095 - ((s * rem) >> 12))) >> 12
                t = (v * (4095 - ((s * (4095 - rem)) >> 12))) >> 12
                if region == 0:
                    po[i * 3] = v
                    po[i * 3 + 1] = t
                    po[i * 3 + 2] = p
                elif region == 1:
                    po[i * 3] = q
                    po[i * 3 + 1] = v
                    po[i * 3 + 2] = p
                elif region == 2:
                    po[i * 3] = p
                    po[i * 3 + 1] = v
                    po[i * 3 + 2] = t
                elif region == 3:
                    po[i * 3] = p
                    po[i * 3 + 1] = q
                    po[i * 3 + 2] = v
                elif region == 4:
                    po[i * 3] = t
                    po[i * 3 + 1] = p
                    po[i * 3 + 2] = v
                else:
                    po[i * 3] = v
                    po[i * 3 + 1] = p
                    po[i * 3 + 2] = q

    @micropython.viper
    def rgb_to_hsv12_buf(rgb, h_out, s_out, v_out, n: int):
        pr = ptr16(rgb)
        ph = ptr16(h_out)
        ps = ptr16(s_out)
        pv = ptr16(v_out)
        for i in range(n):
            o = i * 3
            r = int(pr[o])
            g = int(pr[o + 1])
            b = int(pr[o + 2])
            mx = r
            if g > mx:
                mx = g
            if b > mx:
                mx = b
            mn = r
            if g < mn:
                mn = g
            if b < mn:
                mn = b
            delta = mx - mn
            pv[i] = mx
            if delta == 0:
                ph[i] = 0
                ps[i] = 0
            else:
                ps[i] = (delta * 4095) // mx
                if mx == r:
                    h = (60 * (g - b)) // delta
                    if h < 0:
                        h += 360
                    ph[i] = h
                elif mx == g:
                    h = (60 * (b - r)) // delta + 120
                    if h < 0:
                        h += 360
                    ph[i] = h
                else:
                    h = (60 * (r - g)) // delta + 240
                    if h < 0:
                        h += 360
                    ph[i] = h

else:
    # PC 對照版（無 micropython），語義與 viper 版一致

    def hsv_to_rgb8_buf(h_buf, s_buf, v_buf, out, n):
        for i in range(n):
            h = h_buf[i] % 360
            s = 255 if s_buf[i] > 255 else s_buf[i]
            v = 255 if v_buf[i] > 255 else v_buf[i]
            if s == 0:
                r = g = b = v
            else:
                region = h // 60
                rem = (h - region * 60) * 255 // 60
                p = (v * (255 - s)) // 255
                q = (v * (255 - ((s * rem) // 255))) // 255
                t = (v * (255 - ((s * (255 - rem)) // 255))) // 255
                if region == 0:
                    r, g, b = v, t, p
                elif region == 1:
                    r, g, b = q, v, p
                elif region == 2:
                    r, g, b = p, v, t
                elif region == 3:
                    r, g, b = p, q, v
                elif region == 4:
                    r, g, b = t, p, v
                else:
                    r, g, b = v, p, q
            out[i * 3] = r
            out[i * 3 + 1] = g
            out[i * 3 + 2] = b

    def rgb_to_hsv8_buf(rgb, h_out, s_out, v_out, n):
        for i in range(n):
            o = i * 3
            r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
            mx = max(r, g, b)
            mn = min(r, g, b)
            delta = mx - mn
            v_out[i] = mx
            if delta == 0:
                h_out[i] = 0
                s_out[i] = 0
            else:
                s_out[i] = (delta * 255) // mx
                if mx == r:
                    h = (60 * (g - b)) // delta
                elif mx == g:
                    h = (60 * (b - r)) // delta + 120
                else:
                    h = (60 * (r - g)) // delta + 240
                h_out[i] = h % 360

    def hsv_to_rgb12_buf(h_buf, s_buf, v_buf, out, n):
        for i in range(n):
            h = h_buf[i] % 360
            s = 4095 if s_buf[i] > 4095 else s_buf[i]
            v = 4095 if v_buf[i] > 4095 else v_buf[i]
            if s == 0:
                r = g = b = v
            else:
                region = h // 60
                rem = (h - region * 60) * 4095 // 60
                p = (v * (4095 - s)) >> 12
                q = (v * (4095 - ((s * rem) >> 12))) >> 12
                t = (v * (4095 - ((s * (4095 - rem)) >> 12))) >> 12
                if region == 0:
                    r, g, b = v, t, p
                elif region == 1:
                    r, g, b = q, v, p
                elif region == 2:
                    r, g, b = p, v, t
                elif region == 3:
                    r, g, b = p, q, v
                elif region == 4:
                    r, g, b = t, p, v
                else:
                    r, g, b = v, p, q
            out[i * 3] = r
            out[i * 3 + 1] = g
            out[i * 3 + 2] = b

    def rgb_to_hsv12_buf(rgb, h_out, s_out, v_out, n):
        for i in range(n):
            o = i * 3
            r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
            mx = max(r, g, b)
            mn = min(r, g, b)
            delta = mx - mn
            v_out[i] = mx
            if delta == 0:
                h_out[i] = 0
                s_out[i] = 0
            else:
                s_out[i] = (delta * 4095) // mx
                if mx == r:
                    h = (60 * (g - b)) // delta
                elif mx == g:
                    h = (60 * (b - r)) // delta + 120
                else:
                    h = (60 * (r - g)) // delta + 240
                h_out[i] = h % 360


# ── 單值便利函式（非熱路徑，供測試/除錯/未來彩色 effect 配參）──
# 裝置版用 @micropython.native 裝飾器（編譯器語法，非 runtime 屬性）；
# PC 版為純 Python 對照（CPython 沒有 micropython）。

if _MP:

    @micropython.native
    def hsv_to_rgb8(h, s, v):
        h = int(h) % 360
        s = 255 if s > 255 else int(s)
        v = 255 if v > 255 else int(v)
        if s == 0:
            return v, v, v
        region = h // 60
        rem = (h - region * 60) * 255 // 60
        p = (v * (255 - s)) // 255
        q = (v * (255 - ((s * rem) // 255))) // 255
        t = (v * (255 - ((s * (255 - rem)) // 255))) // 255
        if region == 0:
            return v, t, p
        if region == 1:
            return q, v, p
        if region == 2:
            return p, v, t
        if region == 3:
            return p, q, v
        if region == 4:
            return t, p, v
        return v, p, q

    @micropython.native
    def rgb_to_hsv8(r, g, b):
        r, g, b = int(r), int(g), int(b)
        mx = max(r, g, b)
        mn = min(r, g, b)
        delta = mx - mn
        if delta == 0:
            return 0, 0, mx
        s = (delta * 255) // mx
        if mx == r:
            h = (60 * (g - b)) // delta
        elif mx == g:
            h = (60 * (b - r)) // delta + 120
        else:
            h = (60 * (r - g)) // delta + 240
        return h % 360, s, mx

    @micropython.native
    def hsv_to_rgb12(h, s, v):
        h = int(h) % 360
        s = 4095 if s > 4095 else int(s)
        v = 4095 if v > 4095 else int(v)
        if s == 0:
            return v, v, v
        region = h // 60
        rem = (h - region * 60) * 4095 // 60
        p = (v * (4095 - s)) >> 12
        q = (v * (4095 - ((s * rem) >> 12))) >> 12
        t = (v * (4095 - ((s * (4095 - rem)) >> 12))) >> 12
        if region == 0:
            return v, t, p
        if region == 1:
            return q, v, p
        if region == 2:
            return p, v, t
        if region == 3:
            return p, q, v
        if region == 4:
            return t, p, v
        return v, p, q

    @micropython.native
    def rgb_to_hsv12(r, g, b):
        r, g, b = int(r), int(g), int(b)
        mx = max(r, g, b)
        mn = min(r, g, b)
        delta = mx - mn
        if delta == 0:
            return 0, 0, mx
        s = (delta * 4095) // mx
        if mx == r:
            h = (60 * (g - b)) // delta
        elif mx == g:
            h = (60 * (b - r)) // delta + 120
        else:
            h = (60 * (r - g)) // delta + 240
        return h % 360, s, mx

else:

    def hsv_to_rgb8(h, s, v):
        h = int(h) % 360
        s = 255 if s > 255 else int(s)
        v = 255 if v > 255 else int(v)
        if s == 0:
            return v, v, v
        region = h // 60
        rem = (h - region * 60) * 255 // 60
        p = (v * (255 - s)) // 255
        q = (v * (255 - ((s * rem) // 255))) // 255
        t = (v * (255 - ((s * (255 - rem)) // 255))) // 255
        if region == 0:
            return v, t, p
        if region == 1:
            return q, v, p
        if region == 2:
            return p, v, t
        if region == 3:
            return p, q, v
        if region == 4:
            return t, p, v
        return v, p, q

    def rgb_to_hsv8(r, g, b):
        r, g, b = int(r), int(g), int(b)
        mx = max(r, g, b)
        mn = min(r, g, b)
        delta = mx - mn
        if delta == 0:
            return 0, 0, mx
        s = (delta * 255) // mx
        if mx == r:
            h = (60 * (g - b)) // delta
        elif mx == g:
            h = (60 * (b - r)) // delta + 120
        else:
            h = (60 * (r - g)) // delta + 240
        return h % 360, s, mx

    def hsv_to_rgb12(h, s, v):
        h = int(h) % 360
        s = 4095 if s > 4095 else int(s)
        v = 4095 if v > 4095 else int(v)
        if s == 0:
            return v, v, v
        region = h // 60
        rem = (h - region * 60) * 4095 // 60
        p = (v * (4095 - s)) >> 12
        q = (v * (4095 - ((s * rem) >> 12))) >> 12
        t = (v * (4095 - ((s * (4095 - rem)) >> 12))) >> 12
        if region == 0:
            return v, t, p
        if region == 1:
            return q, v, p
        if region == 2:
            return p, v, t
        if region == 3:
            return p, q, v
        if region == 4:
            return t, p, v
        return v, p, q

    def rgb_to_hsv12(r, g, b):
        r, g, b = int(r), int(g), int(b)
        mx = max(r, g, b)
        mn = min(r, g, b)
        delta = mx - mn
        if delta == 0:
            return 0, 0, mx
        s = (delta * 4095) // mx
        if mx == r:
            h = (60 * (g - b)) // delta
        elif mx == g:
            h = (60 * (b - r)) // delta + 120
        else:
            h = (60 * (r - g)) // delta + 240
        return h % 360, s, mx
