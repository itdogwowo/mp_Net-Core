# LVGL 軟重開機後重新初始化（真根因：C 層 root pointer 跨 soft reboot 存活）

> **用途**：修掉「軟重開機（Ctrl-D／`mpremote` 預設收尾）之後 UI 起不來／板子被打死」。
> **狀態**：🟢 **已修好並真機驗收通過**（2026-10）
> **最後更新**：2026-10
> **相關**：
> - `doc/03_notes/01_changelog.md` §37 —— 這一輪的完整記錄
> - `doc/02_guides/06_lvgl_ui.md` §8 —— 踩坑清單
> - `todo/07_now_setting_ui.md` —— 同一批 UI 工作

---

## 0. 症狀

```
[ERROR] ❌ [Core 0] Failed to start lvgl:
        memory allocation failed, allocating 3254793227 bytes
```

數字每次都不一樣（實測 3254793227 / 1009759640 / 1634034300）。
**那個數字不是尺寸，是指標。**

觸發條件：**軟重開機**。hard reset（RST／斷電／`machine.reset()`）完全正常。

---

## 1. 真根因（C 源碼 + ELF + 真機實測，三路交叉確認）

### 1.1 三行程式碼就能說完

| 位置 | 內容 | 軟重開機後 |
|---|---|---|
| `ext_mod/lvgl/mem_core.c` | `lv_malloc_core()` → `gc_alloc()` | **GC heap 被 `mp_init()` 清空** |
| `gen/lvgl_api_gen_mpy.py` | `void *mp_lv_roots;`（普通 C 全域） | **指到的 `lv_global_t` 在舊 heap 上 → 死指標** |
| 同上 | `static bool mp_lv_roots_initialized = false;`（function-local static） | **仍是 `true` → 永遠不會重建 `lv_global`** |

ELF 符號（`build-ESP32_GENERIC_S3-SPIRAM_OCT/micropython.elf`）：

```
3fcaceec B mp_lv_roots                       ← .bss（軟重開機不清）
3fcacef4 b mp_lv_roots_initialized$7         ← .bss（軟重開機不清）
420403f8 T mp_lv_init_gc
```

**⇒ LVGL 的每一筆配置（display、screen、所有 widget、timer、anim、style…）
都在 MicroPython 的 GC heap 上；軟重開機把那個 heap 整個重來，但 C 層的
root pointer 與「已初始化」旗標都活著 → LVGL 整棵樹變成死指標。**

### 1.2 真機實測（軟重開機後攔在 REPL、`main.py` 未執行）

| 動作 | 結果 |
|---|---|
| `display_get_default()` | **有物件** ← ★ **殘留的判準** |
| └ 解析度 | `320 x 240` ← 還讀得到（舊 heap 還沒被覆寫） |
| `screen_active().get_child_count()` | **14** ← 上一個 session 的 UI 樹 |
| `lv.display_create(320, 240)` | ✅ OK |
| `set_color_format` / `set_buffers` / `task_handler` | ✅ OK |
| **`lv.obj()`** | ❌ **直接打死板子**（USB-CDC 消失，需 RST） |
| **`lv.label(parent)`** | ❌ 打死板子 |
| **`lv.screen_load(x)`** | ❌ 打死板子 |
| **`lv.deinit()`** | ❌ 打死板子 |

> ⚠️ **`lv.is_initialized()` 不能當判準**（這一輪踩過）：
> 這個 binding 在 **import `lvgl` 的當下就會做掉 C 層初始化**，所以**乾淨開機時
> 它也可能是 `True`**。曾經拿它當「有沒有殘留」的判準 → 把正常開機誤判成殘留、
> 自己的守門把 UI 擋掉。**要看的是 `display_get_default()`。**
>
> ⚠️ 殘留狀態的組合**不穩定**：實測看過
> `is_init=True + display=非None`（最典型）、`is_init=False + display=非None`、
> 甚至 `is_init=True + display=None`。軟重開機後 heap 上接下來的活動會決定它，
> 所以**不要用旗標推論**，要嘛探測、要嘛用外部狀態（標記檔）。

「打死」＝ 板子自己硬重置，`/dev/cu.usbmodem11401` 消失 10 秒後回來，
且因為 root pointer 沒被清，**它仍然帶著殘留狀態**（除非那次重置剛好清掉）。

