"""
effect_core.py — pixel 效果框架（lib 層）：Effect 基類 + 登記表 + 波表快取 + 衝突檢查

職責（框架，與「實際寫出來的燈效」分離）：
  - Effect 基類：時間波形 + 空間分布 → 一整幀 array('H')（0-4095）——「畫波」的通用播放器
  - 登記表（內部緩衝）：name → {id, cls, params}
      json 是唯一真源：id / name / params（含 program 畫波）都在 effects.json 手寫。
      py 只「補充」畫波寫不出來的效果：register(類別)，靠 name 與 json 配對。
      沒有 py 類別的 json entry → 用內建 Effect 畫波播放（program 來自 json）。
  - 波表 module 快取 + warm_up() 開機預算（啟動即算、off 即丟、重啟重算）
  - check_conflicts()：id/name/配對衝突收集成警告行，供啟動階段列印
    （對齊 boot.py 的 GPIO 檢查：先收集、後列印，人肉判斷修正）

具體效果一律放 slave/pixel/effects/（effects.json + 效果類別檔），本模組不寫任何效果。
如何寫效果：見 doc/02_guides/11_developing_effects.md（路 A/B/C）。
不碰硬體、不碰 bus、不碰 pixel_stream。
"""

from array import array as _array
from lib.sw.PixelMathMethod import mt

try:
    import micropython
    _MP = True
except ImportError:
    _MP = False
    micropython = None


# ── 播放迴圈（viper / 純 Python 雙路徑）：波緩衝 index + 乘轉加 ──
# wave 已預先算好（array('H')，長度 total），frame 熱路徑只做：讀波 + 加法 + 單次減法取模。
# g 進入時已正規化到 [0,total)，spacing 已 < total → g+spacing 最多 < 2*total，
# 單次 `if g>=total: g-=total` 即完成取模（避免 MicroPython 昂貴的 %）。

if _MP:

    @micropython.viper
    def _fill_fwd(buf, wave, n: int, g: int, spacing: int, total: int):
        pb = ptr16(buf)
        pw = ptr16(wave)
        for i in range(n):
            pb[i] = pw[g]
            g += spacing
            if g >= total:
                g -= total

    @micropython.viper
    def _fill_rev(buf, wave, n: int, g: int, spacing: int, total: int):
        pb = ptr16(buf)
        pw = ptr16(wave)
        for i in range(n):
            pb[n - 1 - i] = pw[g]
            g += spacing
            if g >= total:
                g -= total

else:

    def _fill_fwd(buf, wave, n, g, spacing, total):
        for i in range(n):
            buf[i] = wave[g]
            g += spacing
            if g >= total:
                g -= total

    def _fill_rev(buf, wave, n, g, spacing, total):
        for i in range(n):
            buf[n - 1 - i] = wave[g]
            g += spacing
            if g >= total:
                g -= total


# ── 效果登記表（py 類別 + json 參數的「緩衝/配對」）──────────
# name -> {"id": int|None, "cls": class|None, "params": dict|None}
#   id / name / params 一律由 effects.json 提供（你手寫）；py 只 register 演算法類別。
#   _EFFECTS 就是內部緩衝：載入 json 時按 name 把 cls 與 params 配對起來；
#   配不上的（json 沒類別 / 類別沒 json）→ 啟動時 check_conflicts() 列印警告。
# 衝突不 raise：記錄進 _WARNINGS，啟動時由 check_conflicts() 列印（人肉判斷修正）。
_EFFECTS = {}
_IDS = {}       # id -> name（由 json 提供）
_WARNINGS = []  # 啟動檢查用：收集的 id/name 衝突警告行


def _warn(msg):
    if msg not in _WARNINGS:
        _WARNINGS.append(msg)


