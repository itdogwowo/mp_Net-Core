# Timer — 絕對時間週期計時器（`lib/sys/timer.py`）

> **用途**：全系統共用的週期／超時計時器，取代散落各 task 的手寫 `ticks_ms` + `ticks_add` + `ticks_diff`。
> **分類**：使用教學（02_guides）
> **最後更新**：2026-09-20
> **位置**：`slave/lib/sys/timer.py`
> **測試**：`test/timer/test_timer.py`（27 項，純邏輯不需硬體）、`test/timer/test_bus_speed_migration.py`（15 項，遷移行為驗證）

---

## 1. 一句話

**設定期長陣列 → 每輪 `poll()` 問夠鐘沒 → 夠鐘做事 → `done()` 回報。**
時間基準在 `start()` 時固定，之後**永不修改**，所以 `done()` 什麼時候回報都不會讓節拍漂移。

---

## 2. 為什麼需要它（而不是各處手寫）

全樹原本有 **121 處** `ticks_*` 呼叫、**39 個** `_deadline` / `_start_ms` / `_started_at` / `_t0` 這類手寫計時變數。
手寫會踩到四個坑，集中在一個 class 裡只需要修一次：

| # | 坑 | 後果 |
|---|---|---|
| 1 | 用「起點 + 已觸發旗標」做週期 | 只觸發一次，不會續期 |
| 2 | 用 `start == 0` 當「未啟動」哨兵 | 起點剛好是 0 時被誤判未啟動 |
| 3 | 下一段從 `done()` 時刻起算 | **節拍累積漂移**（越跑越慢） |
| 4 | 去重比較「週期內段數」 | 每圈歸零 → 只觸發一次 |

**成本**：真機實測（ESP32-C3 @160MHz, MicroPython v1.28.0，見 §8）。

`done()` 回傳**單一整數**（不建 tuple）→ 熱路徑零配置、不觸發 GC。

---

## 3.5 真機效能基準（實測，非估算）

環境：ESP32-C3 @160MHz（MicroPython v1.28.0 `ESP32_GENERIC_C3`）、
ESP32-S3 @160MHz（MicroPython v1.29.0-preview `ESP32_GENERIC_S3`）。

### 基礎成本

| 項目 | ESP32-C3 | ESP32-S3 | 備註 |
|---|---:|---:|---|
| 空函式呼叫（基線） | **6.49 µs** | 10.72 µs | MicroPython 呼叫成本 ≈ CPython 的 200 倍 |
| `time.ticks_ms()` | 8.66 µs | 5.33 µs | |
| 模組層綁定 `_ticks_ms()` | **7.15 µs** | 4.05 µs* | 省 1.25–1.51 µs |
| `Timer.poll()`（viper 前） | 48–53 µs | 48–55 µs | 含跨圈邊界判定（準確性所需） |
| **`Timer.poll()`（viper 後）** | **35.0 µs** | **32.3 µs** | C3 加速 1.4–1.5x、S3 1.5–1.7x |
| `Timer.done()`（viper 前） | 10.7 µs | 15.4 µs | ≈ 基線 → 「單一整數」設計有效 |
| `Timer.period_ms()` | 10.1 µs | 14.2 µs | |
| `Timer.elapsed_ms()` | 15.2 µs | 19.1 µs | |
| `Timer.until_next_ms()` | 30.1 µs | 33.7 µs | |
| `Timer.start()` | 175 µs | 172 µs | 建 tuple；非熱路徑 |
| `Timer()` 實例 | 112–160 B | 112 B | `__slots__` 固定欄位 |
| 10000 次 `poll()` / `done()` | **0 B** | **0 B** | 無記憶體累積 |

> \* S3 的綁定節省以「屬性查詢 5.33 µs − 綁定呼叫」推算，兩板結論一致。

### 已採用的加速：模組層函式綁定（交錯量測，5 輪取中位數）

| sched | 對照（`time.ticks_ms()`） | 綁定後 | 差異 |
|---|---:|---:|---:|
| `[1000]` | 45.66 µs | 42.21 µs | **−3.45 µs（−7.5%）** |
| `[2000, 3000]` | 45.81 µs | 42.96 µs | **−2.84 µs（−6.2%）** |
| `[500,1000,2000,3000]` | 49.41 µs | 45.68 µs | **−3.72 µs（−7.5%）** |

行為完全一致（名目序列相同）。**每顆 timer 每次 poll 省約 3 µs**。

