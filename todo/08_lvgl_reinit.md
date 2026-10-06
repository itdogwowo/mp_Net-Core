# LVGL 軟重開機後重新初始化（沿用既有 display）

> **用途**：修掉「軟重開機（Ctrl-D／`mpremote` 預設收尾）之後 UI 起不來」。
> **狀態**：🟡 **根因已量測確認、修法已實作、hard-reset 路徑已驗證；
> 軟重開機的端到端驗證尚未完成**（工具問題，見 §5）
> 👉 **明天從 §4 開始**（板子已經是最新版，只差 push 與「看面板」這一步）
> **最後更新**：2026-10
> **相關**：
> - `doc/03_notes/01_changelog.md` §37 —— 這一輪的完整記錄
> - `doc/02_guides/06_lvgl_ui.md` §8 —— 踩坑清單（「LVGL 只能初始化一次」那條）
> - `todo/07_now_setting_ui.md` —— 同一批 UI 工作

---

## 0. 症狀

```
[ERROR] ❌ [Core 0] Failed to start lvgl:
        memory allocation failed, allocating 3254793227 bytes
```

數字每次都不一樣（實測 3254793227 / 1009759640 / 1634034300）。
**那個數字不是尺寸，是指標。**

觸發條件：**軟重開機**。hard reset（拔電／`machine.reset()`）完全正常。

---

## 1. 根因（逐步量出來的，不是猜的）

先在軟重開機後打斷進 REPL，把 `LvglDisp.__init__` 的每一道手續分開跑：

```
lv.is_initialized()          -> True          ← C 層確實還活著
lv.display_get_default()     -> 有物件
  └ resolution               -> 320 x 240     ← ★ display 其實還是好的
lv.deinit()                  -> 回 OK，但 is_initialized() 仍 True
  └ resolution 變成 (1009033516, 8029)        ← ★ 壞了（0x3C238B2C 是 PSRAM 指標）
lv.display_create(320,240)   -> MemoryError（垃圾尺寸）
```

**結論：**

1. **元兇是 `deinit()`** —— 它沒有真的把狀態清乾淨，反而把一個**還堪用**的
   display 弄成半死。舊版 `lvgl_init.py` 正是這樣寫的：
   ```python
   if lv.is_initialized():
       try: lv.deinit()      # ← 這一行
       except Exception: pass
   lv.init()
   self._disp = lv.display_create(self.W, self.H)   # ← 這一行也錯
   ```
2. 就算 `deinit()` 有效，`display_create()` 也會在**舊的還在**的時候再建一個 —— 那也是錯的。
3. **不能「刪掉舊的再建」**：軟重開機把 MicroPython 的整個 heap 重來，
   LVGL 記的那些指標已經不屬於它了。實測 `disp.delete()`
   → **直接 hard fault 帶走板子**（USB-CDC 消失）。

---

## 2. 修法：**沿用同一個 display**（使用者提的方向）

C 層還在就直接拿回來用，只把 buffers / flush_cb 重新裝上去。
實測可行：

```
resolution = 320 x 240
OK set_color_format(18) / set_buffers(len=25600) / set_flush_cb
OK lv.obj() / label / screen_load / task_handler x20
flush_cb 被呼叫次數 = 6        ← 真的畫出來了
```

`slave/ui/lvgl/lvgl_init.py` 現在的邏輯：

```python
self._reused = False
self._disp = None
if lv.is_initialized():
    self._disp = lv.display_get_default()      # 有就拿來用
if self._disp is not None:
    self._reused = True
else:
    lv.init()                                  # 真的沒有才建
    self._disp = lv.display_create(W, H)

self._disp.set_color_format(18)
self._disp.set_buffers(buf, None, len(buf), 0)
self._disp.set_flush_cb(self._flush_cb)
self._drop_stale()                             # 換掉死掉的舊 screen
```

`_drop_stale()` 刻意**只做最小的一件事**：`lv.screen_load(lv.obj())`。

- **不呼叫 `lv.anim_delete()`** —— 它會走訪殘留的動畫鏈，而那些節點同樣是死指標。
  沒有真機證據說它安全就不放進來（曾經放過，疑似造成 USB 掉線，已移除）。
- **不做任何 free/delete**（見 §1.3）。

> `app.cur` 的初值是 `None`，所以 `app.go("launcher")` 不會 early-return，
> 本來就會 `screen_load` 一個新的 screen。`_drop_stale()` 是墊一次保險
> —— 就算那條路以後改了，LVGL 也不會踩到死 screen。

---

## 3. 已驗證 / 未驗證

### ✅ 已驗證（真機 11401）

| 項目 | 結果 |
|---|---|
| hard reset 走全新 init | `[lvgl_init] 320x240 ... (全新 init)` |
| 7 頁全部建起來 | `[app] build_all: 7 screen(s) pre-built` |
| UI 真的在跑 | `_ui_active=True`、`app.cur='launcher'`、active screen 有 14 個 widget |
| display 解析度 | 320 x 240 |
| 記憶體 | `mem_free = 7.24 MB`（改動前後幾乎相同） |
| 沿用路徑本身 | 手動實測（§2），set_buffers/set_flush_cb/screen_load/task_handler 全 OK、flush_cb 真的被呼叫 |

### ❌ 未驗證

**「軟重開機 → UI 自己回來」這條端到端路徑。**