def load_json(effects_list):
    """載入 effects.json 的 effects[]：id + name + params（含 program 畫波）全由 json 提供。

    py 只「補充」畫波寫不出來的效果（register 類別）；json 是單一真源。
    載入時按 name 把兩者配對（內部緩衝）：
    - 同一 name 重複（json 內）→ 警告 + 跳過
    - 同一 id 被不同 name 使用 → 警告 + 跳過（id 由你手寫，衝突要你人肉判斷）
    - 沒有 py 類別的 entry → 就是畫波效果，用內建 Effect 播放（program 來自 json）
    """
    for e in effects_list:
        eid = int(e["id"])
        name = e["name"]
        entry = _EFFECTS.setdefault(name, {"id": None, "cls": None, "params": None})
        if entry["params"] is not None:
            _warn("EFFECT NAME CONFLICT: name={} 在 json 重複，跳過".format(name))
            continue
        if eid in _IDS and _IDS[eid] != name:
            _warn("EFFECT ID CONFLICT: id={} 已被 {} 使用，{} 跳過（請改 id）".format(
                eid, _IDS[eid], name))
            continue
        entry["params"] = e
        entry["id"] = eid
        _IDS[eid] = name


def register(cls):
    """登記 py 補充類別（畫波寫不出來時才需要）。name = cls.__name__。

    不自動配 id/name —— id/name 由 effects.json 手寫提供，靠 name 與 json 配對。
    同一 name 重複登記 → 警告 + 跳過（保留先登記的）。
    """
    name = cls.__name__
    entry = _EFFECTS.setdefault(name, {"id": None, "cls": None, "params": None})
    if entry["cls"] is not None:
        _warn("EFFECT NAME CONFLICT: name={} 已登記，跳過重複".format(name))
        return
    entry["cls"] = cls


def resolve(ref):
    """ref = id(int) 或 name(str) → 效果類別。

    沒有 py 補充類別的 json entry（畫波效果）→ 回傳內建 Effect（畫波播放器）。
    """
    name = _IDS[ref] if isinstance(ref, int) else ref
    entry = _EFFECTS.get(name)
    if entry is None:
        return None
    return entry["cls"] or Effect


def get_params(ref):
    """ref = id 或 name → 效果參數 dict（來自 json；沒 json entry 回 None）。"""
    name = _IDS[ref] if isinstance(ref, int) else ref
    return _EFFECTS[name]["params"]


def make(ref):
    """ref = id 或 name → 建立效果實例（每次播放都該重建一份）。

    有 py 補充類別 → 用該類別；否則用內建 Effect 畫波（program 來自 json params）。
    """
    name = _IDS[ref] if isinstance(ref, int) else ref
    entry = _EFFECTS[name]
    cls = entry["cls"] or Effect
    return cls(name, entry["params"])


def dump():
    """回傳 name -> id 對照（除錯用；json 沒提供的效果 id 為 None）。"""
    return {name: _EFFECTS[name]["id"] for name in _EFFECTS}


# 所有效果都需要的 json 參數（id/name 在 entry 層；以下為數值層）
REQUIRED_PARAMS = ("pixel_n", "step", "spacing", "offset", "speed", "reverse")


def check_conflicts():
    """啟動檢查：回傳 id/name/配對衝突警告行（無衝突 → []）。

    對齊 boot 的 GPIO 檢查：初始化時收集、啟動階段列印，供人肉判斷修正。
    檢查項目：
      1. 載入期間收集的警告（name 重複 / id 衝突）
      2. py 有補充類別但 json 沒提供（無 id/params，不會被播放）—— 表示該 py 類別沒接上 json
      3. json entry 缺必填參數（所有效果都需要 pixel_n/step/spacing/offset/speed/reverse）
      4. json entry 缺 program（畫波效果沒 program 就無法播放，除非有 py 補充類別）
    """
    lines = list(_WARNINGS)
    for name, entry in _EFFECTS.items():
        has_cls = entry["cls"] is not None
        has_params = entry["params"] is not None
        if has_cls and not has_params:
            lines.append("EFFECT 無 json: py 有效果 {!r} 但 effects.json 沒有 id/params（不會被播放，請補 json entry）".format(name))
        if has_params:
            missing = [k for k in REQUIRED_PARAMS if k not in entry["params"]]
            if missing:
                lines.append("EFFECT 缺參數: {!r} 缺 {}（所有效果都需要 {}/）".format(
                    name, ", ".join(missing), ", ".join(REQUIRED_PARAMS)))
            # 畫波效果（無 py 補充類別）必須有 program，否則播不出東西
            if not has_cls and not entry["params"].get("program"):
                lines.append("EFFECT 缺 program: {!r} 是畫波效果但 json 沒有 program（無法播放，請補 program 或用 py 補充類別）".format(name))
    # 去重（保留順序）
    seen = set()
    out = []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


