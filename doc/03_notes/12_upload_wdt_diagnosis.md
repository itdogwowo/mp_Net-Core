# 上傳大檔「驗證時重啟」追查紀錄 + 分批驗證 / 動態離線判斷改動

> **用途**：記錄「上傳大檔 data.bin 在驗證 (SHA) 階段設備重啟」的完整追查過程、
> 根因、改動清單、驗證結果，以及**尚未解決的 WS 連線抖動（ECONNABORTED loop）**。
> **分類**：筆記（03_notes）
> **最後更新**：2026-09-02
> **相關文件**：`11_firmware_update_fix.md`（前一輪固件更新修復）、`09/10_upload_performance*.md`（上傳效能）
> **狀態**：已改——master 自動敲門重連 + 定時 health 檢查已移除、Scan 重啟已修（見 §2/§5/§6），待真機驗證

---

## 0. 一分鐘結論

| 現象 | 根因 | 改動 | 狀態 |
|---|---|---|---|
| 上傳大檔 (45MB data.bin) 在「驗證 SHA」階段設備重啟 | 大檔整檔 SHA 在 core0 **同步阻塞 >8 秒** → TWDT 復位 (`rst:0xc`) | slave 端分批 SHA + 讓步鉤子（每 256KB 讓出控制權，啟動方依 WDT 策略餵狗） | ✅ 已上設備 |
| 設備「自己說自己是 Watchdog」、設 enable=0 沒用 | `watchdog.auto_rearm_ms=60000`：測試模式沉默 60 秒自動存 enable=1 + 重啟 | REPL 用 `cfg_manager.save_from_bus(update_key="System.watchdog")` 設 `enable=0` + `auto_rearm_ms=0` | ✅ 設備 config 已改 |
| 每一輪 `ECONNABORTED → LAN 連接成功 → DISCOVER`（無 boot banner） | master health check 誤把「正在計算 hash 的設備」標離線 → 敲門 → 設備自我斷線重連（連線抖動，非重啟） | 第一版先加 `transfer_active` 動態判斷；2026-09-02 最終方案：**移除整個定時 health 檢查**，離線改由 WS 通道本身判定（見 §2） | ✅ 已改（2026-09-02），待真機驗證 |
| master 離線判斷後「自動敲門叫回」在離線/無響應期間每 10s 一直發 DISCOVER → slave 端 `on_connect_request` 依 `ws_stale_ms` 自我斷線重連 → 抖動循環 | master 不該主動發起重連（使用者要求） | **移除 master 全部自動重連路徑**：健康檢查自動敲門（`_knock_offline_devices`/`_knock_ip`/`reconnect_knock_interval_s`）與啟動時自動敲門（`main_loop`）全刪；重連一律由操作者手動「選單 1 掃描/敲門」發起 | ✅ 已改（2026-09-02），待真機驗證 |
| 「4. 重建文件索引 (Scan)」發起後冇反應 | FsScanTask one-shot：掃完 `_shutdown()` 設 affinity `(0,0)` 停咗自己；0x200B 只設旗標冇人消費 | `scan_all()` 重新武裝 affinity `(0,1)` + master `_scan_files` 輪詢 `fs_scan_busy` 逐台回報 | ✅ 已改（2026-09-02），待真機驗證 |
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
| `slave/lib/sys/fs_manager.py`（2026-09-02） | `scan_all()`：設 `fs_scan_requested=True` 後把 `fs_scan` 任務 affinity 重新武裝回 `(0,1)`（core1） | 修 0x200B 重掃後冇反應：FsScanTask 完成一次掃描會 `_shutdown()` 把自己 affinity 設 `(0,0)` 停掉（one-shot），之前收到 0x200B 只設旗標、任務已停 → 旗標無人消費。詳見 §6 |
| `slave/action/file_actions.py`（2026-09-02） | `on_file_delete` 特例：剷 `/manifest.json` → 唔回覆、`[FileScan]` log、`machine.reset()` | 「4. 重建文件索引」改用「剷除→重啟→開機重掃」；WS 斷線 = 已執行，唔加新指令 |
| `slave/lib/sys/fs_manager.py`（2026-09-02） | `scan_sd()`：置 `fs_scan_sd_busy` 旗標 + `[FileScan]` log + 每檔 `sleep_ms(0)` 讓步 | SD 主動掃描有得確認（busy 旗標）；render（core1）唔會被霸死 |
| `slave/action/status_actions.py`（2026-09-02） | `fs_scan_busy` provider 覆蓋 local（`fs_scan_requested`）與 SD（`fs_scan_sd_busy`） | master 一個旗標睇晒兩種掃描 |