> ⚖️ **準確性優先的代價**：跨圈邊界判定（見 §3.6）讓 poll() 再增加約 6 µs
> （42→48.6 µs）。這是換取「延遲 poll 不誤判、不漂移」的必要成本，
> 依既定原則（準確性 > 效能）保留。12 顆 @50fps 總計約 0.6 ms/幀 ≈ 3% 幀預算。

### 3.6 準確性設計（優先於效能）

兩條鐵律，都有真機回歸測試固定：

**① 逾時判定的基準必須是「名目到期時刻」，不是「偵測時刻」**

`done()` 用 `_last_fire`（由絕對時間表推導的名目點），不用 `_fire_at`（poll 實際回報時刻）。
若主迴圈忙碌導致 poll() 晚了 K ms 才回報，用偵測時刻當基準會讓 **K ms 憑空消失**，
把逾時誤判為準時。真機實測：期長 200ms、工作 50ms、偵測延遲 300ms → 舊寫法回 0，
實際已逾時 150ms。

**② `seg < 0` 不等於「沒到期」（跨圈漏報）**

`seg` 是「**本週期內**剛結束的段索引」。當時間跨過週期邊界、新週期內尚無段結束時
`seg = -1`，但**上一週期的最後一段其實已經到期**。
錯誤寫法 `if seg < 0: return 0` 會造成漏報（真機實測：期長 1000ms、第一次 poll 在
1300ms → 回 0，且名目被算成 2000）。

正解：已完成段數用 `nth = cycle * n`（`seg < 0` 時）或 `cycle * n + (seg+1)`，
且 `cycle` 必須退一格與 `seg` 落在**同一週期**，否則名目會多算一整圈、
`_last_fire` 跟著錯、逾時判定也跟著錯。

> 這兩個 bug 都是「真機 + 延遲 poll 情境」才暴露出來的；CPython 虛擬時鐘測試
> 若沒有刻意構造「第一次 poll 就晚於名目時刻」的情境，一樣測不出來。

### 加速技巧總表（實測）

| 技巧 | 狀態 | 實測 |
|---|---|---|
| **`@micropython.viper` 編譯熱路徑** | ✅ **已採用** | **C3：48–53 → 35.0 µs（1.4–1.5x）**；**S3：48–55 → 32.3 µs（1.5–1.7x）**；隔離測試（純算術）2.9–3.2x。兩板名目序列皆與純 Python 版完全相同 |
| 模組層函式綁定 | ✅ 已採用 | 省 1.25–1.51 µs/call（-17%） |
| 減少呼叫鏈深度 | ✅ 原則 | 每多一層 wrapper 約 **+11 µs**（S3），比任何微優化都關鍵 |
| `@micropython.native` | ➖ 未用 | 隔離測試 2.34x（viper 3.11x），效果不如 viper，且同樣需 exec 編譯 |
| `micropython.const` | ❌ 無益 | C3/S3 皆 18.32 µs，與全域常數同級 |
| 區域變數常數 | ❌ 無益 | 18.64 µs，與全域同級 |
| `micropython.opt_level(3)` | ❌ 無效 | 只影響 assert/`__debug__`。C3 19.61→19.61；S3 26.12→26.12 |

### ⚠️ 探測陷阱：`hasattr` 會騙你（本庫開發時真的踩到）

`micropython` 模組**沒有任何名為 `viper` / `native` 的屬性**：

```python
hasattr(micropython, "viper")     # False
dir(micropython)                  # 不含 viper
micropython.viper                 # AttributeError
from micropython import viper     # ImportError
```

**但 `@micropython.viper` 語法本身完全可用** —— 它是**編譯器認得的名字**，不是執行期屬性。

因此用 `hasattr` / `try-import` 判斷支援性會**誤判為不支援，白白放棄 1.6~3 倍加速**。
本庫第一版就是這樣寫的（`try: from micropython import native`），導致加速完全沒生效。

**正解**：把熱路徑寫成**原始碼字串**，用 `exec()` 讓編譯器看到裝飾器，再把產生的函式綁進類別：

```python
_VIPER_SRC = """
@micropython.viper
def poll(self) -> int:
    ...
"""
try:
    _ns = {"_ticks_ms": _ticks_ms, "_ticks_diff": _ticks_diff}
    exec(_VIPER_SRC, _ns)
    _viper_poll = _ns["poll"]
except Exception:
    _viper_poll = None        # 不支援的埠 → 退回純 Python 版

Timer.poll = _viper_poll if _viper_poll is not None else Timer._poll_py
```

