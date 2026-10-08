# TFT + lcd_bus 最新使用指南（2026-08 驗證版）

> **用途**：`slave/` 的 Python 層（bus_adapter / TFT.py / jpeg_player_task）使用指南——顯示 API 選擇、decode/DMA 重疊手法、測試工具。
> **分類**：使用教學（02_guides）
> **最後更新**：2026-08-18
> **對應 firmware**：最新（`mp_Make-Tools` build，含 lcd_bus 效能修復）
> **相關**：`04_lcd_bus.md`（lcd_bus 模組總覽）、`03_notes/05_psram_zero_block_plan.md`（PSRAM 零阻塞計劃）

---

## 1. 架構總覽

```
MicroPython 層（slave/）
  jpeg_player_task / tft_dma_bench / tft_pipeline_visual
        │  lcd.set_window / show_async / present / write_data_async
        ▼
  lib/hw/TFT.py（ST7789 driver 包裝）
        │  lcd._bus
        ▼
  lib/sys/bus_adapter.py（SpiBusAdapter / I80BusAdapter / I2cBusAdapter / RgbBusAdapter）
        │  spi.write / wait / pending / wait_all
        ▼
  C 模組（mp_lcd_bus / lcd_bus）
    SPIBus   — 自握 esp-idf raw SPI master（唯一手寫 copy 邏輯的 bus）
    I80Bus   — esp_lcd_panel_io_tx_color（PSRAM 內建支援）
    RGBBus   — esp_lcd_panel_draw_bitmap（PSRAM 內建支援）
    I2CBus   — esp_lcd_panel_io（同步，無 DMA queue）
```

---

## 2. lcd_bus SPIBus 最新 API（ESP32-S3）

### 建構

```python
import lcd_bus
spi = lcd_bus.SPIBus(
    data=[14],          # data line（1=單線，2=雙線，4=QSPI）
    clk=21,
    freq=80000000,      # 80MHz
    host=1,
)
# 註：此專案由 driver/spi_drv.py 依 config.json 建立，見下
```

### 方法

| 方法 | 用途 | 阻塞？ |
|---|---|---|
| `spi.write(buf, cmd=-1, addr=0, multiline=True)` | 送資料。**>32KB 自動 async 分 chunk**；PSRAM 自動處理 | 單筆 ≤32KB 且內部 RAM：async。**PSRAM/大 buffer：fire 含 ISR copy（~2.3ms/32KB）** |
| `spi.wait(tid)` | 等特定 transfer 完成 | 阻塞 |
| `spi.wait_all()` | 等全部完成 | 阻塞 |
| `spi.pending()` | queue 中未完成數（0-8） | 非阻塞 |
| `spi.is_busy()` | 是否忙碌 | 非阻塞 |
| `spi.lane_count()` | lane 數（1/2/4） | — |
| `spi.readinto(buf, write_val=0)` | 讀取（已修正 zero_buf clamp） | 阻塞 |
| `spi.deinit()` | 釋放（deinit 後 handle 清 NULL，防 use-after-free） | — |

### 效能關鍵行為（v1.29.0-preview + v5.5 esp-idf）

1. **大 buffer 自動分 chunk**：`len(buf) > 32768` 或 PSRAM → C 內 `spi_wait_free_slot + enqueue_raw` 分 32KB chunk，8-deep queue。
2. **PSRAM 代價**：v5.5 driver `esp_ptr_dma_capable()` 只認內部 SRAM，PSRAM 一律在 ISR 內同步 copy → **單筆 32KB PSRAM ≈ 2.3ms**（內部 RAM <100us）。這是 fire 12ms/幀的來源（見 `03_notes/05_psram_zero_block_plan.md`）。
3. **GC 條件化**：`wait_all` 正常 drain 完畢不再 `gc_collect()`（修復每幀 ~200ms 隱形地雷）。

---

## 3. bus_adapter.py（Python 層，最新）

### SpiBusAdapter 關鍵行為

- `write_data_async(data)`：**大 buffer 直接 `spi.write()`**（C 自動 async 分 chunk）。
  ⚠ 已移除 Python bounce 攔截（`_write_bounced` 不再攔截 >32KB）——那是序列化元兇。
