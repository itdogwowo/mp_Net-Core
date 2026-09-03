"""
effects.py — 效果範例框架（pixel/effects 層）：單一自包含範例

框架（Effect 基類 / 登記表 / 波表快取 / 衝突檢查）在 lib/sw/effect_core.py，
本檔重導出框架 API（供 pixel_task 從 pixel.effects import effects 沿用）。

分工（重要）：
  - json 是唯一真源：id / name / params（含 program 畫波）都在 effects.json 手寫。
  - 畫波效果（breathing / eyes / wave）不需要 py 類別，program 寫在 json，
    由內建 Effect 直接播放（波表預算 + viper + 無浮點）。
  - 畫波寫不出來時，才在本檔寫 py 類別 + register()，靠 name 與 json 配對。
  - 本檔留一個教學範例 example_eyes：示範「py 手動寫 buffer」（override frame(t)
    寫 self._buf，對照舊專案 main.py 的 generator + _tempbuf），並示範兩種取幀：
    迭代器 next(eff) 逐幀、buffer eff.frame(t) 指定幀。
  - id/name/配對衝突不 raise：啟動時由 check_conflicts() 列印警告（對齊 boot GPIO 檢查）。

不碰硬體、不碰 bus、不碰 pixel_stream。
"""

# PC 直接執行自檢（python3 pixel/effects/effects.py）時，把 slave/ 根補進 sys.path
# 使 `from lib.sw.effect_core import ...` 成立（裝置上 lib 在根目錄，直接可 import）。
try:
    from lib.sw.effect_core import (Effect,register,load_json,resolve,get_params,make,dump,warm_up,clear_wave_cache,check_conflicts,)
    
except ImportError:
    import sys as _sys
    import os as _os
    _ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    if _ROOT not in _sys.path:
        _sys.path.insert(0, _ROOT)
    from lib.sw.effect_core import (
        Effect,
        register,
        load_json,
        resolve,
        get_params,
        make,
        dump,
        warm_up,
        clear_wave_cache,
        check_conflicts,
    )

# ══════════════════════════════════════════════════════════
# 如何寫一個效果（下手同事請照做）── 詳見 doc/02_guides/11_developing_effects.md
# ══════════════════════════════════════════════════════════
#
# 路 A ── 畫波類（首選，純 json，不用寫 py）
#   在 effects.json 加一段即可（id/name 手寫 + program 波形 + 空間分布參數）：
#
#     { "id": 5, "name": "comet", "pixel_n": 64,
#       "program": [
#         {"type": "math_now", "F": 1, "l_max": 3200, "l_lim": 100, "phi": 0, "end_Time": 120}
#       ],
#       "step": 1, "spacing": 2, "offset": 0, "speed": 1, "reverse": false }
#
#   框架會用內建 Effect 畫波播放：開機 warm_up() 先算好波表、viper 播放、無浮點。
#   波形段欄位：type / F / l_max / l_lim / phi / end_Time（pulse 型另加 pulse）
#     type ∈ keep / math_now / square_wave_now / pulse_wave / pulse / starter
#
# 路 B ── 畫波寫不出來，想自訂邏輯（override frame(t)，繼承 Effect）
#
#   class my_effect(Effect):
#       def frame(self, t):
#           ... 自訂邏輯，寫進 self._buf ...
#           return self._buf
#   register(my_effect)   # 效果 name = "my_effect"；id/name/params 在 effects.json 手寫
#
# 路 C ── 完全自訂類別（不繼承 Effect，實作 __next__/restart/seek/release）
#   class xxx: ... 再 register(xxx)。
# ══════════════════════════════════════════════════════════