### 1.3 為什麼第 1 輪軟重開機的 log 會在 `Boot layer 2` 斷掉

軟重開機後的第一次開機**一定會死在 `LvglTask` 啟動 LVGL 的那一刻**：

```
[  28.17] [Config] ✓ 無損更新成功: Router
[  30.94] *** SERIAL BREAK ***        ← 這裡就是 LvglTask → board._setup() → LVGL
[  41.78] *** port 回來（等了 10.84s）***
...（板子自己重開，第二次開機）
[  60.41] [lvgl_init] 320x240 ... (全新 init)
[  64.02] [board] _setup done
[  64.31] [INFO] ⚙ [TM] Boot complete → running
```

**所以 UI 最後看起來是好的，是因為板子先炸了一次、硬重置、才走全新 init。**
第 1 輪的 log 之所以「斷線」，不是工具問題，是板子真的掛了。

---

## 2. 為什麼「沿用同一個 Object」不可行（使用者提的方向，已證偽）

`todo/08` 舊版假設「C 層還活著 → `display_get_default()` 拿回來用就好」。
實測後這個假設有三層錯：

1. `display_get_default()` 回的是一個**還讀得到解析度的死指標**。能讀不等於能用。
2. 能沿用的不只是一個 display，是**整棵死指標樹**（`act_scr` + 14 個 widget +
   timer/anim 鏈）。只要有任何一個動作走到樹上就炸。
3. `lv.obj()` 這種「最平凡」的動作就是第一個炸的 ——
   它內部要配新的物件，而 heap 已經不是 LVGL 的了。

### 試過的復原配方（全部真機實測）

| 配方 | 結果 |
|---|---|
| `lv.deinit()` → `lv.init()` → `display_create()` | ❌ `deinit()` 自己就打死板子 |
| 沿用既有 display，只重裝 buffers/flush_cb | ❌ 撐過設定，畫面/`obj()` 一動就死 |
| `lv.mp_lv_deinit_gc()` → `lv.mp_lv_init_gc()`（兩者確實存在且可呼叫） | ⚠️ **狀態確實清乾淨了**（`display`→`None`、`act_scr`→`None`），但之後 `display_create()`/`lv.obj()` 還是死 |
| 開新 `lv.display_create()` 給新 display 用 | ❌ 舊的還在 refresh 鏈上 |

> 那為什麼 `mp_lv_deinit_gc + init_gc` 之後還是不行？
> 因為該 binding **沒有把真正的 `lv_init()` 匯出到 Python**
> （本 build 的 `lv.init` 被映射成 `lv_anim_del_all`，等於 no-op；
> 真正的 LVGL 初始化在 import `lvgl` 時由 C 的 module init 做掉）。
> `mp_lv_init_gc()` 只重建「Python 側的 root pointer 容器」，不會重跑 LVGL 的
> timers / draw / anim / fs 子系統初始化 —— 所以那個 `lv_global` 是空殼。

**⇒ 只要是在這個韌體上，軟重開機後就沒有任何純 Python 的路能安全重用 LVGL。**

---

## 3. 修法 A（已實作、已驗收）：boot.py Phase 0 自我修復守門

> `slave/boot.py` 最前面 + `slave/ui/lvgl/lvgl_init.py` + `slave/ui/lvgl/board.py`

**思路**：既然軟重開機不可能救，那就**在它造成傷害之前把它變回一次乾淨開機**。

```
Phase 0（boot.py 第一件事）
  1) 讀 /lvgl_state
       reset=1   → 上一輪已經為這件事重置過一次
                   → 清標記、往下走（★ 這步必須在寫標記「之前」）
  2) 寫下本輪標記 owned=1,reset=0
  3) 探測 lvgl.display_get_default()
       不是 None → 殘留 → 標記改 reset=1 → machine.reset()（硬重置）
       None      → 乾淨 → 往下走
  4) UI 起來之後（board._setup 完成）→ 清掉標記
```

**為什麼用「探測」而不是靠「上一輪有沒有正常收尾」**：
軟重開機的觸發時機不可控（Ctrl-C、`mpremote`、watchdog 都可能），
靠旗標會漏；直接問 LVGL「你手上還有沒有 display」最準。