# ── 波表快取（module 層）：儲存每個效果的波表，首次算好後共享 ──
# 舊方法常駐一張 65536 點 sin 全表（128KB）；現在只需存效果自己的波表
# （end_Time × 2B，eyes 才 640B）。同 name + 同 program 只算一次，重啟/重建零重算。
_WAVE_CACHE = {}   # name -> {"key": repr(program), "total": int, "wave": array('H')}


def _wave_key(program):
    return repr(program)


def _get_or_build_wave(name, program):
    """取 name 的波表；program 沒變就命中快取，變了就重算。回傳 (wave, total)。"""
    key = _wave_key(program)
    entry = _WAVE_CACHE.get(name)
    if entry is not None and entry["key"] == key:
        return entry["wave"], entry["total"]
    comp = mt.compile(program)
    total = comp[-1][1] if comp else 0
    if total > 0:
        wave = _array('H', [mt.value_at(comp, x) for x in range(total)])
    else:
        wave = _array('H', [0])
    _WAVE_CACHE[name] = {"key": key, "total": total, "wave": wave}
    return wave, total


def warm_up():
    """開機預計算：把畫波效果的波表先算好，掩蓋首次播放的計算成本。

    之後 frame 只做 index 讀取、零重算；重啟/重建 effect 直接命中快取。
    波形（program）唯一真源 = effects.json；py 補充類別（override frame / 完全自訂）
    沒有 program、不需要波表（自己合成）。回傳已預算的波表數。
    """
    for name, entry in _EFFECTS.items():
        params = entry.get("params")
        if params is not None:
            program = params.get("program")
            if program:
                _get_or_build_wave(name, program)
    return len(_WAVE_CACHE)


def clear_wave_cache():
    """清空波表快取（例如 effects.json 重載後需要重新預算）。"""
    _WAVE_CACHE.clear()


# ── Effect 類別 ──────────────────────────────────────────
class Effect:
    """效果基類：時間波形 + 空間分布 → 一整幀 array('H')（0-4095）。

    這是「畫波」的通用播放器：program（波形）與空間分布參數都來自 effects.json
    （單一真源）。畫波寫不出來的效果才用 py 補充類別（override frame / 不繼承 Effect）。
    frame(t) 是決定性、無狀態的：每顆 pixel i 的值 = pattern_value_at(program, 相位)。
    相位 = (t // speed) * step + i * spacing + offset（對齊舊 wave_list_assign_next）。
    """
    DEFAULT_PROGRAM = []

    def __init__(self, name, params=None):
        self.name = name
        params = params or {}
        self.id = params.get("id")
        self.program = params.get("program") or list(self.DEFAULT_PROGRAM)
        self.pixel_n = int(params.get("pixel_n", 1))
        self.step = int(params.get("step", 1))
        self.spacing = int(params.get("spacing", 1))
        self.offset = int(params.get("offset", 0))
        self.speed = int(params.get("speed", 1))
        self.reverse = bool(params.get("reverse", False))
        self._t = 0
        self._buf = _array('H', [0] * self.pixel_n)
        # 波表：module 層快取（同 name + 同 program 只算一次），開機 warm_up() 已預先算好。
        self._wave, self._total = _get_or_build_wave(self.name, self.program)
        self._spacing_mod = self.spacing % self._total if self._total else 0

    def release(self):
        """off 時丟棄實例的波表引用（波表本身在 module 快取，重啟/重建零重算）。"""
        self._wave = None

    def frame(self, t):
        """回傳第 t 幀（array('H')，pixel_n 個值，全 0-4095）。決定性、無狀態。

        熱路徑只做：index 讀波 + 加法 + 單次減法取模（乘數轉加數，無 sin / 無除法 / 無 %）。
        """
        total = self._total
        if total <= 0:
            return self._buf
        buf = self._buf
        n = self.pixel_n
        g = ((int(t) // self.speed) * self.step + self.offset) % total
        if self.reverse:
            _fill_rev(buf, self._wave, n, g, self._spacing_mod, total)
        else:
            _fill_fwd(buf, self._wave, n, g, self._spacing_mod, total)
        return buf

    def restart(self):
        self._t = 0

    def seek(self, t):
        self._t = int(t)

    def __next__(self):
        b = self.frame(self._t)
        self._t += 1
        return b
