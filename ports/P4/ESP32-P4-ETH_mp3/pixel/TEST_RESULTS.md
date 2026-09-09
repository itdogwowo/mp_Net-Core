# pixel 子系統 — 測試結果與未來方向

> 本文件記錄 pixel 子系統（波形數學 + 色彩轉換）的驗證結果、性能基準，以及接下來的方向。
> 架構說明見同目錄 `README.md`。

## 1. 三條硬約束（設計原則）

1. **library 只放 `slave/lib/`**：數學/色彩核心一律放 `slave/lib/`，`pixel/effects/` 只留效果目錄。
2. **核心 `@micropython.viper`、全程整數**：無 `math.sin`/`math.pi`/浮點、無查表。
3. **數值域固定 12-bit `0-4095`**：輸出 `array('H')`（uint16 只用低 12 位）。

## 2. 核心技術

### 2.1 波形（`slave/lib/sw/PixelMathMethod.py`）
- **免查表多項式逼近**：拋物線基底 + 二次修正 `922*(y²-y)>>12`，把 0-65535 相位映射到 0-4095 正弦。取代舊專案的 65536 點查表。
- **決定性（無狀態）**：`value_at(comp, g)` 給全域幀直接回傳單值，是 `Effect.restart()`/`seek()` 的基石。

### 2.2 波表快取（`slave/lib/sw/effect_core.py`）
- **啟動即算、off 即丟、重啟重算**：波形只在 `Effect` 建構時算一次 `array('H')`（波長 = `end_Time`，與 pixel 數無關），之後每幀只做 index 讀取。
- **乘數變加數**：`frame()` 熱路徑用 `g += spacing` 累加 + 單次減法取模，去掉 `i*spacing` 乘法與昂貴的 `%`。

### 2.3 色彩（`slave/lib/sw/PixelMathMethod.py`）
- HSV↔RGB，兩套位深（8-bit 0-255 / 12-bit 0-4095）、各雙向。
- **bulk 批次**：`hsv_to_rgb8_buf` / `rgb_to_hsv8_buf` / `hsv_to_rgb12_buf` / `rgb_to_hsv12_buf`。
- 修掉舊專案 `mp_LEDController` 的 bug：RGB 順序（舊 G,R,B → 新 R,G,B）、飽和度（`delta*SCALE//max_val`）、色相 offset（`+120/+240` 不再被整數除法吞掉）。

### 2.4 優化模式：loop 在 viper 內
- ❌ 不做：`for i in range(n): viper_func(...)`（逐 pixel 跨進 viper，有呼叫開銷）。
- ✅ 全做：`@micropython.viper def bulk(...): for i in range(n): ...`（迴圈在 viper 內，零逐 pixel Python 開銷）。
- 證據：每像素耗時從 64px 到 2000px 完全持平，無線性累積。

## 3. 測試套件（`test/pixel/`）

| 檔案 | 目的 |
|---|---|
| `test_pixel_math.py` | 波形 + Effect 單元正確性（27 項） |
| `test_pixel_color.py` | 色彩單元正確性（18 項） |
| `test_pixel_full.py` | 基準 vs 優化：全角度準確度 + 速度對比 |
| `test_pixel_bulk.py` | 大批量：正確性 + 性能 + 準確度 |
| `bench_pixel_math.py` | 波形 + `Effect.frame` 吞吐 |
| `bench_wave_cache.py` | 波緩衝 vs 每幀現算 對照 + 記憶體 + 損益平衡 |
| `bench_pixel_throughput.py` | bulk 每秒 RGB 轉換次數（loop 在 viper 內） |

## 4. 驗證結果（裝置，MicroPython + viper）

### 4.1 準確度（與浮點基準對齊，已定案，不需重跑）

| 路徑 | 樣本量 | 誤差 |
|---|---|---|
| `_wave01_q12` | 全相位 65536 點 | max 4 / 4095（0.1%） |
| `_sin_q12` | 全相位 65536 點 | max 6 / 4096（0.12%） |
| hsv→rgb 8-bit | 36 萬點（hue×s×v 密集） | max 2 LSB |
| rgb→hsv 8-bit | 36 萬點 | hue 角 max 1.0°，s 誤差 1，v 誤差 0 |
| hsv→rgb 12-bit | 36 萬點 | max 2 LSB |
| rgb→hsv 12-bit | 36 萬點 | hue 角 max 1.0°，s 誤差 1，v 誤差 0 |

