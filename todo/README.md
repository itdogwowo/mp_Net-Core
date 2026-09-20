# TODO — 測試追蹤清單

> **用途**：存放「不同模組、不同目標」的測試跟進清單。每份 md 記錄該模組/目標的待測項目、已完成驗證、以及後續要補的實測。
> **最後更新**：2026-08-21

## 怎麼用

- 每個模組/目標一份 md，命名 `NN_<名稱>.md`（依序編號）。
- 用 `- [ ]` 待測、`- [x]` 已完成，標記跟進狀態。
- 新模組要開清單時，複製 `_template.md` 改標題即可。
- 只在「真機/實測」通過後才勾 `[x]`；單元/loopback 自測另註明，不算實測完成。

## 清單索引

| 檔案 | 範圍 | 狀態 |
|---|---|---|
| [01_file_update.md](01_file_update.md) | 檔案更新流程（FILE_* 0x20xx） | loopback 自測通過，實測待補 |
| [02_rs485_de.md](02_rs485_de.md) | RS485 半雙工 DE 控制（1ms / rs485_hd 全自動） | 1ms 實測通過，rs485_hd 待真機驗證 |
| [03_signal_router.md](03_signal_router.md) | 訊號 Router（頻道間轉送 / 路由表） | P1 離線自測 74 項通過，P2~P6 待做 |
| `_template.md` | 新清單範本 | — |

## 待開清單的模組（依 doc/02_guides 順序）

- [ ] `01_fast_io` — SD raw 高速儲存
- [ ] `02_uart_motor` — UART 電機控制器（RS485 通道測試可參考 [02_rs485_de.md](02_rs485_de.md)）
- [ ] `04_lcd_bus` / `05_tft_usage` — LCD/TFT
- [ ] `06_lvgl_ui` — LVGL UI
- [ ] `07_jpeg` — JPEG 解碼
- [ ] `08_pixel_subsystem` — pixel 播放
- [ ] `09_cores` — 核心實例
- [x] `16_signal_router` — 訊號 Router（[03_signal_router.md](03_signal_router.md)）

## 待跟進的目標（integration / hardware）

- [ ] **master_timer_slave 整合**（合作方合同，含 OTA 0x22xx 對接）
- [ ] **MCU ↔ MCU 對等傳輸**（需先補「來源位址 + 回給來源」的定址機制）— 轉送面由 [03_signal_router.md](03_signal_router.md) 接手；跨裝置多跳環仍無防護
- [ ] 傳輸通道：UART（RS485）/ ESP-NOW / WS 各自實測
- [ ] 不同硬體（ESP32-S3 變體、無 SD 卡的 fallback `/sd` 在 flash 上）
