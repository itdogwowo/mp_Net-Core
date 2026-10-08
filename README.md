# mp_Net-Core

ESP32-S3 MicroPython slave 專案 — 高效能 Server ⇄ MCU 傳輸控制系統（NC4 二進位協議 + 雙核心架構）。

## 文件索引

全部文件已按主題分成三類，入口在 [doc/README.md](doc/README.md)：

### 協議層（`doc/01_protocol/`）— 對接 / 新增指令 / 寫工具的人

- [NC4 封包協議（唯一真相）](doc/01_protocol/01_nc4_protocol.md) — 封包格式 / CRC32 / Schema payload / 傳輸層
- [完整指令索引](doc/01_protocol/02_command_index.md) — 12 個指令域、112 條指令的單一查詢表
- [OTA 0x22xx](doc/01_protocol/03_ota_protocol.md) — 韌體 OTA 設計（合作方合同）
- [Pixel 0x31xx](doc/01_protocol/04_pixel_protocol.md) — 模式播放（MODE_LIST/GET/SET/STOP/DETAIL）
- [協議整合總規格](doc/01_protocol/05_integration_overview.md) — 與 master_timer_slave 的三組整合指令合約
- [指令遷移對照](doc/01_protocol/06_migration_guide.md) — 人讀版：對方每條舊指令變成什麼
- [協議合併對照](doc/01_protocol/07_merge_comparison.md) — 兩套系統全景比對
- [網路 + 協議性能基準](doc/01_protocol/08_performance_benchmark.md) — 吞吐 / 甜蜜點 / 瓶頸
- [臨時提速（bus_speed）](doc/01_protocol/09_bus_speed_protocol.md) — 協商 / 時序 / 失敗處理

### 使用教學（`doc/02_guides/`）— 寫功能 / 用模組的人

- [SD 卡中央儲存管理器（fast_io）](doc/02_guides/01_fast_io.md)
- [UART 電機控制器（uart_motor）](doc/02_guides/02_uart_motor.md)
- [heap_caps DMA 記憶體分配](doc/02_guides/03_memory_management.md)
- [lcd_bus 總線模組](doc/02_guides/04_lcd_bus.md)
- [TFT + lcd_bus 使用指南](doc/02_guides/05_tft_usage.md)
- [LVGL UI 使用指南](doc/02_guides/06_lvgl_ui.md)
- [JPEG 模組](doc/02_guides/07_jpeg.md)
- [pixel 子系統](doc/02_guides/08_pixel_subsystem.md)
- [cores 核心實例](doc/02_guides/09_cores.md)
- [檔案更新流程](doc/02_guides/10_file_update.md) — 上傳/下載/兩段式 commit/斷點續傳
- [開發燈效指南](doc/02_guides/11_developing_effects.md)
- [交換器設置與連線排障](doc/02_guides/12_network_switch_setup.md)
- [音訊模組（WAV 串流）](doc/02_guides/13_audio_wav_module.md) — 0x32xx 指令 / playlist / 多軌混音
- [音訊上板教學](doc/02_guides/14_audio_bringup.md)
- [定時指令排程（schedule）](doc/02_guides/15_schedule.md)
- [訊號 Router](doc/02_guides/16_signal_router.md) — 頻道間互相轉送
- [Timer 計時器](doc/02_guides/17_timer.md)
- [節點發現流程](doc/02_guides/18_node_discovery.md) — 配對 / 定址 / 壞掉時怎麼查

### 筆記（`doc/03_notes/`）— 維護者 / 想了解設計脈絡的人

- [更新紀錄](doc/03_notes/01_changelog.md) — 遠端更新鏈路 / 臨時提速 / lib 三級分類 / 解碼性能
- [多級緩衝架構](doc/03_notes/02_buffer_architecture.md) — 資料從網路/SD 到 DMA 輸出的五層緩衝設計
- [RS485 DE 時序調查與交接](doc/03_notes/04_rs485_de_timing.md)
- [PSRAM 零阻塞計劃](doc/03_notes/05_psram_zero_block_plan.md) — PLAN
- [Raw SD 計劃](doc/03_notes/06_raw_sd_plan.md) — PLAN
- [上傳效能診斷](doc/03_notes/09_upload_performance_diagnosis.md) / [改動清單](doc/03_notes/10_upload_performance_changes.md)
- [音訊串流定案計劃](doc/03_notes/13_audio_wav_stream_plan.md) — 0x32xx 設計來源
- [統一渲染輸出中心（TxCenter）](doc/03_notes/17_tx_render_center_plan.md) — PLAN，做法待設計
- [遙控器強化計劃](doc/03_notes/19_remote_control_plan.md) — 配對 / 綁定 / 分階段

> `doc/03_notes/` 共 20 份（含兩份編號同為 17 的歷史遺留），完整清單見 [doc/README.md](doc/README.md)。
> 舊版文件保留在 `doc/_archive/`，內容以分類目錄下的新版為準。

## Skills（AI 開發輔助）

- `Skills/buffer-conventions` — 緩衝層使用規範（alloc_dma / AtomicStreamHub / DMA）
- `Skills/mp-netcore` — slave 新增功能模組完整流程（schema / action / task / config）