- `flush()` → `spi.wait_all()`
- `wait(handle)` → `spi.wait(handle)`

### 分配 framebuffer（lib/buffer_alloc.py，新版）

```python
from lib.sys.buffer_alloc import alloc_fb, Fb
fb = alloc_fb(153600, fb_mode="auto")   # auto: SPIRAM→DMA→bytearray
# fb.buf 是 memoryview(heap_caps) 或 bytearray
fb.free()                                # 依來源正確釋放（不會誤 free bytearray）
```

⚠ **鐵律：只有 `isinstance(buf, memoryview)`（heap_caps 成功）才可 `heap_caps.free(buf)`；fallback bytearray 留給 GC。** 對 bytearray 呼叫 `heap_caps.free` 會腐蝕 heap → 硬當機（已修 tft_dma_bench/jpeg_player/fast_io）。

---

## 4. TFT.py driver 最新 API（ST7789）

### 建立（由 boot.py / tft_drv 完成）

```python
from lib.sys.bus_adapter import SpiBusAdapter
adapter = SpiBusAdapter(spi, dc, cs, rst)
lcd = ST7789(adapter=adapter, width=240, height=320, ...)
```

### 顯示 API 選擇（效能排序）

| API | 內部 | 用途 | 效能 |
|---|---|---|---|
| `lcd.show_frame(data)` | write_frame（每 chunk wait） | 同步顯示 | 慢（序列化） |
| `lcd.show(data)` | set_window + write_data_async + flush | 同步顯示 | 中 |
| `lcd.show_async(data)` | set_window + write_data_async | 異步送（caller 需 flush） | 中 |
| **`lcd.begin_display()` + `lcd.present(data)` + `lcd.present_wait()`** | 一次設窗 + RAMWR + write_frame_dma | **連續幀播放（pipeline）** | **最佳** |
| `lcd.set_window()` | CASET/PASET/RAMWR | 每幀重設窗 | 每幀 +1.1ms |

### ST7789 的 window 機制（重要）

- **必須設過 window 才能正確顯示**；reset 預設全螢幕，但被任何子視窗操作污染後失效。
- **每幀不必重設**：`begin_display()` 設一次全螢幕後，每幀 `present()`（只發 RAMWR）即可連續刷全螢幕（column/row counter 自動 wrap）。
- 無 bypass window 的模式（DBI 沒有線性 blit）。

---

## 5. decode/DMA 重疊（本專案核心手法）

### 正確 pipeline 結構（fire 先 → 計算 → 才 wait）

```python
lcd.begin_display()
tid = lcd.present(frame_N)          # fire（async，DMA 背景送）
for i in range(total):
    _decode_next(frame_N1)          # 計算 20ms — DMA 全程在背景傳
    lcd.present_wait()              # wait ≈ 0（已傳完）
    tid = lcd.present(frame_N1)
```

### 實測數據（240x320 RGB565 @80MHz 單線）

| 手法 | work=20ms 假 decode | 說明 |
|---|---|---|
| **serial**（decode→fire→立即wait） | 39ms（25fps） | 完全序列化 |
| **pipeline**（fire→decode→wait） | 32ms（31fps） | 重疊生效，剩 fire 12ms |
| 純傳輸（work=0） | 17ms（59fps） | 理論 15.36ms |

### 重要教訓

1. **每幀 `wait_all` 內嵌 gc_collect 是最大地雷**（~200ms/幀）——C 已條件化。
2. **PSRAM framebuffer 的 fire 有 12ms ISR copy 代價**（v5.5 driver 不認 PSRAM DMA 直讀）——見 `03_notes/05_psram_zero_block_plan.md` 兩方案（暫緩）。
3. **手動分段 vs 單次大 write**：單次 150KB write 有顯示不全的歷史問題，`tft_dma_bench`/`tft_pipeline_visual` 用手動 32KB 分段為準。

---

## 6. TFT Chunked Write Session API（補充）

`slave/lib/hw/TFT.py` 基類新增的 chunked 寫入會話，用於需要精確控制「分塊傳輸 + 進度查詢」的場景（`begin_write` / `write_pixels` / `write_pixels_nonblock` / `end_write`）。