- PC 與裝置結果**完全一致**：證明 `if _MP:` 的 viper 分支與 `else:` 的 PC 純 Python 分支運算邏輯相同，viper 優化沒有改動數學。

### 4.2 波形性能（波表 + viper）

| 項目 | 結果 |
|---|---|
| `_wave01_q12` 單值 | 9.0 µs/值（viper） |
| `Effect.frame`（64px，波緩衝 + viper） | 72.9 µs/幀 → 理論 ~13709 FPS |

### 4.3 波緩衝策略（eyes 波長 320 幀）

| 項目 | 結果 |
|---|---|
| 一次性算波（啟動成本） | 24.6 ms |
| 波緩衝記憶體 | 608 bytes（波長 × 2B，與 pixel 數無關） |
| 每幀現算 → 波緩衝 index | 6.3 ms → 1.0 ms（純 Python index），再 viper 72.9 µs |
| 損益平衡 | 約 11 幀回本 |

### 4.4 色彩吞吐（bulk，loop 在 viper 內）

| 方向 | 每像素 | 每秒轉換（2000px） |
|---|---|---|
| hsv→rgb 8 | 2.68 µs | 372 k px/s |
| rgb→hsv 8 | 1.78 µs | 563 k px/s |
| hsv→rgb 12 | 2.06 µs | 486 k px/s |
| rgb→hsv 12 | 1.84 µs | 545 k px/s |

需求對照：**50 FPS × 2000 px = 100 k px/s**，所有方向都遠超（3.7~5.6 倍餘裕）。

## 5. 已修掉的問題（測試/裝置驗證挖出來的）

1. **`array('H')` 反向 slice**：MicroPython 不支援 `buf[::-1]`（負步長），`reverse` 功能會崩 → 改用反向寫入 index。
2. **`@micropython.native` 用法**：`micropython.native(fn)` 是 runtime 屬性呼叫（部分 firmware 無 native emitter 會炸 `AttributeError`），正確是 `@micropython.native` 裝飾器語法（編譯器攔截）。
3. **`sys.exit()` 在 MicroPython**：未捕捉的 `SystemExit` 會觸發 soft reboot → 測試 `__main__` 只在 PC 呼叫 `sys.exit`。
4. **舊專案色彩 bug**：RGB 順序、飽和度、色相 offset（見 §2.3）。

## 6. 未來方向

### 6.1 彩色接入（下一輪，接口已就緒）
- bulk 色彩接口目前是「暫時包裝」，**尚未接進 scatter/effect/controller**。
- 需要：新增 `hsv`/`rgb` write 模式（`pixel_layout.py` 的 `_SCATTER`）+ 多通道 effect 輸出（effect 吐 hue 相位 + 值，scatter 內做 HSV→RGBW）+ 調色盤。

### 6.2 效果移植
- 已移植：`wave`（舊 main 現時生效）、`eyes`、`breathing`。
- 待移植：舊 `wave_library.py` 的 stepping/overlay/pulse 滑窗（波形類，可波表+viper）；狀態機/隨機類（thunder/lightning，override `frame`）；浮點類（heat_wave/standby，需降到整數）。

### 6.3 調色盤
- 舊 `pattern_library.py` 有 FastLED 風格調色盤（RAINBOW/FIRE/OCEAN/…），可接進彩色 effect 當 hue 來源。

### 6.4 播放優化
- **預先計算下一個燈效**：播 A 時 `next_effect._compile_wave()` 預算好 B，切換零等待。
- **黑幕 / 漸入漸出過渡**：藏住切換時的算波成本。
- **core0 計算 / core1 顯示**：雙核心拆分（計算緩衝 → 排進 PixelLayout → PixelController 顯示）。

### 6.5 config 自動化
- 目前 deploy 工具跳過 config.json，port 選擇靠人工把 `ports/*/config.json` 複製到裝置 `/config.json`。
- 可加「選 port → 自動帶出對應 config」的腳本。

## 7. 結論

波形數學 + 色彩轉換工具鏈已完成並驗證：
- **準確度**：與浮點基準誤差 0.1%（波形）/ 1°（hue）/ 2 LSB（RGB），已定案。
- **性能**：viper bulk 平穩 1.8~2.7 µs/px，每秒數十萬次 RGB 轉換，遠超 50fps × 2000 顆需求。
- **優化模式**：全程「loop 在 viper 內」，零逐 pixel 呼叫開銷。

下一步是把彩色 bulk 接口接進 scatter/effect，實現真正的彩色效果。