# ── 教學範例：py 手動寫 buffer（對照舊專案 main.py 的 generator 寫法）─────────
class example_eyes(Effect):
    """教學範例：py 手動寫 buffer + 迭代器。

    對照舊專案 temp/1/main.py 的 wave_list_assign_next 寫法：

        舊寫法（generator + 手動 buffer）：
            _tempbuf = [l_lim] * led_no              # ① 建 buffer
            _wave_history = list(_gen)               # ② 預算波表
            while True:
                for i in range(led_no):
                    _tempbuf[i] = _wave_history[((counter*step)+(i*spacing)) % max]
                yield _tempbuf.copy()                # ③ 迭代器吐幀

        新框架對照（繼承 Effect，override frame(t)）：
            ① buffer   → self._buf（array('H')，基類 __init__ 已建，長度 pixel_n）
            ② 波表    → self._wave / self._total（基類已預算好，warm_up() 快取）
            ③ 迭代器  → next(eff) 逐幀推進（基類 __next__ 呼叫 frame(t) 再 _t+1）
            ④ 手動填  → frame(t) 內自己寫 self._buf[i]，再 return self._buf

    兩種取幀方式：
      - 迭代器：next(eff) → 逐幀推進，回傳下一幀 array('H')
      - buffer ：eff.frame(t) → 取指定幀 t 的 array('H')（不推進內部時間）
    """
    DEFAULT_PROGRAM = [
        {"type": "keep",     "F": 1, "l_max": 0,    "l_lim": 0,   "phi": 0,    "end_Time": 60},
        {"type": "math_now", "F": 5, "l_max": 100,  "l_lim": 20,  "phi": 3071, "end_Time": 100},
        {"type": "math_now", "F": 5, "l_max": 1023, "l_lim": 100, "phi": 3071, "end_Time": 200},
        {"type": "math_now", "F": 5, "l_max": 1023, "l_lim": 200, "phi": 1023, "end_Time": 320},
    ]

    def frame(self, t):
        """手動寫 buffer：每顆 pixel 從波表取（對照 main.py 的 _tempbuf 迴圈）。"""
        total = self._total
        if total <= 0:
            return self._buf
        for i in range(self.pixel_n):
            self._buf[i] = self._wave[(t * self.step + i * self.spacing + self.offset) % total]
        return self._buf


register(example_eyes)


# ── 珍珠鏈：畫完波後「批量派發 + 控制間距」────────────────────
class pearl_chain(Effect):
    """珍珠鏈：一顆珍珠波形，派發成 N 顆、間距 D 格，順序流動。

    解決「spacing 只能給一顆連續珍珠」的限制：這裡把「時間波形」與「空間派發」
    解耦，畫好一顆珍珠（program 波形 = 升起→保持→下降）後，獨立控制：
      - pearl_n   ：派發幾顆珍珠
      - pearl_gap ：珍珠間距（格），一顆珍珠寬度 = D 格

    算法：
      phase_step = total / D        # 相鄰像素相位差（間距 D 格 = 一顆珍珠寬）
      第 i 顆的相位 = (t*step + i*phase_step + offset) % total
      只在前 N*D 格派發，其餘熄燈。

    json 用法（effects.json）：
      { "id": 6, "name": "pearl_chain", "pixel_n": 64,
        "program": [ 單顆珍珠波形（升起→保持→下降） ],
        "step": 1, "spacing": 0, "offset": 0, "speed": 1, "reverse": false,
        "pearl_n": 4, "pearl_gap": 12 }
    """
    DEFAULT_PROGRAM = [
        {"type": "math_now", "F": 5, "l_max": 4095, "l_lim": 0, "phi": 3071, "end_Time": 8},
        {"type": "keep",     "F": 1, "l_max": 4095, "l_lim": 0, "phi": 0,    "end_Time": 16},
        {"type": "math_now", "F": 5, "l_max": 4095, "l_lim": 0, "phi": 1023, "end_Time": 24},
    ]

    def __init__(self, name, params=None):
        super().__init__(name, params)
        params = params or {}
        # 珍珠派發參數（畫完波後才做的「派發」步驟）
        self.pearl_n = int(params.get("pearl_n", 4))      # 派發幾顆
        self.pearl_gap = int(params.get("pearl_gap", 12)) # 間距（格）

    def frame(self, t):
        """手動寫 buffer：把單顆珍珠波表，按間距 D 派發成 N 顆。"""
        total = self._total
        if total <= 0:
            return self._buf
        n = self.pixel_n
        N = self.pearl_n
        D = self.pearl_gap
        phase_step = total / D   # 相鄰像素相位差（間距 D 格 = 一顆珍珠寬）
        for i in range(n):
            if i < N * D:
                self._buf[i] = self._wave[int((t * self.step + i * phase_step) + self.offset) % total]
            else:
                self._buf[i] = 0
        return self._buf


