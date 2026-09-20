"""
timer.py — 絕對時間週期計時器（全系統共用）

═══════════════════════════════════════════════════════════════════
設計原則（與使用者定案一致）
═══════════════════════════════════════════════════════════════════

1. 絕對時間，零漂移
   start() 設定基準 _t0 之後【永不修改】。所有到期時刻都由 _t0 推導，
   所以 done() 什麼時候回報、工作做多久，都不會讓後續節拍位移。

2. done() 只回報，不影響時刻
   回傳單一整數：0 = 準時、1 = 超時（不建 tuple → 熱路徑零配置）。
   超時 = 「被通知到期」到「回報完成」之間，用掉時間 >= 本段期長
   （= 吃掉了一整個週期）。

3. 倒數顯示由應用層自己算
   本類別【不】維護顯示用計數器。需要顯示時用 remaining_ms() / period_ms()
   自行 -1；框架不代管 UI 狀態。

4. 邊界語意（三個魔鬼細節，實測驗證過）
   a. 段邊界：e == off[k] 屬於「第 k 段剛到期」（含等號）
   b. 週期交界：e == total（rel == 0）歸屬【上一圈最後一段】，不歸下一圈
   c. 不循環收尾：e == total 時先讓最後一段觸發，下一輪 poll 才關機
      （否則每次都會吃掉最後一段）
   另外：內部去重必須用「從基準算起的總段數」，用「週期內段數」會因歸零而只觸發一次。

═══════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════

    from lib.sys.timer import Timer

    self._t = Timer()
    self._t.start([2000, 3000], loop=True)     # 2 秒一段、3 秒一段，循環

    # 主迴圈每輪：
    if self._t.poll():
        do_work()                              # 夠鐘了
        if self._t.done():                     # 回報完成（1 = 本次超時）
            log("吃掉一個週期")

    # 應用層自己的倒數顯示：
    lbl.set_text(str(self._t.remaining_ms() // 1000))

適用：超時回滾（bus_speed）、週期廣播、看門狗倒數、UI 倒數。
不適用：產幀／效果長度（end_Time、maxF、cycles 是【幀數】不是時間）、
        渲染節拍（RenderTask 用 ticks_us 微秒節拍，熱路徑不要包一層）。

跨核心注意：Timer 實例不可跨核心共用（Python 物件跨核 = race）。
每個 task 自己持有自己的實例；需要給別的核或遠端看時，只把算出來的
毫秒數字推進 bus.shared（同 hw_manager 的 snapshot 模式）。
"""

from time import ticks_ms as _ticks_ms
from time import ticks_diff as _ticks_diff

# ══════════════════════════════════════════════════════════════════
# 加速技巧（真機實測，非推測）
# ══════════════════════════════════════════════════════════════════
#
# 1) 模組層函式綁定 ✅ 有效
#    `time.ticks_ms()` 每次都是「全域查 time → 屬性查 ticks_ms」，
#    綁成模組層區域名後只剩一次全域查（真機實測 8.66us → 7.15us，-17%）。
#
# 3) micropython.opt_level(3) ❌ 對本模組無效
#    它只影響 assert / __debug__，不是機器碼編譯。實測 19.61us → 19.61us。
#
# 4) @micropython.viper 即使哪天支援也不建議用在這裡
#    viper 對「呼叫一般 Python 函式（如 ticks_ms）」一律視為 object，每個運算
#    都要顯式 int() 轉型；且 viper 修飾的類別方法在 class body 執行時就編譯，
#    型別不符會讓【整個模組載入失敗】。本模組的整數數學只佔 poll() 一小部分
#    （大宗是呼叫成本），不值得這個風險。
# ══════════════════════════════════════════════════════════════════
# 3) @micropython.viper 熱路徑編譯 ✅【實測 2.2~3.2x，已採用】
#
# ⚠️ 探測陷阱（實際踩過，務必看懂）：
#    `micropython` 模組【沒有任何名為 viper / native 的屬性】：
#        hasattr(micropython, "viper")  → False
#        dir(micropython)               → 不含 viper
#        micropython.viper              → AttributeError
#        from micropython import viper  → ImportError
#    在 ESP32-C3 v1.28.0 與 ESP32-S3 v1.29.0-preview 上皆是如此。
#    但 **`@micropython.viper` 語法本身是可用的** —— 它是【編譯器認得的名字】，
#    不是執行期屬性。所以用 hasattr / try-import 判斷會誤判為「不支援」，
#    白白放棄 3 倍加速（本庫第一版就是這樣錯的）。
#
# 正解：把熱路徑寫成【原始碼字串】，用 exec() 讓編譯器看到裝飾器，
#       再把產生的函式綁進類別。viper 限制（呼叫一般 Python 函式的回傳值、
#       tuple 下標都視為 object，必須 int() 轉型）都已在此原始碼內處理。
#
# 實測（ESP32-S3 @160MHz, v1.29.0-preview）：
#     poll() plain 86~96us → viper 30.0us（2.9~3.2x），名目序列完全相同。
#
# 安全設計：exec 包在 try/except 內 —— 不支援 viper 的埠（或用 CPython 跑
# 離線測試）會自動退回純 Python 版本，絕不因加速而讓模組載入失敗。
# ══════════════════════════════════════════════════════════════════