> ⚠️ 讓步鉤子只在「全檔 SHA 讀取」時觸發；4KB 上傳 chunk 接收路徑不受影響。

### master 端（已改，未完整驗證）

| 檔案 | 改動 | 目的 |
|---|---|---|
| `tools/PC/NetBusMaster.py`（2026-09-02） | **移除全部自動重連路徑**：`_knock_offline_devices()`、`_knock_ip()`、`_knock_last`/`_offline_knocked` 狀態、config `reconnect_knock_interval_s`，以及 `main_loop` 啟動時的自動敲門 | master 不主動發起重連（使用者要求：重連一律人手發起）；操作者用「選單 1 掃描/敲門」手動叫回 |
| 同上（2026-09-02） | **移除整個主動健康檢查**：`_health_check_loop` / `_probe_device` / health 執行緒 / `DeviceMonitor.last_probe_at` / `transfer_active` 旗標全部刪除 | 不再定時 ping (0x100A) 探測、不再「30s 無流量標無響應」——連線狀態以 WS 通道本身為準 |
| 同上（2026-09-02） | 連線狀態 = WS 通道事件：`handle_client` recv 收到 FIN/RST/錯誤 → finally → `unregister_connection` → 標離線 + panel log「📴 離線」；`send_pkt` 發送失敗（RST/EPIPE/半開重傳超時）→ 關 socket 觸發同一條清理路徑 | 「有沒有回應」不再是判斷依據；頻繁 health 檢查完全消失 |
| 同上（2026-09-02） | `handle_client` TCP keepalive 補 Windows 分支：`SIO_KEEPALIVE_VALS`（idle 10s / 每 3s 探） | 半開連線（對面靜默消失、無 FIN/RST）由**通道本身**在 ~20s 內偵測到，recv 拋錯 → 離線 |
| 同上（2026-09-02） | `_scan_files`（Step 0 → 4 重建文件索引）拆三個範圍：1=本地（0x2009 剷 `/manifest.json` → 等 WS 斷線 → 等回線 → 輪詢 `fs_scan_busy`）/ 2=SD（0x200B target=1 → busy=1 確認開始 → busy=0 確認完成）/ 3=兩樣 | 唔加新指令，重用舊指令 + 通道斷線/busy 旗標做確認；兩種索引重建都有明確回報 |
| `tools/WebMaster/device_manager.py`（2026-09-02） | `heartbeat_loop` 不再每 2s 送 0x1101 STATUS_GET、不再「30s 無回應標離線」；只保留 device_list UI 廣播 | 同一原則：WebMaster 也不頻繁 health 檢查；離線 = WS 斷線事件（`/ws/{slave_id}` finally） |

> 改動原則（使用者要求）：**不該改 Slave，要改 Master**。Slave 是被動執行方；Master 主動動態判斷離線。中途一度在 slave 加 `_busy` 計數器已被還原（見 git）。
>
> 2026-09-02 追加原則（使用者）：master **連重連也不主動發起**（DISCOVER 敲門 = 叫 slave 重連的動作）。
>
> 2026-09-02 再追加（使用者）：**連線存活判斷走 WS 通道本身的連接狀態**（TCP FIN/RST/send 錯誤/
> keepalive），不做「回應不回應」的定時 health 檢查。檢查連線的時機只有：
> (1) 操作者手動執行的動作（查狀態 0x1101、量延遲 0x100A、掃描驗證…）；
> (2) 播放途中的進度輪詢（0x1101 每秒，播放中自然附帶）。

---

## 3. 驗證結果

### 已確認（真機 USB serial）

- 設備 = ESP32-P4（`Generic ESP32P4 module`），SD 走 SDIO slot0（`config.json` `slot:0,width:4,freq:40000000`）。
- 設備 `System.watchdog` 原本 `enable=1, auto_rearm_ms=60000`（已被 re-arm 翻成 1）。
- 用 `cfg_manager.save_from_bus(update_key="System.watchdog")` 設成 `enable=0, auto_rearm_ms=0` 後確認生效（REPL 讀回）。
- 分批 SHA 改動已上設備（boot log 正常，`[TM] Boot layer 1` 完整跑完）。
- 純被動捕捉 95s：boot banner 2 次（1 次手動 reset + 1 次 re-arm 觸發 `rst:0xc`），其餘為 `LAN 連接成功 ×8` 重連 loop——**非重啟，是連線抖動**。