### 原理

`BusAdapter.write_data_async()` 已有兩種行為語義：

- **DMA 模式**：回傳 handle (truthy) = 排隊成功；回傳 None = 隊列滿
- **非 DMA 模式**：永遠回傳 True（同步寫完）

TFT 層直接映射這組語義，不需額外判斷。

### 新增 API

```python
TFT(..., chunk_size=8192)   # chunk_size=0 表示不分塊（預設行為）
```

| 方法 | 用途 | 對應經典 |
|---|---|---|
| `begin_write(x, y, w, h)` | 開始寫入會話 — set_window + 重置計數 | Adafruit `beginWrite()` / TFT_eSPI `startWrite()` |
| `write_pixels(data)` | 中斷寫入 — 等傳輸完成才返回 | TFT_eSPI `pushPixels()` |
| `write_pixels_nonblock(data) → bool` | 非中斷 DMA 嘗試 — True=排隊成功, False=重試 | 傳統 `_nonblock` 後綴 |
| `end_write()` | 結束寫入會話 — flush DMA | Adafruit / TFT_eSPI `endWrite()` |

唯讀屬性：`.chunk_total`（總塊數）、`.chunk_done`（已完成）、`.remaining`（剩餘）、`.busy`（DMA 傳輸中）。

所有螢幕差異由 `adapter.set_window()` 層處理（RGB bus 為 no-op）。

### 用法範例

```python
# 非中斷 DMA 模式
lcd.begin_write(0, 0)
while lcd.remaining > 0:
    chunk = fetch_current_chunk()
    while not lcd.write_pixels_nonblock(chunk):
        pass                        # DMA 隊列滿，原地重試
    advance_to_next_chunk()
lcd.end_write()
```

---

## 7. 測試工具（slave/）

| 工具 | 用途 |
|---|---|
| `tft_probe.py` | 診斷 bus 型態（DMA？lane？） |
| `tft_dma_bench.py` | 場景 A-E 效能基準（direct/show_frame/show_async/show/present） |
| `tft_min_dma_probe.py` | 消歧：T0 GC 成本 / T1-T4 傳輸路徑 |
| `tft_pipeline_probe.py` | 重疊驗證（serial vs pipeline） |
| `tft_pipeline_visual.py` | **視覺驗證**：螢幕彩虹滾動 + frame time |
| `tft_pipeline_profile.py` | 拆解 fire/decode/wait 四段耗時 |

快速驗證指令（soft reboot 後）：

```python
import tft_pipeline_visual
tft_pipeline_visual.run(work_ms=0)          # 純傳輸，應 ~17ms + 螢幕全滿
tft_pipeline_visual.run(work_ms=20)         # 重疊，應 ~32ms
tft_pipeline_visual.run(work_ms=20, serial=1)  # 對照，應 ~39ms
```

---

## 8. firmware 建置（mp_Make-Tools）

```bash
cd /Users/user/Documents/code/git/mp_Make-Tools
python3 make.py          # target=esp32s3, BOARD=ESP32_GENERIC_S3_SPIRAM_OCT
# 產出 build/ESP32_GENERIC_S3_SPIRAM_OCT.bin
```

⚠ **改 C 碼後必須同步 ext_mod 再 build**：

```bash
cp /Users/user/Documents/code/git/mp_lcd_bus/esp32_src/*.c \
   /Users/user/Documents/code/git/mp_Make-Tools/ext_mod/mp_lcd_bus/esp32_src/
```

git manager 會在 build 時 reset ext_mod 到 origin/main——**未 commit 的修改會被覆蓋**。

## 相關文件

- `04_lcd_bus.md` — lcd_bus 模組總覽（C 層 + Python 統一層 + 舊版 API）
- `07_jpeg.md` — JPEG 模組（decode_into 零拷貝 framebuffer）
- `03_notes/02_buffer_architecture.md` — 多級緩衝架構（L5 輸出層）
- `03_notes/05_psram_zero_block_plan.md` — PSRAM 零阻塞計劃