### viper 的兩個硬限制（原始碼內已處理）

1. **呼叫一般 Python 函式的回傳值視為 `object`**：`_ticks_ms()`、`_ticks_diff()` 的結果
   都必須 `int()` 轉型，否則 `ViperTypeError: can't do binary op between 'object' and 'int'`
2. **tuple 下標也是 `object`**：`self._off[k]` 要寫成 `int(off[k])`；
   `self` 屬性參與運算時同樣要轉型（例：`int(self._t0) + nom`）

### 安全設計

`exec` 包在 `try/except` 內：不支援 viper 的埠、或用 CPython 跑離線測試時，
自動退回純 Python 版 —— **絕不因加速而讓模組載入失敗**。
CPython 端 `Timer.poll is Timer._poll_py`（31 項單元測試即在此路徑驗證）。

### 預算換算

12 顆 Timer 每輪 poll，在 50fps（20ms/幀）下 ≈ **0.51 ms/幀 ≈ 2.5% 幀預算**。

> ⚠️ 結論：MicroPython 上真正貴的是「**呼叫次數**」，不是資料結構或數學。
> 每輪只 poll 必要的那幾顆，不要放進逐幀的內圈迴圈。

> 📌 **量測方法教訓**：把兩個變體**分開連續量**會得到假結果（本庫開發時曾因此誤報
> 「1.42x 加速」——實際是兩次量測之間的系統狀態差）。正確做法是**交錯量測**：
> A→B→A→B… 多輪取中位數，才隔離得出真實差異。

---

## 3. API

```python
from lib.sys.timer import Timer

t = Timer()
t.start([2000, 3000], loop=True)   # 期長陣列（ms）；loop=False 跑完自動停
t.start(1000)                      # 單一值也接受；非法值自動夾成 1
```

| 方法 | 回傳 | 說明 |
|---|---|---|
| `poll()` | `0` / `1` | `1` = 夠鐘。同一段內重複呼叫只回一次 1（自動去重） |
| `done()` | `0` / `1` | 回報完成。`1` = **超時**（本次用掉時間 ≥ 本段期長）。★ 不影響時間表 |
| `stop()` | — | 停止（可再 `start()` 重新起算） |
| `is_running()` | bool | 是否啟動中 |
| `period_ms()` | int | 本段期長 |
| `total_ms()` | int | 一圈總長 |
| `seg_index()` | int | 目前在第幾段（0-based） |
| `elapsed_ms()` | int | 自 `start()` 起算的總經過時間 |
| `remaining_ms()` | int | 距基準／上一次名目到期點經過多久（每次新到期歸 0 重新起算） |
| `until_next_ms()` | int | 距**下一個**到期點還剩多少（協議 `remain_ms` 用） |
| `nominal_ms()` | int | 本次到期的**名目**時刻（log 用：實際 − 名目 = 抖動） |
| `state()` | tuple | 診斷快照 `(running, seg_index, period_ms, remaining_ms, nominal)` |

### 典型用法

```python
# ── 週期工作（例如每秒廣播狀態）──
self._t = Timer()
self._t.start([1000], loop=True)

def loop(self):
    if self._t.poll():
        broadcast_status()
        if self._t.done():                 # 回報完成；1 = 這次超時
            log("[WARN] 廣播超時，吃掉一個週期")

# ── 超時回滾（bus_speed 的實際用法）──
self._t_sync.start(timeout_ms, loop=False)
if self._t_sync.poll():
    revert()
```

### 應用層倒數顯示（本類別【不】代管 UI 狀態）

倒數的「-1」由應用層自己減，`Timer` 只提供時間來源：

```python
# 每格 -1 的顯示（面板用）
if self._t.poll():
    do_work()
    self._t.done()
lbl.set_text(str(self._t.remaining_ms() // 1000))   # 本期已過幾秒
```

---

## 3.7 該用庫還是手寫？（三方實測對比）

常被問：「我自己在手寫 `ticks_ms` + `ticks_diff` 也可以，為什麼要用庫？」
以下是 ESP32-C3 實測（交錯量測，5 輪中位數），**手寫版也套用 viper**，條件對等：