### 未解決（進行中）

- **WS 連線抖動**：master 敲門（DISCOVER）→ 設備 WS 連上 → 立刻 `ECONNABORTED` → 重連，無限循環（無 boot banner）。根因指向 master 離線判斷後的自動敲門：離線/無響應期間每 10s 持續發 DISCOVER，slave `on_connect_request` 的防抖門檻 (`ws_stale_ms`) 與之形成「敲門 → 自我斷線重連」循環。**2026-09-02 已移除 master 全部自動敲門路徑 + 全部定時 health 檢查（見 §2），待真機驗證。**
- **半開連線的機制說明（使用者補充）**：半開 WS（兩端都以為還連著）→ slave 唔放新連線；後來 slave 加咗防抖門檻（`on_connect_request`：連住同一 URL 但近 `ws_stale_ms` 冇流量 → 放行斷線重連）→ 所以 master 先會見到「不斷重新連接 WS」。master 停止自動敲門後，呢個門檻只會喺**手動**敲門時先會行到，唔會再自己循環。

---

## 4. 復原 / 開關

| 項目 | 做法 |
|---|---|
| 停 WDT + re-arm（設備） | REPL：`from lib.sys.watchdog import watchdog_set_enable; watchdog_set_enable(False)`，並確保 `auto_rearm_ms=0`（用 `cfg_manager.save_from_bus(update_key="System.watchdog")` 才持久） |
| 直接改 config.json | ❌ 無效：`ConfigManager` 把 config 快取在 `bus.shared`，直接 `open().write()` 會被蓋回去。必須走 `cfg_manager` API |
| 回滾 slave 分批 SHA | `git checkout slave/lib/sys/fs_manager.py slave/lib/sys/task_manager.py` |
| 回滾 master 動態判斷 | `git checkout tools/PC/NetBusMaster.py`（連同 `last_probe_at` 等一起回） |

---

## 5. 目前狀態：master 自動重連 + 定時 health 檢查已移除（2026-09-02），待真機驗證

**現況**：

1. master 端所有「自動發起重連」與「定時 health 檢查」路徑已移除：不再自動敲門、不再定時 ping (0x100A)、不再「無響應」判定。離線 = WS 通道斷線事件（recv 錯誤/FIN/keepalive 偵測半開 → `unregister_connection`），面板 log 會印「📴 離線」。
2. 設備 config 已設 `enable=0, auto_rearm_ms=0`（WDT 與 re-arm 已停），理論上不再被 WDT 復位。
3. 手動叫回路徑不變：選單 `1. Scan Devices` → 1=廣播掃描 / 2=定向 IP / 3=依紀錄敲門（`_knock_recorded_devices`）；「重試上傳失敗檔案」選項也會先敲門（這是使用者主動操作）。

**下一步（接手者）**：
1. 重啟 NetBusMaster（載入無自動敲門 + 無 health 檢查 + keepalive + 掃描驗證改動）。
2. 真機驗證：設備離線後 master 只 log 標記、不再發任何自動封包；手動「掃描/敲門」能把設備叫回。
3. 真機驗證大檔部署：不再 `task_wdt` 復位、不再 ECONNABORTED loop。
4. 真機驗證「Step 0 → 4 重建文件索引」：應逐台回報「重建完成」（slave 端需已更新 §6 的 re-arm 修正）。
5. 若設備仍自己斷線重連，查 slave 端 `on_connect_request` 防抖（`sys_actions.py:24`）——那是 slave 側行為，需另案處理。
6. 必要時把本文件 §4 的 config 設定同步到其他設備。

---

## 6. 「4. 重建文件索引 (Scan)」追查：不會觸發看門狗；真正問題是任務唔識重啟

### 6.1 看門狗結論：Scan 唔會觸發 WDT

- `fs_scan` 任務註冊在 **core1**（`Core_Manager.py`：`default_affinity=(0,1)`），
  唔喺 core0 主線程。
- `scan_step()` 每次 loop 只 hash **一個檔案**，每 ~256KB 讓步一次
  （`time.sleep_ms(0)` + `engine_run` abort 檢查），唔會長阻塞。
- WDT 由 core0 `runner_loop(0)` 餵（watchdog v5 設計）——core1 掃描唔會頂到
  餵狗。加上設備 config 已 `enable=0`（冇狗），更加冇事。