**為什麼不會無窮重置**：`reset=1` 是點數。硬重置後重入會消耗掉它；
降到 0 之後若又偵測到殘留，會再重置一次（重新累積 1 點）
→ 最壞情況是「每兩次開機重置一次」，不會卡死、也不會無限快速重置。

**代價**：每次軟重開機多一次約 8 秒的硬重置 + USB 重新列舉。

### 3.1 驗收結果（真機 11401，2026-10）

| 情境 | 期望 | 實測 |
|---|---|---|
| 軟重開機（有殘留） | 守門攔下 → 硬重置 → UI 回來 | ✅ `[BOOT] LVGL guard: 偵測到 soft-reboot 殘留 → hard reset` → `(全新 init)` → `_setup done` → `Boot complete` |
| 軟重開機（乾淨） | 不重置，直接開機 | ✅ 28s 完成，USB 沒斷 |
| 標記被強制設成 `reset=1` | 清標記往下走，不再重置 | ✅ `重置後重入，清標記繼續` → 直接開機成功 |
| 開機後畫面 | 320x240、launcher 頁、14 個 widget | ✅ `lvgl_disp` 在、`app.cur='launcher'`、`_ui_active=True` |

---

## 4. 修法 B（根治，需重編韌體）：讓 `lv_global` 跟 heap 一起重來

一行 C 的改動 —— 把「已初始化」旗標從 **function-local static** 換成
**VM state**（soft reset 會清）：

`gen/lvgl_api_gen_mpy.py`（`ext_mod/lvgl/micropython.cmake` 用
`GEN_SCRIPT=lvgl` 生成 `lv_mp.c`）：

```c
// Register LVGL root pointers
MP_REGISTER_ROOT_POINTER(void *mp_lv_roots);
MP_REGISTER_ROOT_POINTER(void *mp_lv_user_data);
MP_REGISTER_ROOT_POINTER(int mp_lv_roots_initialized);   // ← 新增

void *mp_lv_roots;

void mp_lv_init_gc()
{
    // ★ 原本是 function-local static → soft reset 不會清（.bss 常駐）
    if (!MP_STATE_VM(mp_lv_roots_initialized)) {
        mp_lv_roots = MP_STATE_VM(mp_lv_roots) = m_new0(lv_global_t, 1);
        MP_STATE_VM(mp_lv_roots_initialized) = 1;
    }
}
```

改了之後：軟重開機 → `MP_STATE_VM` 歸零 → `mp_lv_init_gc()` 在新 heap 上
重建乾淨的 `lv_global` → 軟重開機不再需要硬重置，也不必多花 7 秒。

> ⚠️ 還需要確認同一份 generate 出來的 module init 在 soft reset 時有被重跑
> （把 `MP_STATE_VM(lvgl_mod_initialized)` 也一起歸零），否則 LVGL 的
> timers/draw 子系統仍然只初始化過一次。

**修法 B 是正解，但需要重編 + 重燒韌體**（`lvgl_micropython`，
`build-esp32` / `make.py`）。在做之前，修法 A 是可靠且已落地的替代。

---

## 5. 驗證狀態（2026-10）

### ✅ 已確認（真機 + 源碼）

| 項目 | 結果 |
|---|---|
| 根因三要素（heap / root pointer / static 旗標） | ✅ ELF 符號 + C 源碼確認 |
| 軟重開機後 `lv.obj()`/`screen_load()`/`deinit()` 打死板子 | ✅ 多次重現 |
| 軟重開機後第一次開機必死在 `Boot layer 2` | ✅ 完整時序 log |
| 崩潰後板子自己硬重置，第二次開機 UI 正常 | ✅ `(全新 init)` → `_setup done` → `Boot complete` |
| 「沿用既有 display」不可行 | ✅ 已證偽（含 4 種復原配方） |
| `mp_lv_deinit_gc` / `mp_lv_init_gc` 可呼叫、能清 Python 側狀態 | ✅ 但之後 `display_create()` 仍失敗（C 層無 `lv_init` 可呼叫） |
| **修法 A 上板：軟重開機 → 守門 → 硬重置 → UI 回來** | ✅ **通過**（見 §3.1） |
| **修法 A：乾淨軟重開機不誤判、不多重置** | ✅ 通過 |
| **修法 A：`reset=1` 防無窮重置** | ✅ 通過 |