| 實作 | `poll()` | 記憶體/顆 |
|---|---:|---:|
| 手寫 + viper | **20.7 µs** | 54.7 B |
| 手寫（名目累加，零漂移） | 21.3 µs | 54.7 B |
| 手寫（直覺版，期滿重設） | 21.6 µs | 54.7 B |
| **Timer 庫（viper）** | **31.2 µs** | 64.7 B |

> **viper 不是庫的專屬優勢**：手寫版一樣能用（同樣 `exec` 編譯原始碼字串即可）。
> 庫的 +10.5 µs（+45%）不是用來買速度的。

### 那多出來的 10.5 µs 買到什麼（實測）

| 情境 | 手寫 + viper | Timer 庫 |
|---|---|---|
| **多段排程** | ❌ 只有單段；換段要自己寫 `if seg==0: period=3000; seg=1` 並處理交界 | ✅ `start([2000,3000])` 一行 |
| **晚 300ms 才 poll** | ❌ 遲到量憑空消失（名目被算成偵測時刻） | ✅ 名目仍是理論值，不漂移 |
| **逾時偵測** | ❌ 無（要自己補，且基準容易寫錯） | ✅ `done()` 回 0/1 |
| **落後可觀測性** | 🟡 只有累計落後（150→301→451→601ms） | ✅ 逐名目點（名目200 落後50、名目500 落後1…） |
| 去重 / 零漂移 | ✅ 有 | ✅ 有 |
| 觸發次數 | 相同 | 相同 |

### 判準

| 情境 | 建議 |
|---|---|
| 單段週期、不需要逾時語意 | **手寫 + viper**（最快最省） |
| 需要「工作是否超時」 | 用庫（自己補容易寫錯基準——本庫開發時就真的寫錯過） |
| 需要「哪個名目點遲到」 | 用庫 |
| 多段不同期長 | 用庫 |
| 極致熱路徑（逐幀內圈） | **兩者都別用**——改用幀計數（如 `RenderTask` 節拍） |

### 規模換算（12 顆 @50fps）

| | 每秒 CPU | 佔比 |
|---|---:|---:|
| 手寫 + viper | 12.4 ms | 1.2% |
| Timer 庫 | 18.7 ms | 1.9% |

差距 0.6% CPU。**在這個規模下效能不是選擇依據，正確性才是** ——
本庫開發過程中抓到兩個手寫版會靜默吃掉的 bug（晚 poll 漏報、逾時低估），
那 10.5 µs 買的是「不會無聲出錯」。

### 想再快就往下鑽的選項（評估後未採用）

熱路徑已經交給 viper 編譯器（機器碼），再往下只有：
- **C 模組**（marshalling 成本，且要維護 .mpy 與 build）
- **`asm_esp32`/`asm_xtensa` 手寫組譯**（兩板皆無此功能）
- **完全不呼叫**：把計時改成幀計數

以 12 顆 @50fps 差 0.6% CPU 的規模，這些都不值得。

## 4. 邊界語意（三個魔鬼細節，已經被測試固定）

| # | 邊界 | 定案 | 測試 |
|---|---|---|---|
| 1 | **段邊界** `e == off[k]` | 屬於「第 k 段剛到期」（`>=` 含等號） | `test_segment_boundary_inclusive` |
| 2 | **週期交界** `e == total`（`rel == 0`） | 歸屬**上一圈最後一段**，不是下一圈第 1 段 | `test_cycle_boundary_belongs_to_previous_cycle` |
| 3 | **不循環收尾** `e == total` | 先讓最後一段觸發，下一輪 `poll()` 才關機（否則每次吃掉最後一段） | `test_nonloop_fires_final_segment` |

去重必須用「**從基準算起的總段數**」——用「週期內段數」會因每圈歸零而只觸發一次（`test_dedup_uses_total_count_not_per_cycle`）。

---

## 5. 適用範圍（重要）

| 用途 | 適合 | 理由 |
|---|---|---|
| 超時回滾（`bus_speed` SYNCING / idle） | ✅ | 本質就是「多久之後要做事」 |
| 週期狀態廣播、心跳 | ✅ | 陣列 `[1000]`、`loop=True` |
| 看門狗 re-arm 倒數 | ✅ | 一次性倒數 |
| 面板／UI 倒數顯示 | 🟡 | 用 `remaining_ms()` 換算，倒數由 UI 自己減 |
| **產幀／效果長度**（`end_Time`、`maxF`、`cycles`） | ❌ | 那些是**幀數**不是時間；碰時間會破壞決定性 |
| **渲染節拍**（`RenderTask.next_tick_us`） | ❌ | 微秒級熱路徑，已有 wrap 安全與起播對齊邏輯，包一層只會變慢變模糊 |

