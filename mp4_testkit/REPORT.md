# mp4_testkit：I/O 抖動分析與效能優化報告

## 背景與問題

在 ESP32-S3 + MicroPython 的圖片序列播放器中，單獨測試時：

- JPEG 解碼可以達到很高 FPS
- LCD 寫入也可以達到很高 FPS

但一旦把「讀取檔案 → 解碼 → 顯示」串成完整管線，會出現：

- FPS 變低
- FPS 抖動明顯（偶發卡頓）

本報告整理我們的定位過程、量測方法、數據結果與最終可落地的架構建議。

## 核心結論（TL;DR）

- 真正的樽頸不是解碼或顯示本身，而是 **小檔案（folder）模式的 open/read/close 造成的 I/O 抖動與固定成本**。
- 改用 **pack 單檔容器（keep-open sequential read）** 可以把吞吐提升約 30～40 倍，並大幅降低抖動。
- 使用 **SDIO/SDMMC（4-bit）** 相比 flash 小檔案模式有顯著提升；搭配 pack 後可得到最佳吞吐與穩定性。
- RAM preload 能把讀取成本降到 0，但不適合作為常態方案（容量受限）；適合作為 debug/對照基準或小素材模式。
- pipeline 預設與參數已做「可省略」與「智能化」：使用者不需要理解 `io_prefetch/io_read_chunk/preload_limit_bytes`，開發者仍可覆蓋。

## 量測方法

為了把抖動來源拆開，我們做了三層量測：

1. 檔案 I/O micro-bench（排除解碼與顯示）
2. pack 單檔讀取 micro-bench（排除 open/close 成本）
3. 播放器 runtime stats（同時觀察解碼、顯示、讀取耗時）

### A. 檔案 I/O：bench_fs.py

用於對比「小檔案 open/read/close」與「單檔 keep-open sequential read」。

### B. 多介質對比：bench_transfer.py / bench_profiles.py

用於對比：

- flash folder vs flash pack
- sd folder vs sd pack
- 並提供延遲尾巴的統計（p50/p90/p99、>20ms 次數等）

### C. 播放器 runtime stats（Core0/1）

在播放器主迴圈中統計：

- `avg_disp_us`：LCD 單幀寫入耗時
- `avg_dec_us`：JPEG decode_into 耗時
- `avg_read_us`：讀取一幀 JPEG 的耗時（含 FS/pack 路徑）
- `read_KB/s`：以 bytes/us 推算的讀取吞吐

## 數據與對比（重點表格）

### 1) 介質/格式 I/O 對比（實測）

來自 `bench_transfer.run()` 的結果（同一塊板子/同一批素材）：

| 測試組合 | avg_us（每次讀取） | MB/s | jitter | 註解 |
|---|---:|---:|---:|---|
| flash + folder | ~30,889us | ~0.03 | ~1.07x | 小檔案 open/close 固定成本大，且容易抖動 |
| flash + pack | ~1,112us | ~1.14 | ~1.45x | 大幅提升，穩定性也明顯更好 |
| sd + folder | ~2,533us | ~0.42 | ~1.25x | SDIO 對小檔案改善很明顯，但仍有 open/metadata 成本 |
| sd + pack | ~1,051us | ~1.20 | ~1.49x | 最佳組合（吞吐最高且穩定） |

倍數（以 flash+folder 為基準）：

- sd+folder：約 14×
- flash+pack：約 38×
- sd+pack：約 40×

### 2) 播放器 FPS 對比（實測）

以下以 `1s_frames`（每秒 frame 數）觀察：

- sd + folder（preload=0）：約 28～33 FPS，且讀取成本會逐步變大（`avg_read_us` 上升、`read_KB/s` 下降）。
- sd + pack（preload=0）：約 34～35 FPS，讀取成本穩（`avg_read_us` ~1.1ms、`read_KB/s` ~1,050KB/s）。
- preload=1：讀取成本為 0，FPS 可穩在 ~37 FPS，但受 RAM 限制，不作為常態方案。

## 根因分析：為什麼「單項很快」但「整體很慢又抖」

### 1) 小檔案模式的固定成本

大量小檔案會反覆觸發：

- open/close
- 目錄/metadata 查詢
- 檔案系統內部鎖與快取行為

這些成本相對於 1KB 級別的小 JPEG 會變得非常顯著，並造成尾巴延遲（偶發卡頓）。

### 2) 外部記憶體系統與仲裁（ESP32-S3）

即使 DMA channel 很多，仍會受到：

- cache refill / 外部記憶體匯流排仲裁
- PSRAM/flash 的共享帶寬影響

因此播放時「讀取 + 顯示 DMA」容易互相干擾，造成讀取耗時逐步漂移或偶發尖峰。