register(pearl_chain)


# ── UART 電機正弦波效果（uart_motor_sine）────────────────────
# 設定方法：program 段序列在 effects.json 手寫，段 type：
#   {"type":"uart_motor_sine","direction":"B","speed_percent":100,
#    "speed_curve":"Sine","end_Time":500}
#   {"type":"uart_motor_stop","end_Time":750}
# 波形不在這裡計算：即时速度 bell 由框架 PixelMathMethod（整數正弦）
# __init__ 時預計算成 raw 表，frame() 只查表。
# 輸出走 write:"w"：全像素同值 = raw << 4；0x00 全速收 / 0x80 停 /
# 0xFF 全速伸都是有效命令（render 原樣收）。

from lib.sw.PixelMathMethod import mt as _mt

_STOP_RAW = 128


def _sine_bell_raws(frames, max_speed):
    """単方向正弦 bell 的整數速度%表（框架正弦，無浮點）。"""
    frames = max(1, int(frames))
    comp = _mt.compile([{
        'type': 'math_now', 'F': 5, 'l_max': 4095, 'l_lim': 0, 'phi': 0,
        'end_Time': frames,
    }])
    out = []
    for p in range(frames):
        v = _mt.value_at(comp, p)           # wave01: 2048..4095 升降 bell
        percent = (v - 2048) * 100 // 2047  # ~ 100*sin(pi*p/frames)
        if percent < 0:
            percent = 0
        elif percent > 100:
            percent = 100
        out.append((percent * max_speed + 50) // 100)
    return out


def _motor_value(direction, speed_percent):
    """direction A/B + 力出 0..100 → raw byte（A: 收，B: 伸）。"""
    speed = max(0, min(int(speed_percent), 100))
    if speed <= 0:
        return _STOP_RAW
    if str(direction).upper() == 'B':
        return 128 + ((127 * speed + 50) // 100)
    return 128 - ((128 * speed + 50) // 100)


class uart_motor_sine:
    """正弦波設定方法：json 段序列 + write:"w" raw<<4。

    __init__ 把整段 program 預計算成 raw 表（框架整數正弦），
    frame(t) 只查表 + raw<<4；決定性、無狀態。
    """

    def __init__(self, name, params=None):
        params = params or {}
        self.name = name
        self.id = params.get('id')
        self.pixel_n = max(1, int(params.get('pixel_n', 1)))
        self.program = params.get('program') or []
        self.cycles = int(params.get('cycles', 1))
        self.hold_raw = max(0, min(int(params.get('hold_raw', 128)), 255))
        self._t = 0
        from array import array as _array
        self._buf = _array('H', [self.hold_raw << 4] * self.pixel_n)

        segs = []
        prev = 0
        for seg in self.program:
            end = int(seg.get('end_Time', 0))
            if end <= prev:
                raise ValueError('uart_motor_sine: end_Time 必須递增')
            kind = str(seg.get('type', 'uart_motor_stop'))
            if kind == 'uart_motor_sine':
                direction = str(seg.get('direction', '')).upper()
                if direction not in ('A', 'B'):
                    raise ValueError('uart_motor_sine: direction 必須 A/B')
                speed = max(0, min(int(seg.get('speed_percent', 100)), 100))
                curve = str(seg.get('speed_curve', 'Sine')).lower()
            else:
                direction, speed, curve = '', 0, 'linear'
            segs.append((prev, end, kind, direction, speed, curve))
            prev = end
        self._total = prev

        # 預計 raw 表（stop 段 = 0x80；sine 段 = 框架正弦 bell → raw）
        table = [self.hold_raw << 4] * self._total
        for start, end, kind, direction, maximum, curve in segs:
            L = end - start
            if kind != 'uart_motor_sine':
                for p in range(start, end):
                    table[p] = _STOP_RAW << 4
                continue
            if curve == 'linear':
                prof = [p * 100 // L for p in range(L)]
            else:
                prof = _sine_bell_raws(L, maximum)
            for p in range(L):
                raw = _motor_value(direction, prof[p])
                table[start + p] = raw << 4
        self._table = _array('H', table)

    def frame(self, t):
        absolute = int(t)
        if self._total > 0 and (self.cycles < 0 or absolute < self._total * self.cycles):
            v12 = self._table[absolute % self._total]
        else:
            v12 = self.hold_raw << 4
        buf = self._buf
        for i in range(self.pixel_n):
            buf[i] = v12
        return buf

    def release(self):
        pass

    def restart(self):
        self._t = 0

    def seek(self, t):
        self._t = int(t)

    def __next__(self):
        buf = self.frame(self._t)
        self._t += 1
        return buf


register(uart_motor_sine)


if __name__ == "__main__":
    # ── PC 快速自檢（不依賴硬體）：讀取真實 effects.json ──
    import os
    import json

    _here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(_here, "effects.json")) as _f:
        load_json(json.load(_f).get("effects", []))

    print("已登記:", dump())
    _conf = check_conflicts()
    if _conf:
        print("⚠️ 衝突警告（人肉判斷修正）：")
        for _c in _conf:
            print("  " + _c)
    else:
        print("✅ 無 id/name/配對衝突")

    # 每個 json 效果：make + frame 長度/值域檢查
    for _name, _eid in sorted(dump().items(), key=lambda kv: kv[1]):
        eff = make(_name)
        buf = next(eff)
        assert len(buf) == eff.pixel_n, \
            "{} 輸出長度應為 pixel_n，got {}".format(_name, len(buf))
        assert all(0 <= v <= 4095 for v in buf), "{} 值域必須 0-4095".format(_name)
        print("{}: id={} pixel_n={} frame[0] 前 4 值 = {}".format(
            _name, _eid, eff.pixel_n, list(buf)[:4]))

    # restart / seek 決定性（eyes 是畫波效果）
    eff = make("eyes")
    f0 = list(next(eff))
    f1 = list(next(eff))
    eff.seek(0)
    assert list(next(eff)) == f0, "seek(0) 後應重現 frame 0"
    eff.restart()
    assert list(next(eff)) == f0, "restart 後應重現 frame 0"
    assert f0 != f1, "frame 0 與 frame 1 應不同（eyes 有空間分布）"

    # breathing 全像素同值（json spacing=0）
    eff = make("breathing")
    b0 = next(eff)
    assert all(v == b0[0] for v in b0), "breathing 應全像素同值"

    # 教學範例：手動寫 buffer（override frame）+ 迭代器 vs buffer
    print("--- 教學範例 example_eyes：手動 buffer + 迭代器 ---")
    eff = make("example_eyes")
    print("  迭代器 next(eff) frame0 前 4 值 =", list(next(eff))[:4])
    print("  迭代器 next(eff) frame1 前 4 值 =", list(next(eff))[:4])
    print("  buffer  eff.frame(5) 前 4 值   =", list(eff.frame(5))[:4])
    eff.seek(0)
    assert list(next(eff)) == list(eff.frame(0)), "next 與 frame(0) 應一致（seek(0) 後）"

    print("OK — effects 目錄（框架在 lib/sw/effect_core.py）驗證通過")
