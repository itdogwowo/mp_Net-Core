# 上傳大檔「驗證時重啟」追查紀錄 + 分批驗證 / 動態離線判斷改動

> **用途**：記錄「上傳大檔 data.bin 在驗證 (SHA) 階段設備重啟」的完整追查過程、
> 根因、改動清單、驗證結果，以及**尚未解決的 WS 連線抖動（ECONNABORTED loop）**。
> **分類**：筆記（03_notes）
> **最後更新**：2026-09-01
> **相關文件**：`11_firmware_update_fix.md`（前一輪固件更新修復）、`09/10_upload_performance*.md`（上傳效能）
> **狀態**：進行中——「Step 4 重新掃描」目前無法執行（見 §5）

---

## 0. 一分鐘結論

| 現象 | 根因 | 改動 | 狀態 |
|---|---|---|---|
| 上傳大檔 (45MB data.bin) 在「驗證 SHA」階段設備重啟 | 大檔整檔 SHA 在 core0 **同步阻塞 >8 秒** → TWDT 復位 (`rst:0xc`) | slave 端分批 SHA + 讓步鉤子（每 256KB 讓出控制權，啟動方依 WDT 策略餵狗） | ✅ 已上設備 |
| 設備「自己說自己是 Watchdog」、設 enable=0 沒用 | `watchdog.auto_rearm_ms=60000`：測試模式沉默 60 秒自動存 enable=1 + 重啟 | REPL 用 `cfg_manager.save_from_bus(update_key="System.watchdog")` 設 `enable=0` + `auto_rearm_ms=0` | ✅ 設備 config 已改 |
| 每一輪 `ECONNABORTED → LAN 連接成功 → DISCOVER`（無 boot banner） | master health check 誤把「正在計算 hash 的設備」標離線 → 敲門 → 設備自我斷線重連（連線抖動，非重啟） | master 端 `transfer_active` 旗標：傳輸中設備強制視為活著（動態判斷，不動 slave） | 🔧 已改未驗證 |
| Step 0 固件更新 manifest 下載失敗 → 設備跳過 | 同一顆雷：`FILE_QUERY /manifest.json` 走 realtime 全檔 hash，配合 WDT 開啟時復位 | 同分批 SHA 修復 | ✅ 同第一項 |

---

## 1. 根因鏈（完整）

```
設備 WDT 開啟 (enable=1 或測試模式 auto_rearm_ms=60000 沉默後自動翻 1)
   ↓ 上傳大檔 → master 發 FILE_QUERY / FILE_END
   ↓ slave 對 /sd/data.bin (45MB) 整檔重算 SHA256
   ↓   calc_sha256 / _finalize_atomic_write 用 2048B 迴圈讀, 全程同步阻塞 core0
   ↓   core0 runner_loop 卡在 hash 迴圈裡, 回不到頂端餵狗
   ↓ 阻塞 > 8 秒 (ESP32 WDT timeout 硬上限)
   ↓ TWDT (task_wdt: mpy_machine_wdt) 觸發 → panic abort → rst:0xc (SW_CPU_RESET)
   ↓ 重開機 → config 已 enable=1 (re-arm 存的) → WDT 常駐 → 惡性循環
```

### 關鍵機制

- **ESP32 WDT 硬上限 8 秒**：`watchdog.py` clamp `min(timeout, 8000)`。設 20 秒無效（會砍回 8 秒）。
- **`machine.WDT` 一經建立無法停**（無 deinit），軟 reset 不清除，只有斷電/硬體 reset 才清。
- **`auto_rearm_ms=60000` 是「設 enable=0 沒用」的主因**：測試模式（enable=0 且 rearm>0）開機啟動 60 秒倒數，沉默逾時 → 存 enable=1 + `machine.reset()`。要徹底停必須 `auto_rearm_ms=0`。
- **驗證本身不爆 RAM**：SHA 全是 2048B 串流（`_finalize_atomic_write`/`calc_sha256`），無 O(檔案大小) 分配。RAM 不是問題，**阻塞時間**才是。

### crash dump 特徵

```
E (209128) task_wdt: Task watchdog got triggered.
E (209128) task_wdt:  - mpy_machine_wdt (CPU 0/1)
MCAUSE : 0xdeadc0de   ← TWDT 哨兵值 (非真記憶體/匯流排錯誤)
rst:0xc (SW_CPU_RESET) ← 軟體重啟
```

---

## 2. 改動清單

### slave 端（已上設備）

| 檔案 | 改動 | 目的 |
|---|---|---|
| `slave/lib/sys/fs_manager.py` | 新增模組級讓步鉤子 `yield_point()` / `set_yield_cb()`（預設 no-op，不碰 WDT） | 分批驗證的讓步點，模組零 WDT 耦合 |
| 同上 | `_finalize_atomic_write` / `calc_sha256` 全檔 SHA 迴圈：block 2048→4096 + 每 ~256KB 呼叫 `yield_point()` | 大檔重讀分批讓出控制權，避免卡死 core0 |
| 同上 | `scan_step`（core1 背景掃描）block 2048→4096（原本已有 `sleep_ms(0)` 讓步） | 減少呼叫次數 |
| `slave/lib/sys/task_manager.py` | `runner_loop(0)` 啟動時，若 `bus.get_service("wdt")` 存在 → 注入讓步回呼（`wdt.feed()` + `sleep_ms(0)`） | 啟動方依 WDT 策略決定餵狗；WDT 沒開就是 no-op |