- 唯一同步路徑係 `target=1`（SD 掃描）：`on_file_scan` 用 `_thread.start_new_thread`
  開線程行 `fs.scan_sd()`，一樣唔喺 core0。所以「Scan 會唔會觸發看門狗」→ **唔會**。

### 6.2 「無法發起」真正原因：FsScanTask 係 one-shot，做完自己熄咗

```
開機(manifest 缺)或第一次 0x200B → fs_scan_requested=True
  → FsScanTask(core1) 掃完 → finalize_scan → _shutdown()
  → set_affinity("fs_scan", (0,0))   ← 任務被 TaskManager 停咗 (両核都唔跑)
之後再按「4. 重建文件索引」→ on_file_scan 只設 fs_scan_requested=True
  → 冇人重新武裝任務 → 旗標永遠冇人消費 → 重建冇反應 (master 見到「指令已發送」
    但 manifest 一啲都冇變)
```

### 6.3 最終方案（2026-09-02）：「剷除 → 自己重啟 → 開機重掃」，唔加新指令

- **本地 flash（/manifest.json）**：
  1. master 送 `0x2009 FILE_DELETE {path:/manifest.json}`；
  2. slave `on_file_delete` 特例：剷走 `/manifest.json` 後**唔回覆**、
     `get_log` 打 `[FileScan]` 標籤、即刻 `machine.reset()`；
  3. **master 見到 WS 斷線 = 已執行**（「通道斷線」本身做確認，
     唔使 0x2004/0x2006，0x2004 係 chunk ACK 語意唔啱）；
  4. 開機 detect 到 manifest 缺失 → 自動背景重掃（core1）→ master 等設備
     回線後輪詢 `fs_scan_busy` 歸零 → 「✅ 文件索引重建完成」。
- **SD（/sd/.manifest.json）**：
  - 設計：SD manifest **平時 delta 維護**（協議上傳/下載先紀錄），唔主動掃；
    只有 `0x200B(target=1)` 主動掃描先重建自己張表。
  - 修正：`fs_manager.scan_sd()` 置 `bus.shared["fs_scan_sd_busy"]=1`（finally
    清零）+ `[FileScan]` log + 每檔 `sleep_ms(0)` 讓步（render 同喺 core1）；
    `status_actions` 嘅 `fs_scan_busy` provider 改為 local/SD 任一 busy 都報 1。
  - master `_scan_files_sd()`：送 0x200B(target=1) → 等 busy=1（確認開始，
    舊韌體冇旗標會提示）→ 等 busy=0 → 「✅ SD 表重建完成」。
- master 選單「4. 重建文件索引」而家有三個範圍：1=本地 / 2=SD / 3=兩樣
  （先 SD 後本地，因本地會重啟設備）。
- 本地流程同時避開 FsScanTask one-shot 問題：重啟後任務由 boot 重新註冊，
  affinity 天然係 `(0,1)`。`scan_all()` 仍保留重新武裝 affinity 嘅修正
  （0x200B console 手動重掃 local 嗰陣用）。
- 舊韌體兼容：冇 self-reset 特例嘅韌體會照舊回 0x2006 且唔重啟 →
  master「10s 內未見斷線」會提示，之後照等上線 + 狀態輪詢確認。

> 回答「收到回覆後係重啟定係真係執行?」：而家根本**唔使等回覆**——
> WS 斷線 = 設備已剷除並重啟（冇執行就唔會斷線）；重新上線 = 開機重掃中，
> `fs_scan_busy` 歸零 = 真係掃完。

### 6.4 後備手段（舊韌體手動版）

舊韌體未有 §6.3 嘅 self-reset 特例時：用 master「Step 0 → 3 刪除文件」刪走
`/manifest.json`（SD 嘅話係 `/sd/.manifest.json`）→ 「Step 0 → 7 軟重啟設備」
→ 開機 detect 到 manifest 缺失會自動起背景掃描重建。

---

## 7. 附：USB 驗證腳本（temp/）

| 腳本 | 用途 |
|---|---|
| `temp/usb_capture.py [sec]` | 被動收聽設備 serial（不發字元） |
| `temp/usb_repl.py` | 進 REPL 讀 `reset_cause` / watchdog config |
| `temp/usb_reset_capture.py [sec]` | REPL 重啟設備 + 統計 boot/重連/WDT dump |
| `temp/usb_set_wdt3.py` / `usb_disable_wdt.py` | 設 watchdog（後者用 ConfigManager API，正確） |
| `temp/yield_hook_test.py` | 離線驗證分批 SHA 讓步鉤子（45MB→180 讓步，SHA 不變） |