不是程式的問題，是**工具的問題**：**軟重開機會讓 USB-CDC 重新列舉**，
序列埠控制代碼中途失效 → 開機 log 只抓到一半（停在 `Task Runner Started`），
重開後也查不到狀態（`OSError: Device not configured`）。

> ⚠️ 但要留意：**改動前**的軟重開機是能抓完整 log 的（一路到 `schedule`），
> **改動後**兩次都在中途斷線。這個相關性**還沒有排除** ——
> 有可能是當時版本裡的 `lv.anim_delete()` 撞到死指標（已移除），
> 也可能只是重開機的時序競爭。

---

## 4. 下一步（回來第一件事）

### 現在的狀態（2026-10 收工時）

| | |
|---|---|
| **板上 `lvgl_init.py`** | ✅ **已是最新版**（`8018 B`，`anim_delete` 已移除）|
| **本機 / git** | ✅ 已 commit（`5a3db25`），**尚未 push** |
| **板子** | 上傳後收尾是 `mpremote` 風格的 soft reset，**停在 raw REPL 沒有跑 `main.py`** → 用之前先按一下 RST 或拔插一次電 |

### 明天只需要做這一件事

```bash
# 1) 推上去（我這邊沒有憑證，推不動）
git push origin main          # main 目前 ahead 2

# 2) 確認板子已經在跑（按 RST 或斷電重上之後）
python -B temp/check_reuse_flag.py
```

**然後看面板。** 做一次軟重開機（friendly REPL 按 Ctrl-D），

| 現象 | 意思 | 下一步 |
|---|---|---|
| 畫面有回來 | ✅ 修好了 | 把 §3 的驗證表補完、把 `todo/08` 改成 🟢、changelog §37.4 補上結果 |
| 畫面黑、序列埠還在 | reuse 路徑還有別的問題 | 先確認 `_flush_cb` 有沒有被呼叫（`self._dirty` 有沒有累積）|
| **USB 整個消失** | 撞到死指標 | 直接走 §5 的備案（自我修復式 hard reset），別再追 |

> ⚠️ 如果 USB 消失，記得先確認這是**改動前就有的**還是**改動後才有的** ——
> §3 有記：改動前的軟重開機能抓完整 log，改動後兩次都中途斷線，
> 這條相關性還沒排除。

---

## 5. 備案：自我修復式 hard reset

如果「沿用」在某些情況下仍然不安全，還有一條**保證可靠**的路：

> 開機時偵測到「`lv.is_initialized()` 為 True **但** Python 狀態是全新的
> （＝剛剛軟重開機）」→ 直接 `machine.reset()` 做一次 hard reset。

- 硬重置會把 C 層一起清掉，之後就是正常開機，UI 一定起得來。
- **不會無窮迴圈**：hard reset 之後 `lv.is_initialized()` 是 False，
  走全新 init 那條路，不會再觸發。
- 代價：多花約 7 秒、USB 重新列舉一次。

判斷「是不是剛軟重開機」可以用 `machine.reset_cause()`（軟重開機與硬重置
的 cause 不同），或用「`lv.is_initialized()` 為 True 但 `bus` 上沒有
`lvgl_disp` 服務」這個條件 —— 後者更直接，因為 bus 一定是新的。

---

## 6. 踩到的工具坑（會浪費你半小時的那種）

1. **`\x04` 的意義看你在哪個 REPL**
   - friendly REPL → 軟重開機（會跑 boot.py + main.py）
   - raw REPL → **「執行我剛送進去的程式」**，什麼都不會重開
2. **`machine.soft_reset()` 從 raw REPL 呼叫，回來還是 raw REPL**
   → `main.py` 根本不會跑，你會以為「軟重開機沒事」。
   要真的重開：Ctrl-C 打斷 → `\x02` 回 friendly →**等到看見 `>>>` 提示字元**
   →才送 `\x04`（沒等提示字元就送，整段重開機不會發生，log 一片空白）。
3. **`sys.stdout.flush()` 在 MicroPython 不存在** → `AttributeError`。
4. **軟重開機會讓 USB-CDC 重新列舉** → 控制代碼失效，必須 `reopen`；
   但 reopen 成功不代表連線穩定，`in_waiting` 還是可能踩到 `Device not configured`。
5. **`NowBus.peers` / `peer_count` 是 `@property`**（加 `()` 會 `TypeError`），
   而且 `peers` 回的是 **bytes** 不是 hex 字串。
6. **`font.get_glyph_dsc` 是 unbound 風格**：`f.get_glyph_dsc(f, d, cp, 0)`
   —— 少傳 `f` 會 `TypeError`（見 changelog §36.2，那裡被騙過一次）。
7. **`sys.stdout.flush` / MicroPython 的 `open(w)` 不 close 就 reset**
   → 寫入會整批消失（changelog §35.7a）。

---

## 7. 這一輪新增的工具（`temp/`，依 .gitignore 不進版控）

| 檔案 | 用途 |
|---|---|
| `repro_lvgl_reinit.py` | 重現失敗（軟重開機 → 抓 log） |
| `probe_lvgl_steps.py` | **逐步定位**是哪一行炸的（就是它找出元兇是 deinit） |
| `probe_lvgl_reuse.py` | 證明「沿用既有 display」可行 |
| `check_reuse_flag.py` | 驗證軟重開機後走 `reused=True` 還是 `False` |
| `soft_reboot_test.py` | 可靠地做一次真正的軟重開機 + soak |