## 最終架構建議（不吃 RAM、用戶不複雜）

### 推薦預設：SDIO + pack

- 使用者只需要一個檔案：`/sd/background.jpk`
- 播放時只 open 一次，後續全程 `readinto` 串流
- 穩定、吞吐高、不依賴 RAM preload

### 保留簡單模式：SDIO + folder

- 讓使用者能直接把 JPEG 丟到 `/sd/background/`
- 效能中等，偶發抖動風險較高

## 使用方式（給使用者）

### 1) folder 模式（簡單）

- `assets_pack` 留空
- `assets_root="/sd"`（或 `"/jpeg"`）
- 把圖片放到：`{assets_root}/{type}/`，例如 `/sd/background/*.jpeg`

### 2) pack 模式（推薦）

- 設定 `assets_pack="/sd/background.jpk"`
- 可選擇保留 `assets_root="/sd"` 作為 fallback

pack 檔可由 PC 端工具打包產生：

- `mp4_testkit/pack_jpegs.py`

## Pipeline 參數（開發者向）

### io_buffers / frame_buffers

- `io_buffers`：Core0 讀取 → Core1 解碼之間的 buffer 槽數（吸收 I/O 尖峰）。
- `frame_buffers`：Core1 解碼 → Core0 顯示之間的 buffer 槽數（吸收顯示端阻塞）。

在 SDIO + pack 的量測下，I/O 尾巴延遲很乾淨（>20ms 幾乎為 0），因此預設 `3/3` 已足夠；在 flash+folder 或不穩定 I/O 時，可提高 `io_buffers` 以吸收尖峰。

### io_prefetch（已內建預設）

`io_prefetch` 是「水位線」：當 `io_hub.fill_level < io_prefetch`，Core0 才會讀下一張填入 io_hub。

已支援負數作相對值：

- `io_prefetch = -1` → `io_buffers - 1`（更積極補貨）
- `io_prefetch = -2` → `io_buffers - 2`（較保守，預設）

可省略不填，會使用內建預設（`-2` 並保底至少 1）。

### preload（支援 int 模式）

`preload` 可用一個整數描述「預載張數等價」：

- `preload = 0`：關閉 preload
- `preload = N (>0)`：預載 budget = `max_jpeg_bytes * N`（約等於 N 張最大 JPEG 的容量）

此模式用來降低啟動期抖動；cache 用完會自動切回正常讀取並繼續向前（不會只循環前幾張）。

### preload_limit_bytes（保留兼容）

仍可使用 `preload_limit_bytes` 作為固定上限：

- `preload_limit_bytes > 0`：固定上限
- `preload_limit_bytes < 0`：智能上限（參考 max_jpeg_bytes、io_buffers 與 mem_free）

若 `preload` 使用 int 模式，會優先以 `max_jpeg_bytes * preload` 作為 budget。

## 播放節奏：pace_ms 語意修正

`pace_ms` 已修正為「每幀最少耗時（frame period）」而不是「額外 sleep」：

- `pace_ms=0`：跑最大 FPS
- `pace_ms=50`：目標約 20 FPS（若 decode+display 本身已超過 50ms 則無法達到）

在等待節奏的空檔期間，Core0 會嘗試補 `io_hub`（預讀下一張），避免 sleep 造成管線斷糧。

## 實作摘要（本 repo 改動）

- pack 容器格式與讀取：
  - `mp4_testkit/lib/pack_source.py`（避免每幀配置 4 bytes，使用 readinto）
  - `mp4_testkit/pack_jpegs.py`（PC 端打包工具）
- SDIO/SDMMC 掛載：
  - `mp4_testkit/lib/sdio_mount.py`
  - bench 會自動嘗試依 config 掛載 SD
- 量測工具：
  - `mp4_testkit/bench_fs.py`
  - `mp4_testkit/bench_pack.py`
  - `mp4_testkit/bench_profiles.py`
  - `mp4_testkit/bench_transfer.py`
- 播放器核心統計：
  - `mp4_testkit/Core0_worker.py`（統計 read/dec/disp 與 read_KB/s）
  - `mp4_testkit/Core1_engine.py`（cache 路徑 / io_hub 路徑）
- 設定：
  - `mp4_testkit/dp_config.json` 增加 `assets_pack` 與 `SDcard` 範本

## 建議的預設配置（可直接套用）

推薦（穩定/效能）：

- `assets_pack="/sd/background.jpk"`
- `SDcard.enable=1`
- `player.pipeline.preload=0`
- `player.pipeline.io_buffers=3`
- `player.pipeline.io_prefetch=2`

簡單（好用/可接受效能）：

- `assets_pack=""`
- `assets_root="/sd"`
- `player.pipeline.preload=0`