> 判準：**「多久之後／多久沒有」→ 用 Timer；「第幾格」→ 不要用 Timer。**

---

## 6. 跨核心注意

**`Timer` 實例不可跨核心共用**（Python 物件跨核 = race）。
每個 task 自己持有自己的實例；需要給別的核或遠端看時，只把**算出來的毫秒數字**推進 `bus.shared`（同 `hw_manager` 的 snapshot 模式）。

---

## 7. 已遷移案例：`bus_speed`（臨時提速超時回滾）

原本兩個獨立 deadline 欄位，改用兩顆 Timer：

| 原本 | 現在 |
|---|---|
| `s["timeout_at"] = ticks_add(ticks_ms(), timeout_ms)` | `_t_sync.start(timeout_ms, loop=False)` |
| `s["idle_timeout_at"] = ticks_add(...)` | `_t_idle.start(idle, loop=False)` |
| `bus_speed_poll()` 手算 `ticks_diff(now, timeout_at) >= 0` | `if _t_sync.poll(): _revert()` |
| `bus_speed_touch()` 重寫 `idle_timeout_at` | `_t_idle.done(); _t_idle.start(idle, loop=False)` |
| `bus_speed_query()` 手算 remain | `_t_sync.until_next_ms()` |
| `bus_speed_commit()` 手動清 `timeout_at` | `_t_sync.stop()`（交棒 `_t_idle`） |

行為驗證見 `test/timer/test_bus_speed_migration.py`（15 項）：SYNCING 超時回滾、COMMIT 後舊 deadline 不得再觸發、持續 touch 不誤回滾、停止通訊後 idle 超時、`timeout_ms=0` 不誤觸發。

---

## 相關文件

- `01_protocol/09_bus_speed_protocol.md` — 臨時提速完整工作流程（本庫第一個使用場景）
- `04_pixel_protocol.md` — 模式時長 `elapsed_ms` / `total_ms`（u32 ms；注意模式本身是幀驅動）
- `11_developing_effects.md` — 效果以**幀**為單位（`end_Time`、`maxF`），不要用 Timer

---

## 8. 驗證狀態

| 測試 | 項數 | 環境 | 結果 |
|---|---:|---|---|
| `test/timer/test_timer.py` | 27 | CPython + 虛擬時鐘 | 全 PASS |
| `test/timer/test_bus_speed_migration.py` | 15 | CPython + 虛擬時鐘 + 假 UART | 全 PASS |
| `test/timer/test_timer_device.py` | **40** | **ESP32-C3 + ESP32-S3 真機** | 兩板皆全 PASS |

> `test/` 在 `.gitignore` 內（本機測試工具，不進版控）。

### 加速技巧研究（實機驗證）

| 技巧 | 狀態 |
|---|---|
| 模組層函式綁定 | ✅ 已採用，poll() −6~7.5% |
| `@micropython.native` / `viper` | ⚠️ C3 建置無此模組；庫內 getattr 取用，未來埠自動生效 |
| `micropython.opt_level(3)` | ❌ 無效（只影響 assert/`__debug__`） |
| **交錯量測法** | 📌 分開連續量測會誤報 1.42x；必須 A→B→A→B 多輪取中位數 |
| **呼叫鏈深度** | ✅ 每多一層 wrapper 約 +11 µs（S3 實測），比任何微優化都關鍵 |

### 真機驗證發現（CPython 測不到的）

1. **`remaining_ms()` 語意缺陷**：原本用名目時刻當基準，導致「首次到期前」會一路遞增成「距開機時間」。
   修正：獨立 `_last_fire` 欄位（語意：距上次名目到期點；無到期時 = `start()` 基準）。
2. **`seg_index()` 語意確認**：`done()` **不會**推進段索引 —— 要到下一次 `poll()` 回報新到期才改變。
   已在 docstring 註明，避免 log 誤讀（搭配 `nominal_ms()` 一起看）。
3. **延遲 poll 的跳號行為**：監控通道抖動造成某輪 `poll()` 晚 100ms 回來時，
   名目序列出現 `[50,100,200,300,...]`（跳過 150）—— 這是設計行為（**只回報最新到期點、不補發**），
   不是 bug。真機驗證因此改用結構性不變式（名目必為期長倍數、嚴格遞增），不用牆鐘讀數。