> ⚠️ 讓步鉤子只在「全檔 SHA 讀取」時觸發；4KB 上傳 chunk 接收路徑不受影響。

### master 端（已改，未完整驗證）

| 檔案 | 改動 | 目的 |
|---|---|---|
| `tools/PC/NetBusMaster.py` | `DeviceMonitor.last_probe_at` 欄位 | 固定週期探測基準 |
| 同上 | 設備 dict `transfer_active` 旗標（預設 False） | 動態離線判斷 |
| 同上 | `_transfer_begin()` / `_transfer_end()` 設/清 `transfer_active` | 批量上傳期間設備視為活著 |
| 同上 | `step_3_deploy` 查詢階段（`0x2005` 發出後）設 `transfer_active=True`；無設備上傳時清掉 | 大檔 SHA 驗證期間不誤標離線 |
| 同上 | `_health_check_loop`：`transfer_active=True` 的設備強制刷新 `last_update`、不標離線、不敲門 | 動態判斷「對方在計算」而非離線 |
| 同上 | `_health_check_loop` 探測改「`idle>=15s` **或** 距上次成功探測 ≥15s」固定週期探測 | 保證 master 持續餵 0x100A 刷新 slave idle |

> 改動原則（使用者要求）：**不該改 Slave，要改 Master**。Slave 是被動執行方；Master 主動動態判斷離線。中途一度在 slave 加 `_busy` 計數器已被還原（見 git）。

---

## 3. 驗證結果

### 已確認（真機 USB serial）

- 設備 = ESP32-P4（`Generic ESP32P4 module`），SD 走 SDIO slot0（`config.json` `slot:0,width:4,freq:40000000`）。
- 設備 `System.watchdog` 原本 `enable=1, auto_rearm_ms=60000`（已被 re-arm 翻成 1）。
- 用 `cfg_manager.save_from_bus(update_key="System.watchdog")` 設成 `enable=0, auto_rearm_ms=0` 後確認生效（REPL 讀回）。
- 分批 SHA 改動已上設備（boot log 正常，`[TM] Boot layer 1` 完整跑完）。
- 純被動捕捉 95s：boot banner 2 次（1 次手動 reset + 1 次 re-arm 觸發 `rst:0xc`），其餘為 `LAN 連接成功 ×8` 重連 loop——**非重啟，是連線抖動**。

### 未解決（進行中）

- **WS 連線抖動**：master 敲門（DISCOVER）→ 設備 WS 連上 → 立刻 `ECONNABORTED` → 重連，無限循環（無 boot banner）。疑似 master health check 誤判離線觸發敲門，或 slave `on_connect_request` 防抖門檻 (`ws_stale_ms=30000`) 與 master 探測間隔不匹配。
- master 端 `transfer_active` 動態判斷**尚未在真機完整驗證**。

---

## 4. 復原 / 開關

| 項目 | 做法 |
|---|---|
| 停 WDT + re-arm（設備） | REPL：`from lib.sys.watchdog import watchdog_set_enable; watchdog_set_enable(False)`，並確保 `auto_rearm_ms=0`（用 `cfg_manager.save_from_bus(update_key="System.watchdog")` 才持久） |
| 直接改 config.json | ❌ 無效：`ConfigManager` 把 config 快取在 `bus.shared`，直接 `open().write()` 會被蓋回去。必須走 `cfg_manager` API |
| 回滾 slave 分批 SHA | `git checkout slave/lib/sys/fs_manager.py slave/lib/sys/task_manager.py` |
| 回滾 master 動態判斷 | `git checkout tools/PC/NetBusMaster.py`（連同 `last_probe_at` 等一起回） |

---

## 5. 目前狀態：無法執行 Step 4 重新掃描

**現況**：設備現在無法執行 master 的「4. 重建文件索引 (Scan)」——因為：

1. master 對該台設備的連線處在**抖動循環**（連上 → ECONNABORTED → 重連），`transfer_active` 動態判斷已改但 master 需重啟才生效，且未在真機驗證。
2. 設備 config 已設 `enable=0, auto_rearm_ms=0`（WDT 與 re-arm 已停），理論上不再被 WDT 復位，但 WS 抖動仍在。
3. 待辦：重啟 master（新版）→ 確認抖動停止 → 才能正常跑 Step 4 / Step 3 部署大檔。

**下一步（接手者）**：
1. 重啟 NetBusMaster（載入 `transfer_active` / 固定週期探測改動）。
2. 真機驗證大檔部署：不再 `task_wdt` 復位、不再 ECONNABORTED loop。
3. 若抖動仍在，查 slave `on_connect_request` 防抖（`sys_actions.py:24`）`ws_stale_ms` vs master `probe_at` 是否匹配。
4. 必要時把本文件 §4 的 config 設定同步到其他設備。

---

## 6. 附：USB 驗證腳本（temp/）

| 腳本 | 用途 |
|---|---|
| `temp/usb_capture.py [sec]` | 被動收聽設備 serial（不發字元） |
| `temp/usb_repl.py` | 進 REPL 讀 `reset_cause` / watchdog config |
| `temp/usb_reset_capture.py [sec]` | REPL 重啟設備 + 統計 boot/重連/WDT dump |
| `temp/usb_set_wdt3.py` / `usb_disable_wdt.py` | 設 watchdog（後者用 ConfigManager API，正確） |
| `temp/yield_hook_test.py` | 離線驗證分批 SHA 讓步鉤子（45MB→180 讓步，SHA 不變） |
