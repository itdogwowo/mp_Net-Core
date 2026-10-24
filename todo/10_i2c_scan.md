# I2C 位址掃描（`_i2c_devices`）

> **用途**：追蹤「I2C 掃描 → 存 `bus.shared["_i2c_devices"]` → 本地直接讀」這條線的實測進度。
> **最後更新**：2026-10-17（v7 簡化後）
> **相關文件**：`doc/03_notes/21_hw_status_query_plan.md`（設計）、`doc/01_protocol/02_command_index.md`（0x1402/0x1409）

## 已完成（實測通過）

- [ ] 冷開機後直接讀 `bus.shared["_i2c_devices"]` → 有 `0x40`、`0x70`（**不用按任何按鈕**）
- [ ] 開機 log 只有 **一次** I2C 掃描（PCA 是讀快取，不是自己再掃）
- [ ] `0x1402{type:3,id:0xFF}` → 收到 `0x1409`，`json` = `{"0": ["0x40", "0x70", ...]}`
- [ ] LVGL 掃描鈕 → 目標可切到 `0x40`（`_pca_actions` → `_i2c_devices` 這段通了）
- [ ] 拔掉 PCA 再查 → 清單變成 `{0: []}`（不是保留舊值）

## 已完成（loopback/單元自測，非實測）

- [x] stub 整合驗證（machine/time stub）：`init_i2c` → `{0:[0x40,0x70]}`／掃描失敗記 `[]` 且 bus 仍建立／
      `I2C.enable=0` 不掃／`on_hw_query` 外部回 41 B 封包、內部呼叫不送包但更新快取

## 待跟進（實測）

- [ ] 開機時把 I2C 拉死（短路 SDA）→ 裝置**仍能開機**、`_i2c_devices == {0: []}`
- [ ] 走 ESP-NOW 送 `0x1402{type:3}` → 正常（封包只 41 B，遠低於上限）
- [ ] 多 I2C 匯流排的板子（N16R8-V1.0 / Touch-LCD-2.8）：確認 `{0: [...], 1: [...]}` 分開

## 已知問題 / 待決

- [ ] **熱插拔不重建（播放端）**：查詢會更新 `_i2c_devices`，但 `pca9685_list` 的
  controller 不會重新註冊 → 自動播放仍需要重開機（reinit 未實作）。
  UI 的 `pca9685_ctl` 會在掃描後由 `refresh_pca_ctl()` 更新，手動控制不用重開機。
- [x] ~~`_pca_actions` 的 `start` / `stop`~~ → 2026-10 頁面改版：不再送 `start`/`stop`，
  改成直接操作 `pca9685_ctl`（單板/廣播都同一個入口）。`_pca_actions` 現在只剩 `scan`。
- [ ] `I2C.scan()` 無法中途打斷 → 單一死線最壞 ~5.6s（< 8s WDT，會卡住但不會重開機）
- [ ] **沒有離線單元測試**（v7 隨 `lib/sys/i2c_scan.py` 一起刪除）；要驗就在 REPL 直接呼叫

## 筆記

- 掃描函式只有一支：`driver/i2c_drv.scan_devices()`（12 行）。
  開機時 `init_i2c()` 尾端呼叫一次；查詢時 `hw_actions._query_i2c()` 再呼叫一次；UI 按鈕透過
  `_pca_actions` → `pixel_control_panel` 呼叫同一支。
- 開機 log：`[I2C] devices: {0: ['0x40', '0x70']}`（印在 `init_i2c` 裡）。
- `_i2c_devices` 是 **per-bus**：`pca9685_drv` 用 `devs.get(i2c_idx)` 才建對匯流排的 controller。
  UI 已**不再自己攤平**：改讀 driver 建的 `pca9685_ctl`（每筆 = 一個控制目標，
  含 `broadcast` / `boards` / `online`），廣播永遠排第一。
- `pca9685_ctl` 與 `pca9685_list` 分開：前者給 UI 手動控制（原始 PCA9685），
  後者給 PixelStreamer 播放（不要讓播放端每幀多寫一筆）。
- 掃描後 `pixel_control_panel` 會呼叫 `refresh_pca_ctl()`：熱插拔的板子可以
  手動控制，但**不會**進播放端 `pca9685_list`（重建 controller 仍是未實作）。
- 位址是 **int**（`0x40`）；`0x1409` 的 JSON 才轉成 `"0x40"`。
- **空 ≠ 壞**：`{0: []}` 代表「掃過、沒有裝置 ACK」。先在 REPL 用 `i2c_list[0].scan()` 對照，
  兩邊都是 `[]` 就是硬體/接線（上拉、供電、SDA/SCL 接反），不是程式。
