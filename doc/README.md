# mp_Net-Core 文件索引

> **用途**：全部文件的單一入口。文件依主題分成三個分類，從這裡找你要的。
> **最後整理**：2026-08-21
> **歸檔**：舊版原始文件（改版前）保留在 `_archive/`，內容以分類目錄下的新版為準。

## 三個分類怎麼選

| 分類 | 看什麼的人 | 內容 |
|---|---|---|
| **01_protocol** 協議層 | 對接方 / 新增指令 / 寫 PC·Server 工具的人 | NC4 封包、指令集、schema、對外合約、性能基準 |
| **02_guides** 使用教學 | 要在 slave 上寫功能 / 用模組的人 | 各模組 API、怎麼接、怎麼跑、踩坑 |
| **03_notes** 筆記 | 維護者 / 想了解設計脈絡的人 | 調查記錄、架構筆記、計劃、變更紀錄 |

---

## 01_protocol — 協議層

| 文件 | 內容 |
|---|---|
| [01_nc4_protocol.md](01_protocol/01_nc4_protocol.md) | **NC4 封包協議（唯一真相）**：封包格式 / CRC32 / schema payload / 傳輸層 / 定址模型 |
| [02_command_index.md](01_protocol/02_command_index.md) | **完整指令索引**：全部指令域（0x10xx~0x32xx）的指令表總收錄 |
| [03_ota_protocol.md](01_protocol/03_ota_protocol.md) | OTA 0x22xx 設計：零常數約定 / 指令定義 / 長度限制 / 推薦流程 |
| [04_pixel_protocol.md](01_protocol/04_pixel_protocol.md) | Pixel 0x31xx：mode_type 語義 / byte 佔位 / 行為約定 / 時鐘同步 |
| [05_integration_overview.md](01_protocol/05_integration_overview.md) | 協議整合總規格（與 master_timer_slave 統一合約） |
| [06_migration_guide.md](01_protocol/06_migration_guide.md) | 對方指令遷移對照（人讀版：每條舊指令變成什麼） |
| [07_merge_comparison.md](01_protocol/07_merge_comparison.md) | 兩套系統全景比對（含對方 RS485 catalog 留存） |
| [08_performance_benchmark.md](01_protocol/08_performance_benchmark.md) | 網路 + 協議性能基準（甜蜜點 / 瓶頸 / 修改方法） |
| [09_bus_speed_protocol.md](01_protocol/09_bus_speed_protocol.md) | 臨時提速（bus_speed）**完整工作流程**：協商 / 時序 / 失敗處理 / master 整合範例 |

## 02_guides — 使用教學

| 文件 | 內容 |
|---|---|
| [01_fast_io.md](02_guides/01_fast_io.md) | SD 卡中央儲存管理器（Storage / StreamReader / alloc.json） |
| [02_uart_motor.md](02_guides/02_uart_motor.md) | UART 電機控制器（原始速度 / 行程模式 / 三點校準） |
| [03_memory_management.md](02_guides/03_memory_management.md) | heap_caps DMA 記憶體分配（malloc / free / 查詢 / 注意事項） |
| [04_lcd_bus.md](02_guides/04_lcd_bus.md) | lcd_bus 總線模組（新版非同步 DMA + 舊版 C API） |
| [05_tft_usage.md](02_guides/05_tft_usage.md) | TFT + lcd_bus 使用：顯示 API 選擇 / decode·DMA 重疊 / chunked write |
| [06_lvgl_ui.md](02_guides/06_lvgl_ui.md) | LVGL UI：架構 / 啟動 / 螢幕方向 / 字型生成 / 踩坑 |
| [07_jpeg.md](02_guides/07_jpeg.md) | JPEG 模組：decode / decode_into / block decode / benchmark |
| [08_pixel_subsystem.md](02_guides/08_pixel_subsystem.md) | pixel 子系統：效果 / mapping / modes / 播放清單 / 整合流程 |
| [09_cores.md](02_guides/09_cores.md) | cores 核心實例：Core_LVGL / Core_Comm |
| [10_file_update.md](02_guides/10_file_update.md) | 檔案更新流程：上傳/下載/兩段式 commit/斷點續傳/delta journal |
| [11_developing_effects.md](02_guides/11_developing_effects.md) | **開發燈效指南**：效果介面 / 三種寫法 / 雙核播放 / 四層設定 / 效能踩坑 |
| [12_network_switch_setup.md](02_guides/12_network_switch_setup.md) | 交換器設置與連線排障：DHCP snooping trust / half-open / 拿不到 IP / 上傳循環 / SOP |
| [13_audio_wav_module.md](02_guides/13_audio_wav_module.md) | **音訊模組（WAV 串流）**：硬體接線 / config 兩層 / 檔名自述契約 / playlist.json / 0x32xx 指令 / 多軌混音 / gmode 燈效綁定 / 測試 |
| [14_audio_bringup.md](02_guides/14_audio_bringup.md) | **音訊上板教學**：挑腳（S3 Octal-SPIRAM 33–37 不可用）/ 接線 / 出聲回歸 / irq 探針 / block vs irq A/B |

## 03_notes — 筆記

| 文件 | 內容 |
|---|---|
| [01_changelog.md](03_notes/01_changelog.md) | 更新紀錄：遠端更新鏈路 / 臨時提速 / lib 三級分類 / 解碼性能 |
| [02_buffer_architecture.md](03_notes/02_buffer_architecture.md) | 多級緩衝架構（L0~L5：分配 / Ring / 傳輸 / 協議 / 應用 / 輸出） |
| [03_ota_design_reference.md](03_notes/03_ota_design_reference.md) | ESP-IDF partition OTA 機制參考（寫入 / 確認 / 回退） |
| [04_rs485_de_timing.md](03_notes/04_rs485_de_timing.md) | RS485 DE 使能時序：20ms 調查 + 交接（20ms→1ms + rs485_hd） |
| [05_psram_zero_block_plan.md](03_notes/05_psram_zero_block_plan.md) | PSRAM framebuffer 零阻塞直送計劃（方案 A / B） |
| [06_raw_sd_plan.md](03_notes/06_raw_sd_plan.md) | Raw SD 繞過 FAT 兩階段計劃（Python 層 + Async C module） |
| [07_pixel_test_results.md](03_notes/07_pixel_test_results.md) | pixel 測試結果：準確度 / 性能基準 / 未來方向 |

---

## 舊版歸檔（_archive/）

改版前的原始文件完整保留在 [`_archive/`](_archive/)，供回溯比對。內容已重新整理到上面三個分類，**請以新分類目錄為準**。

## 相關（不在 doc/ 下）

- `Skills/buffer-conventions` — 緩衝層使用規範（alloc_dma / AtomicStreamHub / DMA）
- `Skills/mp-netcore` — slave 新增功能模組完整流程（schema / action / task / config）
- `slave/pixel/` — pixel 子系統程式碼（四層資料檔案）
- `cores/` — 獨立核心實例（Core_LVGL / Core_Comm）
- `todo/` — **測試追蹤清單**（各模組待測項目 / 給下一位測試者的步驟）
