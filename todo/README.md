# TODO — 測試追蹤清單

> **用途**：存放「不同模組、不同目標」的測試跟進清單。每份 md 記錄該模組/目標的待測項目、已完成驗證、以及後續要補的實測。
> **最後更新**：2026-10

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
| [03_signal_router.md](03_signal_router.md) | 訊號 Router（頻道間轉送 / 路由表） | **P1~P5 完成（離線自測 401 項全過）**，P6 上板實測待做 |
| [04_remote_pairing.md](04_remote_pairing.md) | 遙控配對（解除 / 驗證 / 衝突政策） | Task 1（解除雙邊同步）✅；**Task 2（驗證）✅ 2026-10 完成** —— 用 `0x1101` 空中查詢（真機 20~30ms 往返）；**Task 3 已取消**（由 `todo/05` D8 取代）|
| [05_node_pairing.md](05_node_pairing.md) | **節點發現與配對（傳輸層無關）** —— 抖動 / cid 身分 / 管子介面 / UI 分界線 | ✅ **階段 1~4 已實作 ＋ 真機驗證全部完成**（離線 153/153 PASS；§12 兩板實測，含抖動分佈、`reply_cid`、認主、閃寫保護、重啟換手、**解除後不重啟換手**）|
| [06_router_local_paths.md](06_router_local_paths.md) | Router 的兩個本機來源（`self` / `vbus`）—— 「絕對內部執行」怎麼走 | ✅ **已決：不改 Router**，分流在產生者側（`exec_cmd` ↔ `vbus.inject`）；待辦全部結案（`router_selftest` 75/75）|
| [07_now_setting_ui.md](07_now_setting_ui.md) | **ESP-NOW 設定 ＋ 遙控器設定 兩頁 UI** —— 傳輸層 vs 應用層、兩個清單的鍵與上限、FF 互斥、加密（6 格 / 共用金鑰）| 🟢 **兩頁已完成並上板**（`build_all: 7 screen(s)`；掃描／套用／清除真機跑過）。§6 的陷阱已增到 9 條 |
| [08_lvgl_reinit.md](08_lvgl_reinit.md) | **LVGL 軟重開機後重新初始化** —— 真根因是 C 層 root pointer 跨 soft reboot 存活；用 `lvgl_init` 的 soft-reboot 復原（硬重置）| 🟢 **已修好並真機驗收通過**（軟重開機 → 守門 → 硬重置 → UI 自己回來；防無窮重置也驗過）。根治的 C 改法見該檔 §4 |
| `_template.md` | 新清單範本 | — |

> ⚠️ **開發這幾條流程時務必 hard reset**，不要用 Ctrl-D／`mpremote` 預設的軟重開機 ——
> 它不拆 WiFi/ESP-NOW 驅動，內部 SRAM 會累積到開機 hard fault（USB-CDC 會整個消失）。
> 見 `todo/05` §12.5、changelog §32.4 / §34.5，以及 `todo/07` §6 的九個陷阱。
>
> ⚠️ **軟重開機另外還會讓 LVGL 起不來**（`allocating 3254793227 bytes`，
> 那個數字是指標不是尺寸）。**真根因已查清**（LVGL 的配置全在 GC heap 上，
> 而 binding 的 `mp_lv_roots` 是跨 soft reboot 存活的 C 全域），
> 現在由 `slave/ui/lvgl/lvgl_init.py` 的 soft-reboot 復原處理 —— 見 **`todo/08`**、
> changelog §38（§37 是已被證偽的版本）。
>
> ⚠️ **另外兩個也會靜默失敗的**：MicroPython 沒有 refcount（`open(w)` 之後不
> `close()` 就 reset，寫入會整批消失）、以及板上的檔案可能比 repo 舊
> （新 API 會 `AttributeError`，而 `try/except` 會把它變成**安靜的錯值**）。
> 見 changelog §35.7。

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
- [ ] **MCU ↔ MCU 對等傳輸**（需先補「來源位址 + 回給來源」的定址機制）— 轉送面由 [03_signal_router.md](03_signal_router.md) 接手（**程式面 P1~P5 已完成**，待 P6 上板）；跨裝置多跳環仍無防護，多跳前必須先指派 `System.cID`
- [ ] 傳輸通道：UART（RS485）/ ESP-NOW / WS 各自實測
- [ ] 不同硬體（ESP32-S3 變體、無 SD 卡的 fallback `/sd` 在 flash 上）
