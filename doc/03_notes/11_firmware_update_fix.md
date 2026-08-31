# 固件更新「無限還原/永遠顯示需要更新」修復 + 真機驗證

> **用途**：記錄本次對「固件全量更新」流程的完整修復、根因、改動清單，以及
> 在真機 `30EDA0E22EC0` 上的端到端驗證結果。
> **分類**：筆記（03_notes）
> **最後更新**：2026-08-31
> **相關文件**：
> - `doc/02_guides/12_network_switch_setup.md`（§7 上傳確認循環的前一次修復）
> - `doc/03_notes/01_changelog.md`（檔案流程重設計 / promote / 開機自動恢復）
> - `tools/PC/net_test_upload.py`（本次新增的純網絡上傳驗證腳本）

---

## 0. 症狀

使用者回報「step_0_update_firmware / 固件全量更新（批量上傳 slave 目錄）」：

1. **重複重複又重複無法上傳**，一直還原、一直更新不了。
2. **上傳時永遠顯示哈希表都是那些檔案需要更新**（重跑一次比對，同一批檔又跳出來）。
3. 上傳/下載時 UI 把資訊覆蓋掉，顯示「完成」其實在等 Enter。

---

## 1. 根因（三個獨立的 bug）

### 1.1 確認流程預設「暫不確認」→ 3 次重啟自動回滾 → 無限循環

`tools/PC/NetBusMaster.py` 的固件更新預設走「上傳後詢問確認」，而那個詢問視窗
預設又是「暫不確認」；上傳完又自動接軟重啟。於是一路按 Enter 就會：

```
上傳(留 .bak + pending) → 沒確認 → 重啟×3 → slave 自動回滾 .bak → 下次又顯示需要更新
```

對應 slave 端保護機制 `fs_manager._boot_recovery_check()`：pending 記錄 `boots` 每開機 +1，
滿 3 次未確認就自動把 `.bak` 蓋回舊版（這是設計行為，不是 bug）。

### 1.2 manifest 寫入沒 `os.sync()` → 軟重啟丟掉 → 哈希表永遠過期

`slave/lib/sys/fs_manager.py::_write_manifest()` 寫完 `/manifest.json`（或
`/sd/.manifest.json`）後**沒有 `os.sync()`**。在 ESP32 MicroPython 上，`close()`
只把資料從檔案 buffer 推到 VFS 區塊快取（RAM），要等 `os.sync()` 或快取被擠出
才會真正寫進 flash。`machine.reset()`（軟重啟）只清 RAM、不清 flash，所以
「寫完立刻 reset」會把還沒落盤的 manifest 寫入丟掉 → 下次比對又看到舊哈希。

> 註：若寫入與 reset 之間隔了數秒（例如有 confirm/下載等中間步驟），VFS 的
> lazy 寫入可能已自行完成，所以此 bug 不是 100% 必現——這也解釋了「有時會、
> 有時不會」。

### 1.3 ConfigManager 寫 config 沒 `os.sync()` → watchdog re-arm 無限重啟

`slave/lib/sys/ConfigManager.py` 的無損更新（`_update_value_preserve_format`）與
標準保存（`save_from_bus`）寫 config.json 後**沒有 `os.sync()`**。watchdog 的
`poll_rearm()` 在測試模式（`enable=0` + `auto_rearm_ms=60000`）沉默 60 秒後：

```
存 enable=1（無 sync）→ 立刻 machine.reset() → enable=1 被丟掉
→ 下次開機又 enable=0 → 60 秒後又 re-arm → 無限重啟循環
```

症狀是每 ~60 秒重開一次，log 結尾固定出現：

```
[Config] ✓ 無損更新成功: System.watchdog.enable
ESP-ROM:esp32p4-eco2-20240710
rst:0xc (SW_CPU_RESET)
```

---

## 2. 改動清單