_VIPER_SRC = """
@micropython.viper
def poll(self) -> int:
    if not self._run:
        return 0
    e = int(_ticks_diff(_ticks_ms(), int(self._t0)))
    total = int(self._total)
    n = int(self._n)
    stop_after = 0
    if self._loop:
        cycle = e // total
        rel = e - cycle * total
        if cycle > 0 and rel < int(self._off[0]):
            cycle -= 1
            rel = total
    else:
        cycle = 0
        rel = e
        if rel >= total:
            rel = total
            stop_after = 1
    off = self._off
    seg = -1
    for k in range(n):
        if rel >= int(off[k]):
            seg = k
        else:
            break
    if seg < 0:
        nth = cycle * n
    else:
        nth = cycle * n + (seg + 1)
    if nth > int(self._done_upto):
        self._done_upto = nth
        si = seg if seg >= 0 else n - 1
        self._i = si
        self._fired = 1
        nom = cycle * total + int(off[si])
        self._nom = nom
        self._last_fire = int(self._t0) + nom
        self._fire_at = _ticks_ms()
        return 1
    if stop_after:
        self._run = 0
    return 0

@micropython.viper
def done(self) -> int:
    if not self._run or not self._fired:
        return 0
    if int(_ticks_diff(_ticks_ms(), int(self._last_fire))) >= int(self._seg[int(self._i)]):
        over = 1
    else:
        over = 0
    self._fired = 0
    return over
"""

_viper_poll = None
_viper_done = None
try:
    _ns = {"_ticks_ms": _ticks_ms, "_ticks_diff": _ticks_diff}
    exec(_VIPER_SRC, _ns)
    _viper_poll = _ns["poll"]
    _viper_done = _ns["done"]
except Exception:                        # 不支援 viper → 保持 None，用純 Python 版
    _viper_poll = None
    _viper_done = None

__all__ = ("Timer",)


