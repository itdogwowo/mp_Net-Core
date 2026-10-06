# Soft reboot 的資源殘留：哪些會漏、哪些會自己回來

> **用途**：軟重開機（friendly REPL 的 Ctrl-D／`mpremote` 預設收尾）之後，
> 哪些資源會變成孤兒、哪些其實會自己回來。動手「加一個 soft-reboot cleanup」
> 之前先看這份，免得像我一樣先做了兩個錯誤的結論。
> **狀態**：🟢 機制已量測清楚；有一個 **~10 KB／輪的殘差來源未定**
> **最後更新**：2026-10
> **相關**：`slave/lib/sys/soft_reboot.py`、`Skills/buffer-conventions` 規則一、
> `doc/03_notes/01_changelog.md` §38、`todo/08_lvgl_reinit.md`

---

## 0. 一句話

**「配置在 GC heap 之外的東西會漏」是對的，但這塊板子上真正會漏的不是
`heap_caps` 那些 —— 它們在 soft reboot 時自己就回來了。真正無解的是 LVGL，
因為它的配置在 GC heap **裡面**，而 C 層指標在外面。**

---

## 1. 三種記憶體，soft reboot 後的下場不同

| 來源 | 誰在用 | soft reboot 後 | 為什麼 |
|---|---|---|---|
| **GC heap**（`m_malloc` / `gc_alloc`） | 所有 Python 物件、`bytearray`、**LVGL 的全部配置** | 整個重來，內容消失 | `mp_init` → `gc_init` 重建 heap |
| **`heap_caps.malloc(CAP_DMA)`** | `lib/sys/buffer_hub.alloc_dma()`（唯一入口） | **實測：會回來** | 見 §3；推測與 `gc_init` 的 heap 重置範圍有關 |
| **內部 SRAM 的靜態/BSS** | driver 殘留、`mp_lv_roots`、`heap_caps` 的追蹤陣列 | **原封不動** | 不是 heap，沒人清 |

關鍵差別：**BSS 的指標活著，但 heap 上的內容沒了** → 這就是 LVGL 死指標的成因。

---

## 2. LVGL：唯一真正無解的那個

```
ext_mod/lvgl/mem_core.c   lv_malloc_core() → gc_alloc()
    → LVGL 的配置全在 GC heap 上（會被 soft reboot 清掉）
gen/lvgl_api_gen_mpy.py   void *mp_lv_roots                    ← .bss，不清
                          static bool mp_lv_roots_initialized  ← .bss，不清
    ELF: 3fcaceec B mp_lv_roots / 3fcacef4 b mp_lv_roots_initialized$7
```

軟重開機後 `lv_global` 指向死掉的 heap，而 `mp_lv_init_gc()` 因為旗標還是
`true`、再也不會重建它。實測 `lv.obj()` / `lv.screen_load()` / `lv.deinit()`
**直接打死板子**。

**為什麼不能靠換 allocator 救**（想過，三層阻礙）：

1. `MICROPY_MALLOC_USES_ALLOCATED_SIZE` 沒開 → 沒有 size 記錄，
   `lv_free` / `lv_realloc` 沒辦法交給 `heap_caps.free/realloc`。
2. 就算 C 物件活了，Python binding 的包裝物件、root pointer registry
   還是在 GC heap 上 —— 全部消失。
3. C 端的 callback（動畫／timer callback、`flush_cb`）握著**上一個 session
   的 Python 物件指標**。軟重開機後它們是死的，動畫一到期就炸。

所以「把 LVGL 的記憶體搬到 PSRAM」只會把「當場炸」換成「之後炸」，
不是解法。目前的正解是 `lvgl_init._soft_reboot_recover()`（硬重置 + 標記檔）。

---

## 3. `heap_caps`：孤兒會自己回來（違反直覺，兩次量測才定案）

`mp_heap_caps` 用一個 BSS 靜態陣列追蹤配置過的指標
（`HEAP_CAPS_MAX_TRACKED` = 128），並提供 `heap_caps.reset()` = `untrack_all()`。
文件寫它是「designed to be called from boot.py」—— 但**本專案從未呼叫過**。

於是預期是「soft reboot 後孤兒會卡在內部 SRAM」。實際量測（11401）：

| | A 基準 | B 配置後（未 free） | C 軟重開機後 | D `reset()` |
|---|---|---|---|---|
| 第 1 輪 | 118,023 | 68,859（−49,164） | **106,359** | 106,359（**回收 0**） |
| 第 2 輪 | 106,359 | 40,807（−65,552） | **95,895** | 95,895（**回收 0**） |

**孤兒在 C 那一格就回來了**（−49,164 → 只少 ~11.7 KB），
所以 D 的 `reset()` 無事可做。推測：`mp_init` → `gc_init` 的 heap 重置
涵蓋了那塊內部 SRAM（GC heap 會用到內部 SRAM）。

> ⚠️ **我第一次的結論是錯的**。第一次量到「軟重開機後仍佔 62 KB、reset 回收
> 65 KB」，看起來完美支持「孤兒會漏」——但那是**量測假象**：
> 基準本身在漂移（每次中斷 main.py 進 REPL 都留下一點東西），
> 而且不同輪的基準不可比。**同一個 session 內、可重複的 A/B 才算數。**

### 那 `heap_caps.reset()` 還要放嗎？—— 放，但定位是保險

- 同一輪裡還有追蹤項時，它**真的會 free**（實測 8 × 16KB 全部回收）。
- 放開機最前面（任何配置之前）是為了**避免誤殺本輪剛配好的緩衝**。
- 現行 config 沒踩到：沒有任何地方傳 `try_dma=True`，SD 也沒起來
  （`SD card init error: 16`）。所以正常開機是 no-op。
- 一旦有人打開 `try_dma=True` 或 SD 起來了，這條路就會被走到。

---

## 4. 未解：~10 KB／輪的殘差

量測時看到 `internal_free` 的基準每輪都在掉：

```
118,023  →  106,359  →  95,895
```

這**不是** heap_caps 記帳的那些（`reset()` 回收 0）。來源未定。
嫌疑：反覆用 Ctrl-C 打斷 `main.py` 進 REPL，讓 driver 每次重新配置
但沒有走正常 teardown（`spi_drv` 的 `SPI(sid).deinit()` 就是為此寫的）。

**這個要另外查**，別跟 LVGL 那條混在一起。要驗的話：
在**不中斷 main.py** 的前提下連續做 soft reboot，看基準會不會掉。

---

## 5. 這份筆記的教訓（方法論）

1. **基準會漂移就不要用它做結論** —— 先確認同一個 session 內連續量三次是穩定的。
2. **A/B 要可重複**：跑兩輪，兩輪同形狀才算定案。
3. **「聽起來對」的機制要量**：`heap_caps.reset()` 的文件寫得像是必需品，
   實測在這塊板子上是 no-op。
4. 但**「目前 no-op」不等於「不該存在」** —— 要分開講「機制對不對」
   與「現在有沒有被觸發」。