| 檔案 | 改動 | 目的 |
|---|---|---|
| `slave/lib/sys/fs_manager.py` | `_write_manifest()` 落盤後加 `os.sync()` | manifest 寫入跨 reset 耐久 |
| 同上 | `_save_delta()` 落盤後加 `os.sync()` | delta(pending/partial) 跨 reset 耐久 |
| 同上 | `scan_init()` 跳過 `*.bak` | 重掃不把舊韌體備份掃進 manifest 污染 |
| `slave/lib/sys/ConfigManager.py` | `_update_value_preserve_format()` 寫完加 `os.sync()` | 無損更新 config 跨 reset 耐久 |
| 同上 | `save_from_bus()` `os.replace` 後加 `os.sync()` | 標準保存 config 跨 reset 耐久 |
| `tools/PC/NetBusMaster.py` | `_update_firmware_files()` 預設改「直接確認(auto)」 | 打斷「暫不確認→回滾」循環 |
| 同上 | `_prompt_confirm_promoted()` 預設 Enter=確認 | 同上（手動確認模式也安全） |
| 同上 | 移除 `_update_firmware_files()` 每次觸發 root 重掃 (0x200B) | manifest 是 write-through 權威表，重掃多餘且拖慢 |
| 同上 | `_confirm_path_batch`/`_undo_path_batch` 重構為 `_commit_path_batch`（ThreadPoolExecutor 每台平行） | confirm/undo 平行化，慢台不拖累全部 |
| 同上 | `_run_upload_batch()` 上傳完 `panel.stop()` + 顯示游標 | 修 UI：結果報告/確認提示不再被面板重繪覆蓋 |
| 同上 | `_bootstrap_root_fix()` 清單加 `ConfigManager.py` | 引導修復一併推 config 耐久修正 |

### 附：新增純網絡驗證腳本

`tools/PC/net_test_upload.py`：跳過 GUI，以 master 身分做 DISCOVER 敲門 →
等連線 → 上傳 → 驗 sha → CONFIRM → 下載 manifest 驗證 → （可選 `--reboot`）
軟重啟後再驗 manifest 存活。用法：

```bash
python -B tools/PC/net_test_upload.py <device_ip> <local_file> <remote_path> [--port=8005] [--reboot]
```

---

## 3. 真機驗證結果（設備 `30EDA0E22EC0`）

| 項目 | 結果 |
|---|---|
| 上傳 `fs_manager.py`（51915B） | ✅ sha `82139a16…` 一致 |
| CONFIRM | ✅ pending 1→0 |
| manifest 哈希表 | ✅ `/lib/sys/fs_manager.py` 一致 |
| 軟重啟後 manifest 存活 | ✅ 哈希仍一致 |
| 上傳 `ConfigManager.py`（25529B） | ✅ sha `69f266d7…` 一致 |
| CONFIRM | ✅ pending 1→0 |
| manifest 哈希表 | ✅ `/lib/sys/ConfigManager.py` 一致 |
| 軟重啟後 manifest 存活 | ✅ 哈希仍一致 |

結論：上傳覆蓋 → sha 驗證 → 確認清 pending → manifest 同步 → 重啟存活，
整條鏈路在真機上通過；`os.sync()` 修正生效。

---

## 4. SD（`/sd/...`）上傳的耐久性

上傳到 SD 走 **FAT**（`begin_write` 用 `open("/sd/xxx.tmp","wb")`），不是
`fast_io.Storage`（raw）那條路。耐久性三個 sync 點都覆蓋：

- `.tmp` 內容 → `_close_session()` 原本就有 `os.sync()`
- SD manifest（`/sd/.manifest.json`）→ 本次 `_write_manifest` 加的 `os.sync()`
- delta（`/sd/.delta.json`）→ 本次 `_save_delta` 加的 `os.sync()`

`fast_io.Storage`（raw，`alloc.json` 存在才啟用）是 app 資料層 `fs.write()`/
`fs.read()` 專用，直接 `writeblocks` 不經 FAT，與 upload tool 無關；本次設備
boot log 為 `FAT mode (alloc.json not found)`，屬純 FAT。

---

## 5. 下次排障速查

| 症狀 | 根因 | 修法 |
|---|---|---|
| 重跑更新同一批檔一直「需要更新」 | manifest 未落盤（寫完立刻 reset） | 確認 slave 跑的是有 `os.sync()` 的新 `fs_manager.py` |
| 每 ~60s 重開，log 見 `System.watchdog.enable` + `SW_CPU_RESET` | ConfigManager 未落盤 → re-arm 循環 | 確認 slave 跑的是有 `os.sync()` 的新 `ConfigManager.py`，或把 `auto_rearm_ms=0` / `enable=1` |
| 上傳後又被還原 | 沒 confirm（pending 留存 3 次重啟回滾） | 用新版 NetBusMaster（預設直接確認） |
| UI 顯示完成卻在等 Enter | 面板重繪覆蓋提示 | 新版 `_run_upload_batch` 上傳完會停面板 |