### ⏳ 還沒做

| 項目 | 怎麼驗 |
|---|---|
| `mpremote` 上傳收尾（真 soft reset）→ 守門接手 | 用 `mpremote` 上傳任一檔後看面板 |
| 修法 B（改 C 重編韌體） | 見 §4；做完就不必再硬重置 |
| 把 `temp/` 的工具正式收進 `tools/`（目前依 .gitignore 不進版控） | 需要時再搬 |

---

## 6. 這一輪的工具（`temp/`，依 .gitignore 不進版控）

| 檔案 | 用途 |
|---|---|
| `board_put.py` | **raw-REPL 最小上傳器**（不對板子做 soft reset 收尾） |
| `verify_guard.py` | **核心驗收**：軟重開機 → 守門 → 硬重置 → UI 回來（含自動重連 + 逐項 PASS/FAIL） |
| `verify_cold.py` | 反向驗收：不該被守門誤判 |
| `repro_lvgl_reinit.py` | 軟重開機 → 抓完整 log（**會處理 USB-CDC 重新列舉與重連**）+ 狀態探針 |
| `watch_reboot.py` | 純監看一次軟重開機的逐行時序（含斷線/回來時間） |
| `reuse_ab.py` | 軟重開機後攔在 REPL，逐步量殘留狀態 |
| `bisect_lvgl.py` | **二分定位**哪一個 LVGL 動作會死（就是它抓出 `lv.obj()`） |
| `recover_lvgl.py` | 試各種「沿用/復原」配方 |
| `gc_heal_test.py` / `gc_heal_test2.py` | 測 `mp_lv_deinit_gc` + `mp_lv_init_gc` 能不能救 |
| `measure_clean_boot.py` / `reset_state_probe.py` | 軟重開機 vs 硬重置的狀態對照 |
| `board_probe.py` | 單發狀態探針（不重開機），可帶自訂程式碼 |

> ⚠️ 這些工具會把板子打死。打死後**必須按 RST 或拔插 USB**：
> 此時 `/dev/cu.usbmodem11401` 還在，但 `open()` 會直接卡住（macOS CDC 典型症狀），
> 要等 USB 重新列舉後才會恢復。

---

## 7. 踩到的工具坑

1. **板子被打死後 `open()` 會卡住而不是報錯** —— 必須按 RST／拔插，別等它自己好。
2. **`\x04` 的意義看你在哪個 REPL**：friendly → 軟重開機；raw → 「執行我剛送進去的程式」。
3. **friendly REPL 的 `>>>` 不一定會印在新的一行**：`print(...)` 之後接 `>>> ` 可能黏在
   同一行，判斷式要用 `rstrip().endswith(b">>>")`。
4. **paste mode（Ctrl-E）最後要多等一個提示字元**才代表跑完；
   只等 `PROBE_DONE` 會抓到半截輸出。**能用 raw REPL（Ctrl-A）就用 raw REPL** ——
   回應格式固定是 `OK<stdout>\x04<stderr>\x04>`，比 paste mode 好解析太多。
5. **`f.write()` 之後 `os.stat()` 在同一個 raw-REPL session 會回 0** ——
   資料在 `f.close()` 才落盤。驗證檔案大小要另開一個 session 量。
6. **raw REPL 一行不要拉太長**：`repr(bytes)` 會膨脹約 2.4 倍，
   512 bytes/chunk 會卡住；64 bytes/chunk 穩定。
7. **`lv.init` 在這些 binding 上不是 `lv_init`**（是 `lv_anim_del_all`）——
   不要用它來「重新初始化 LVGL」。
8. **`NowBus.peers` / `peer_count` 是 `@property`**（加 `()` 會 `TypeError`）。
9. **`font.get_glyph_dsc` 是 unbound 風格**：`f.get_glyph_dsc(f, d, cp, 0)`。
10. **寫守門邏輯時，讀標記一定要在寫標記之前** —— 順序顛倒會把自己的訊號蓋掉
    （這一輪真的踩到，`reset=1` 被自己的 `owned=1,reset=0` 蓋掉）。