class Timer:
    """週期計時器：時間陣列 + 可選循環；poll() 問、done() 回報。"""

    # 固定欄位（MicroPython 友善：無 instance dict、熱路徑零配置）
    __slots__ = (
        "_seg",          # 各段期長 tuple
        "_off",          # 各段累計結束點（週期內）tuple
        "_total",        # 一圈總長 = _off[-1]
        "_n",            # 段數
        "_t0",           # ★ 絕對時間基準（start 之後永不修改）
        "_i",            # 剛到期的段索引
        "_done_upto",    # 已回報過的總段數（去重；跨圈遞增，不歸零）
        "_run",          # 是否啟動中
        "_loop",         # 是否循環
        "_fired",        # 已通知到期、尚未 done()
        "_nom",          # 本次到期的名目時刻（絕對，供 log / 除錯）
        "_fire_at",      # 本次實際被抓到的時刻（僅診斷：與名目相減 = 抖動）
        "_last_fire",    # 上一次名目到期點（供 remaining_ms；無到期時 = 基準）
    )

    def __init__(self):
        self._seg = (1000,)
        self._off = (1000,)
        self._total = 1000
        self._n = 1
        self._t0 = 0
        self._i = 0
        self._done_upto = 0
        self._run = 0
        self._loop = 1
        self._fired = 0
        self._nom = 0
        self._fire_at = 0

    # ══════════════════════════════════════════════════════════
    # 設定
    # ══════════════════════════════════════════════════════════

    def start(self, ms_list=(1000,), loop=True):
        """設定週期表並起算。

        ms_list : 期長陣列（ms），或單一 int。每段依序到期，loop=True 用完回頭。
                  非法值由本方法自行夾正（<=0 → 1），呼叫端不需防護。
        loop    : True = 循環；False = 跑完全部段後自動停止。

        ★ 這是唯一設定 _t0 的地方；之後任何呼叫都不會更動時間基準。
        """
        if isinstance(ms_list, int):
            ms_list = (ms_list,)
        seg = []
        for v in ms_list:
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = 1000
            seg.append(v if v > 0 else 1)
        if not seg:
            seg = [1000]

        off = []
        acc = 0
        for v in seg:
            acc += v
            off.append(acc)

        self._seg = tuple(seg)
        self._off = tuple(off)
        self._total = acc
        self._n = len(seg)
        self._i = 0
        self._done_upto = 0
        self._run = 1
        self._loop = 1 if loop else 0
        self._fired = 0
        self._fire_at = 0
        self._t0 = _ticks_ms()
        # _nom = 0 表示「尚無到期」；_last_fire 從基準起算，讓 remaining_ms()
        # 在首次到期前回 0（實機測試修正：不能拿 _nom 當基準，兩者語意不同）
        self._nom = 0
        self._last_fire = self._t0

    def stop(self):
        """停止（可隨時再 start 重新起算）。"""
        self._run = 0
        self._fired = 0

    def _done_py(self) -> int:
        """回報本次執行完成 → 0 = 準時、1 = 超時。

        超時 = 從【名目到期時刻】到現在，用掉時間 >= 本段期長。
        ★ 基準用 _last_fire（名目時刻），不是偵測時刻 —— 這是準確性關鍵：
          若主迴圈忙碌導致 poll() 晚了 K ms 才回報，用偵測時刻當基準會讓
          K ms 憑空消失、把逾時誤判為準時（真機實測：期長 200ms、工作 50ms、
          偵測延遲 300ms → 舊寫法回 0，實際已逾時 150ms）。
        ★ 只是回報：不推進時間表、不改變任何到期時刻（零漂移）。
        ★ 回傳單一整數，不建 tuple → 不產生垃圾、不觸發 GC。
        未到期或已停止時呼叫 → 回 0（安全，不拋錯）。
        ★ viper 註記：viper 對「呼叫一般 Python 函式」的回傳值一律視為
          object，故 _ticks_diff/_ticks_ms 與 tuple 下標都需 int() 轉型。
        """
        if not self._run or not self._fired:
            return 0
        if int(_ticks_diff(_ticks_ms(), int(self._last_fire))) >= int(self._seg[int(self._i)]):
            over = 1
        else:
            over = 0
        self._fired = 0
        return over

    # ══════════════════════════════════════════════════════════
    # 詢問
    # ══════════════════════════════════════════════════════════

    def _poll_py(self):
        """0 = 未夠鐘；1 = 夠鐘（可重複呼叫；同一次到期只回一次 1）。

        只讀絕對時間表 → 與 done() 的呼叫時機完全無關。
        太晚才 poll（工作超時導致跨過多個到期點）時，只回報【最新】那一個，
        中間漏掉的不補發 —— 對超時／週期廣播場景這是要的行為。

        ★ viper 註記：_ticks_* 與 tuple 下標對 viper 都是 object，需 int() 轉型。
        """
        if not self._run:
            return 0
        e = int(_ticks_diff(_ticks_ms(), int(self._t0)))
        total = int(self._total)
        n = int(self._n)
        stop_after = 0

        if self._loop:
            cycle = e // total
            rel = e - cycle * total
            if cycle > 0 and rel < int(self._off[0]):
                # 跨圈但尚未走完新圈的第 1 段 → 剛到期的是【上一圈的最後一段】。
                # 把 cycle 退一格讓 cycle 與 seg 落在同一個週期（rel == 0 的
                # 週期交界也走這條；否則名目會多算一整圈、_last_fire 跟著錯）。
                cycle -= 1
                rel = total
        else:
            cycle = 0
            rel = e
            if rel >= total:
                rel = total        # 已越過全部：停在最後一段
                stop_after = 1

        # seg = 【本週期內】剛結束的段索引（-1 = 本週期尚無段結束）
        off = self._off
        seg = -1
        for k in range(n):
            if rel >= int(off[k]):
                seg = k
            else:
                break

        # ★ 不可因 seg < 0 就 return 0：
        #   seg = -1 代表「本週期尚無段結束」，但上一個週期的最後一段可能已經
        #   到期（例：期長 1000，第一次 poll 在 1300 → 名目 1000 已過）。
        #   正確算法是把已完成段數算成 cycle * n（前面完整週期的所有段），
        #   再由 nth vs _done_upto 決定是否回報；seg < 0 時回報的是上一週期的
        #   最後一段（si = n - 1）。
        if seg < 0:
            nth = cycle * n
        else:
            nth = cycle * n + (seg + 1)

        if nth > int(self._done_upto):
            self._done_upto = nth
            si = seg if seg >= 0 else n - 1
            self._i = si
            self._fired = 1
            nom = cycle * total + int(off[si])
            self._nom = nom
            self._last_fire = int(self._t0) + nom
            self._fire_at = _ticks_ms()
            return 1
        if stop_after:
            self._run = 0
        return 0

    # ══════════════════════════════════════════════════════════
    # 唯讀查詢
    # ══════════════════════════════════════════════════════════

    def is_running(self):
        return self._run != 0

    def period_ms(self):
        """本段期長（ms）。"""
        return self._seg[self._i]

    def total_ms(self):
        """一圈總長（ms）。"""
        return self._total

    def seg_index(self):
        """最近一次到期的段索引（0-based）。

        注意：done() 不會推進它 —— 要到下一次 poll() 回報新到期才會改變。
        （實機測試確認過這個語意；log 時搭配 nominal_ms() 才不會誤讀。）
        """
        return self._i

    def remaining_ms(self):
        """距【上一次名目到期點】已經過多久（= 本期已經過多久）。

        _last_fire = 上一次名目到期點；尚未發生任何到期時 = start() 基準。
        所以首次到期之前的值就是「自 start() 起算」的時間。
        ★ 每當 poll() 回報一次新到期，計數就歸 0 重新起算 ——
          所以它天然就是「本期已經過多久」，面板用它做每格 -1 的倒數即可。
        ★ 首次到期之前回 0（不是「距開機時間」，實機測試修正）。
        已停止回 0。
        """
        if not self._run:
            return 0
        d = _ticks_diff(_ticks_ms(), self._last_fire)
        return d if d > 0 else 0

    def until_next_ms(self):
        """距離【下一個】到期點還剩多少 ms（期滿前為正；已越過全部時為 0）。

        週期表的絕對時間推導；協議回報 remain_ms（bus_speed_query）用它。
        """
        if not self._run:
            return 0
        e = _ticks_diff(_ticks_ms(), self._t0)
        total = self._total
        if not self._loop and e >= total:
            return 0
        rel = e % total if self._loop else e
        for k in range(self._n):
            if rel < self._off[k]:
                return self._off[k] - rel
        return 0

    def elapsed_ms(self):
        """自 start() 起算的總經過時間（ms）。"""
        if not self._run:
            return 0
        return _ticks_diff(_ticks_ms(), self._t0)

    def nominal_ms(self):
        """本次到期的【名目】時刻（絕對，從 start 起算）。

        Log 用：實際抓到時刻 - nominal_ms() = 節拍抖動，可用來驗證零漂移。
        """
        return self._nom

    def state(self):
        """診斷用快照 tuple：(running, seg_index, period_ms, elapsed_since_fire, nominal)"""
        return (self._run,
                self._i,
                self._seg[self._i],
                self.remaining_ms(),
                self._nom)


# ══════════════════════════════════════════════════════════════════
# 熱路徑綁定：viper 編譯成功就用它，否則用純 Python 版（見上方說明）
# ══════════════════════════════════════════════════════════════════
# _viper_poll 非 None 就代表 viper 編譯成功（偵測即編譯，不需另行 try-import）
Timer.poll = _viper_poll if _viper_poll is not None else Timer._poll_py
Timer.done = _viper_done if _viper_done is not None else Timer._done_py
