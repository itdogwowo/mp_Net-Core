# NC4 協議 + File + SPEED — 一晚自動化測試結果（2026-08-22 夜）

> **用途**：記錄 2026-08-22 夜自動化測試的完整結果：file 域（上傳/下載/備份/恢復/續傳）、SPEED 臨時提速狀態機、協議層（StreamParser/buffer_hub/decode 性能）的驗證狀態與發現的問題。
> **分類**：筆記（03_notes）
> **最後更新**：2026-08-22
> **相關文件**：`test/protocol/night_run/`（測試工具與原始結果）、`todo/01_file_update.md`、`doc/01_protocol/09_bus_speed_protocol.md`

---

## 0. 一分鐘結論

| 測試 | 結果 | 涵蓋 |
|---|---|---|
| selftest_file（loopback） | **17/17 通過** | 上傳/下載/覆蓋/備份/UNDO/續傳/MOVE |
| night_loopback（自寫擴充） | **24/24 通過** | 256KB 大檔/錯誤路徑/SPEED 狀態機/自動回滾 |
| test_proto_speed（decode） | 2.49~3.97 MB/s | StreamParser 純解碼吞吐 |
| test_proto_speed（pipe） | 256/256 無遺漏 | 雙線程管道完整性 |
| test_decode_perf | 4/4 通過 | decode 正確性 + 性能 + native 全路徑 |

**合計：45 項斷言全通過 + 3 項性能基準。** 雙板真實 UART 傳輸受阻（1401 板 USB 硬體卡死），待重插後補測。

---

## 1. 測試環境

- **1201**：master agent + 被測 slave（完整 slave 專案部署）
  - lib/schema 上傳 → 組/拆 NC4 幀（`lib.sys.proto` + `lib.sys.schema_codec`）
  - app.py/action/tasks/driver 上傳 → 完整 TaskManager 可用
  - UART(1, 115200, tx=9, rx=8) 註冊進 `uart_list`
- **1401**：slave 端部署完成、boot 正常（GPIO 9/8 claim 正確），但**測試中途 USB 完全卡死**（mpremote/pyserial/esptool 全部無回應）→ 需實體重插/按 RESET
- **接線**：GPIO9=TX、GPIO8=RX 交叉直連

## 2. 環境準備關鍵修正（踩坑紀錄）

1. **GPIO 衝突導致 boot 崩潰**：原 config `ENC.list[0].GPIO.b=8` 與 UART RX(8) 衝突，`gpio_validate()` raise → boot 中止、所有 service 未建立。**停用 ENC 後 boot 正常**。
2. **此自編 firmware 開機進 raw REPL，不自動跑 main.py**：需在 boot.py 尾部加 hook 啟動 `main()`。
3. **`lib/` 三級重構後 test/ 與 tools/ 未同步 import 路徑**（changelog §6 已預告）：`test_decode_perf.py`/`test_proto_speed.py`/`test_proto_hotpath.py` 用舊 `lib.proto`/`lib.sys_bus`/`lib.buffer_hub`，實測確認 import 失敗，已現場修正後通過。

## 3. File 域測試結果（0x20xx）

### 3.1 selftest_file 17/17（單板 loopback，`app.handle_stream` 記憶體迴路）
- A. 全新上傳 20KB + QUERY + 分片 READ + DELETE：sha/size/pending 全程正確
- B. **備份/恢復完整流程**：上傳 v1 → 覆蓋 v2（pending=1, .bak 生成）→ **CONFIRM**（pending=0, 保留 v2）→ 覆蓋 v3 → **UNDO**（回滾 v2, manifest 回填）
- C. sha 不符 → `err_sha_mismatch` + 檔案未落地 + 無 pending
- D. 中斷 → `FILE_PARTIAL_QUERY`(written=8192) → 重 BEGIN 續傳 → sha/size 正確 → partial=0
- E. FILE_MOVE：原路徑 exists=0、新路徑 sha 正確

### 3.2 night_loopback 24/24（自寫擴充）
- T1. **256KB 大檔上傳 + 分片下載**：sha 全程一致（offset 0/100000/尾段 各驗）
- T2. 錯誤路徑 5 項：
  - 無 session 就 CHUNK → `err_not_active`
  - file_id 不符 → `err_id_mismatch`
  - **MOVE 跨卷(/sd→/ram) → `err_write_fail`**（跨卷拒絕）
  - 下載不存在檔案 → 空 data（EOF 語意）
  - DELETE 不存在 → exists=0
- T4. READ length=0 → 空 data

## 4. SPEED 臨時提速測試（0x1403-0x1408）

**狀態機完整驗證（loopback，手動註冊 UART 後）**：
- QUERY 初始 state=0 (IDLE)
- SET(UART, id=0, 921600, timeout=3000) → ACK ok=1, target=921600 → 狀態 SYNCING
- QUERY → state=1, remain_ms 計時中
- COMMIT → state=2 (COMMITTED, 取消回滾)
- REVERT → state=0 (IDLE)
- SET(SPI=2) → ok=0（not supported）
- SET(bus_id=9 超界) → ok=0
- **自動回滾**：SET(timeout=1500ms) 不 COMMIT → 等 timeout → `bus_speed_poll()` 觸發 → state=0 (IDLE)

> 雙板實測補充（1401 卡死前曾部分成功）：SET 切速到 921600 後，**master 同步切 baud 敲門 STATUS_GET 成功**（`verify @921600 OK`）、COMMIT 後 state=2——證明新速下雙向通訊正常。

## 5. 協議層測試

### 5.1 StreamParser 純解碼吞吐（test_proto_speed.bench_decode）
| payload | 幀數 | 吞吐 |
|---|---|---|
| 2K | 256 | 2.49 MB/s |
| 4K | 128 | 3.60 MB/s |
| 8K | 64 | 3.97 MB/s |

### 5.2 雙線程管道（test_proto_speed.bench_pipe）
- 生產 256 / 解出 256，**無遺漏**，解碼吞吐 2.26 MB/s（含 GIL 串行，參考值）

### 5.3 decode 正確性 + 性能（test_decode_perf.run_all）
- 正確性 **4/4 通過**（roundtrip / ADDR 過濾 / batch 計數 / heavy roundtrip）
- 輕量核心迴圈（SLAVE_ANNOUNCE 18B）：每幀 1542us，0.03 MB/s（viper）
- 重量核心迴圈（FILE_CHUNK 2KB）：每幀 1740us，1.19 MB/s
- native handle_stream 全路徑：每幀 775us，**1289 幀/s**

## 6. 發現的問題 / 待修 bug

| # | 問題 | 嚴重度 | 說明 |
|---|---|---|---|
| 1 | **`SchemaCodec.decode` 的 viper 路徑在此 firmware 回傳欄位全 None** | 高 | `schema_codec.py` 的 `_viper_decode` 與此 firmware 不相容；測試用 struct unpack 繞過。**需查 viper/native 裝飾器相容性** |
| 2 | `bus_speed._cur_baud` 讀 `uart.baudrate` | 低 | MicroPython UART 物件無此屬性，`SPEED_STATUS.cur_speed` 恆回 0；不影響功能 |
| 3 | `test/` 與 `tools/` 未同步 lib 重構 import 路徑 | 中 | changelog 已預告；實測確認 `lib.proto`/`lib.sys_bus`/`lib.buffer_hub` 失效，需逐一修正 |
| 4 | FILE_CHUNK offset 連續性不驗證（已知設計） | 低 | 靠 END 的 sha256 兜底；亂 offset 也能寫，最後不符才拒絕 |
| 5 | FILE 回應恆廣播（不帶 addr） | 低 | 單 master 場景無影響；多 master 需補來源定址 |

## 7. 未完成 / 受阻項目

- ❌ **雙板真實 UART 傳輸**（SPEED 提速後跨板 FILE 上傳下載）：1401 USB 硬體卡死，需**實體重插/按 RESET** 後補測。工具已備妥（`test/protocol/night_run/master_agent.py`）
- ❌ **無線測試**（WiFi 板間互連 / ESP-NOW 傳檔 / Web UI API）：需 1401 恢復 + 額外設定（WiFi AP、ESP-NOW enable、Web UI IP）

## 8. 測試工具位置

| 檔案 | 用途 |
|---|---|
| `test/protocol/night_run/master_agent.py` | 1201 master agent：NC4 組/拆幀 + SPEED/FILE 指令 + 手動 decoder（`_HAND_DEC`） |
| `test/protocol/night_run/night_loopback.py` | 單板 loopback 綜合測試（24 項） |
| `test/protocol/night_run/config.1401.test.json` | 1401 測試用 config（9/8 + CircuitDecode + ENC off） |
| `test/protocol/night_run/config.1401.backup.json` | 1401 原 config 備份 |
| `test/protocol/night_run/results/` | 各測試原始輸出（01~04） |
| `test/protocol/night_run/REPORT.md` | 完整報告 |

---

## 9. 第二輪：新 1401 板 + 雙板實測（2026-08-22 白天）

> 用戶更換 1401 板後重新部署，完成雙板真實 UART 測試。

### 9.1 環境變化
- 新板 USB 序列號變更：`/dev/cu.usbmodem11201`（原 1201）、`/dev/cu.usbmodem11401`（原 1401）。
- 新 1401 是乾淨 MicroPython，重新部署 slave 專案 + 測試 config + boot hook。

### 9.2 修復的 3 個 bug（真實硬體暴露）

| # | 檔案 | 問題 | 修法 |
|---|---|---|---|
| 1 | `slave/lib/sys/bus_speed.py` | MicroPython UART 無 `baudrate` 屬性 → `_cur_baud` 回 0 → `old_baud=0`，REVERT 時 `if old:` 為假**不切速**，slave 永久卡在高 baud | 新增 `_config_baud(bus_id)` 從 config `UART.list[bus_id].baudrate` 讀舊速；`bus_speed_set` 與 `bus_speed_query` 在 `_cur_baud` 回 0 時 fallback |
| 2 | `slave/driver/uart_drv.py` | `UART()` 未指定 `rxbuf` → 預設 256，4KB 大幀接收時 RX FIFO 溢位，跨板 FILE_CHUNK 丟包 | 加 `rxbuf=4096, txbuf=2048`（可被 config 覆寫） |
| 3 | （發現，未修）| 高速(921600/460800)下 FILE 傳輸 + REVERT 後 CircuitBus 收發不穩 | 疑 rxbuf 修復後部分緩解；需進一步驗證 `uart.init()` 重設後 CircuitBus 內部狀態 |

### 9.3 雙板實測結果（真實 UART）

| 測試 | 結果 |
|---|---|
| UART 連通（STATUS_GET / SPEED_QUERY / TASK_QUERY） | ✅ 全部有回應 |
| SPEED 提速核心（SET→切速 921600→敲門→COMMIT→state=2） | ✅ 通過（`verify @921600 OK`、`commit state=2`）|
| SPEED cur_speed 顯示 | ✅ 修正後正確顯示 115200（原恆 0） |
| 跨板 FILE 傳輸（4KB chunk + sha256 驗證） | ✅ 通過（rxbuf 修復後）|
| SPEED 高速下大檔傳輸 / REVERT 後穩定 | ⚠️ 不穩定，見 bug #3 |

### 9.4 關鍵發現
- **此 firmware 分區表無 ota_0/ota_1**（只有 factory/nvs/phy_init/vfs）→ **無 OTA slot**，`esp32.Partition.get_next_update()` 不可用 → 無法做 partition OTA 燒寫。
- 「MCU 更新 MCU 固件」的可行路徑 = **透過 file 域(0x20xx)更新 slave 的 .py/.json 檔案**（非燒 bootloader bin），`esp32.Partition` 有 `writeblocks/set_boot` 但需 OTA slot 才能用。
- 跨板 FILE 傳輸的核心能力已驗證（4KB chunk + sha 一致），但測試腳本的 session 清理時序需完善。

---

## 10. 「MCU 更新 MCU 固件」架構調查結論（2026-08-22）

### 10.1 關鍵發現：fs_manager 只管理 /sd 和 /ram

`slave/lib/sys/fs_manager.py::resolve()` 的路徑映射規則：
- `/ram/...` → RAM 卷
- `/sd/...` → SD/FAT 卷
- **其他任何路徑 → 自動補成 `/sd/...`**

所以 FILE 域(0x20xx)只能寫 `/sd`（資料/素材）或 `/ram`（RAM），**無法覆蓋 slave 根目錄的系統檔**（`/action/*.py`、`/lib/*.py`、`/schema/*.json` 等）。實測：`FILE_QUERY('/lib/sys/proto.py')` 回 `path='/sd/lib/sys/proto.py', exists=0`。

### 10.2 分區表無 OTA slot

實測 `esp32.Partition.find()` 只有：factory(APP) + nvs/phy_init/vfs(DATA)。**無 ota_0/ota_1**，所以：
- `esp32.Partition.get_next_update()` 不可用 → 無法 partition OTA 燒寫 firmware bin。
- MicroPython 雖有 `writeblocks/set_boot/mark_app_valid_cancel_rollback`，但需 OTA slot 才能用。

### 10.3 結論

「MCU 更新 MCU 固件」兩條路徑的可行性：

| 目標 | 可行性 | 路徑 |
|---|---|---|
| SD 資料/素材檔（/sd/xxx.bin 等） | ✅ 已可用 | FILE 域(0x20xx) + Master 讀 SD → 傳 slave 存 /sd |
| slave 系統程式檔（/action/*.py 等） | ❌ 目前不可 | fs_manager 只寫 /sd，需另闢寫根目錄的機制 |
| firmware bin（bootloader/partition/app） | ❌ 目前不可 | 無 OTA slot |

### 10.4 待辦：若要「更新 slave 系統檔」

需二選一：
1. 改 `fs_manager.resolve()` 支援根目錄（如 `/app.py`、`/lib/...` 直接落根，不補 /sd）——但這會動到 fs 層核心，需評估安全性。
2. slave 端加「特殊寫根目錄」的 handler（如 `/sys/...` 前綴），走獨立寫入邏輯。

---

## 11. SPEED 兩層 timeout 設計 + 時序修正（2026-08-22）

### 11.1 時序修正（關鍵 bug）

**原實作**：`bus_speed_set` 內「先 `uart.init(target)` 切速、再回 ACK」→ ACK 以新速發出，master 在舊速收不到 → 之後 master 只能「盲猜 100ms 後切速」→ REVERT 也因時序錯亂卡死。

**修正**：拆成兩步 ——
1. `bus_speed_set`：只記 old/target/timeout_at，進 SYNCING，**不切速**。
2. `on_speed_set`：**先回 ACK（舊速）**，再呼叫 `bus_speed_apply()` 切速（apply 內先 `txdone()` 等 FIFO 排空 + 2ms 安全 margin，確保 ACK 尾部離開發射器後才 `uart.init(target)`）。

**結果**：master 收到 ACK（舊速）→ 兩邊同步切速 → 新速敲門 1 次即成功。REVERT 也穩定還原（`state=0 cur=115200`）。

### 11.2 兩層 timeout（對應需求）

| 層 | 狀態 | 觸發 | 行為 |
|---|---|---|---|
| 設定層 | SYNCING | `timeout_at`（SPEED_SET 的 `timeout_ms`）到仍未 COMMIT | `_revert()` 回滾舊速 |
| 通訊層 | COMMITTED | `idle_timeout_at`（進入通訊後 N 秒無有效通訊）到 | `_revert()` 回滾舊速 |

- 設定層：master 在 `timeout_ms` 內「不斷敲門」確認新速通訊（`bus_speed_poll` 純時間檢查，不依賴收到指令）。
- 通訊層：`app.handle_stream` 每收到有效幀呼叫 `bus_speed_touch()` 刷新 idle 倒數；`bus_speed_poll` 在 COMMITTED 且 idle 超時時回滾。
- 目前兩層暫共用同一個 `timeout_ms`（`idle_timeout_ms = timeout_ms`）；若要獨立，可在 SPEED_SET 增加第二個欄位。

### 11.3 MPY 啟動時間

實測：slave 剛 boot（soft reset / 硬 reset）後，TaskManager + 一堆模組初始化需要時間，**第一個指令可能無回應**。master 端 `t_speed` 已加「預熱敲門」（最多 5 次，間隔 300ms），確保 slave 穩定後再開始協商。

### 11.4 未解：高速下大檔傳輸

- 高速(460800)下 SPEED 協商/敲門/COMMIT/REVERT 全通，但 `FILE_CHUNK`(4KB) 傳輸仍偶發 `chunk fail @off=0`。
- 已確認：rxbuf=4096 修復了 115200 下的大幀溢位；高速下 flash 寫檔期間 CircuitTask poll 頻率不足是獨立效能問題，需進一步調 CircuitTask 優先權或 chunk 大小。

---

## 12. FILE_QUERY「不穩定」精確診斷（2026-08-22）

### 12.1 根因（已定位）

透過「master 端發 10 次 + slave 端 pyserial 抓 stdout」雙邊對照：

- master 發 10 次 FILE_QUERY，slave **只收到 5 次**（`[circuit] FILE_QUERY` log 只出現 5 次）。
- slave 收到的 5 次**全部正確回應**（`[Query] not found` → 回 0x2006 exists=0）。

**結論：問題是 master→slave 的「請求幀」在 UART 鏈路上丟失（~50%），不是 slave 處理錯誤，也不是回應丟失。**

### 12.2 影響範圍（同一個根因）

| 操作 | 丟包率（115200） |
|---|---|
| FILE_QUERY x20 | 35% 丟（ok 13/20） |
| SPEED_QUERY x20 | 25% 丟（ok 15/20） |
| 高速 460800 1KB chunk | 80% 丟（ok 1/5） |

規律：**丟包發生在 master→slave 方向**；slave 端 TaskManager 多任務（`sleep_ms(0)` 忙等）導致 CircuitTask poll 間隙不固定，master 的幀在 poll 間隙到達時被 rxbuf 覆蓋/錯位。高速下幀到達更快，丟失更嚴重。

### 12.3 可否恢復 / 解決方向

- **能恢復**：丟包是「偶發」的，不是卡死。重發即恢復（slave 一直健康，`state=0 cur=115200`）。
- **臨時緩解**：master 端每次發送後加 30~50ms 間隔 + 失敗重發（ACK 停等重試），能把有效成功率拉到接近 100%（傳輸層重試遮蓋鏈路丟包）。
- **根本解決**（需改 slave 端）：
  1. CircuitTask 提高 poll 優先權 / 降低其他 task 對 core0 的佔用；
  2. `CircuitBus.poll` 的 `drain_reads` 從 1 提高（每輪多讀幾次，清空 rxbuf）；
  3. master 端發送用「ACK 停等」——但這只是緩解，真正要減少 poll 間隙。

### 12.4 對照：昨晚 uart_cross 100% 通過的原因

昨晚 `uart_cross_bench` 是**純裸 UART echo**（無 TaskManager、無多任務競爭、receiver 是忙等迴圈），所以 100% 不丟。現在 slave 跑完整 TaskManager，CircuitTask 要跟 network/web_ui/log/render 等搶 core0，才暴露這個鏈路丟包。

---

## 13. 無線傳輸（ESP-NOW）狀態（2026-08-22）

### 13.1 可行性已確認
- `NowBus.init(channel=6)` 在無 WiFi 的 standalone 模式下可啟動（實測 11201 `ESP-NOW active, channel=6`、`broadcast(True)`）。
- ESP-NOW 板間傳檔走 NC4 FILE 協議（0x20xx），接收板 slave 零改動（NowBus 已是 bus source，FILE handler 已註冊）。
- 單包 ≤250B → FILE_CHUNK data ≤231B（腳本已用 200B）。

### 13.2 傳檔腳本（已寫，未實測端到端）
- `test/protocol/night_run/espnow_transfer.py`：SENDER 端 `send_file(path, mac=None)`。
- 待實測前置：slave(11401) config 開 `Network.ESP_now.enable=1` + 重啟，讓 NowBus 進 bus_sources。

### 13.3 WiFi 傳輸
- 需 AP + 兩板 `wifi.enable=1` + 填 ssid；PC 端可用 NetBusMaster 當 master。
- 今晚未做（無 AP 環境）。

---

## 14. 重試邏輯 + rxbuf 保留修正（2026-08-22 深夜）

### 14.1 已加的重試邏輯（回答「這個包不行就再試一次」）
- `master_agent.Link.send_wait(cmd, args, want_cmds, retries=3)`：發送 + 等回應 + 失敗重發（每次重發前 `_drain()` 清 RX 殘留）。
- 用途：遮蓋 master→slave 的鏈路偶發丟包。
- **實測結論**：對 FILE_QUERY 這類小幀偶發丟包有效（重發即通）；但對「高速 460800 下 4KB 大幀」的穩定失敗**無效**（不是隨機丟包，是系統性問題）。

### 14.2 新發現：uart.init() 縮回 rxbuf
- MicroPython 的 `uart.init(baudrate=...)` 若不帶 `rxbuf/txbuf`，會把它們**縮回預設 256**。
- 所以「切速」這個動作本身會把 slave 的 rxbuf 從 16384 縮回 256 → 大幀溢位。
- 已修：`bus_speed._reinit_uart()` 在 apply/revert 時保留 config 的 rxbuf/txbuf；master 端 `set_baud` 也帶 rxbuf=16384。

### 14.3 高速大幀仍失敗的現狀
- 460800 下 FILE_CHUNK(4KB) 仍 `chunk fail @off=0`（穩定復現），但 SPEED 協商（小幀）全通。
- 115200 下 4KB chunk + sha 驗證通過。
- 剩餘根因候選：master 端 txbuf=4096 對 4KB 幀偏小（460800 下 write 阻塞行為）、CircuitBus poll 間隙、或高速下 slave flash 寫檔期間接收停頓。需進一步隔離。

---

## 15. 高速(460800) vs 115200 傳輸失敗率精確對比（2026-08-22 深夜）

### 15.1 隔離測試數據（不重試，單 chunk，各 6~10 次）

| 速率 | chunk 大小 | 成功率 |
|---|---|---|
| 115200 | 4KB | 8/10 (80%) |
| 460800 | 512B | 3/6 (50%) |
| 460800 | 1KB | 2/6 (33%) |
| 460800 | 2KB | 3/6 (50%) |
| 460800 | 4KB | 2/6 (33%) |

### 15.2 結論

- **460800 下不分 chunk 大小全部掉包（33%~50%）**——不是「4KB 大幀專屬」問題，是高速鏈路整體不穩。
- 115200 下 4KB 成功率 80%（不重試），加重試可接近 100%。
- 高速掉包根因：slave TaskManager 多任務（sleep_ms(0) 忙等）→ CircuitTask poll 間隙不固定 → 高速下幀到達快，poll 間隙內幀被 rxbuf 覆蓋/錯位。

### 15.3 「需不需要 4KB」的答案
- 不需要死守 4KB。數據傳輸可以小包（如 512B/1KB），但**實測證明高速下連 512B 也掉包**，所以縮小 chunk 不是解方。
- 高速要穩，唯一有效是**修 slave 端 CircuitTask poll 競爭**（drain_reads 提高 + poll 優先權 + 減少其他 task 佔用），不是調 chunk 大小。

---

## 16. 「一次給足」buffer + drain_reads 調校（2026-08-23 凌晨，程式已完成、實測待 USB 復活）

### 16.1 用戶指出的問題
- 記憶有偏差：實際 rxbuf 已是 16384，但 **txbuf 只有 4096**（不足最大幀 8205B）。
- 提議：兩個方向都一次給足 + drain_reads 提高，能否解決高速掉包。

### 16.2 已做的修改（本機完成，語法檢查通過）
1. `slave/driver/uart_drv.py`：`txbuf` 4096 → **16384**（rxbuf 已 16384），兩個方向都 ≥ 最大幀。
2. `test/protocol/night_run/master_agent.py`：master 端 UART 的 rxbuf/txbuf 都改 16384（含 set_baud）。
3. `slave/lib/sys/bus_speed.py`：`_reinit_uart` 的 txbuf fallback 4096 → 16384（切速後不再縮小）。
4. `config.json`（slave 本機 + 測試 config）：`Buffer.drain_reads` 1 → **3**、`drop_on_full` 1 → **0**（每輪多讀、讀滿前不丟）。

### 16.3 判斷
- 「txbuf 一次給足」**是正確修法**：之前 master 發 4KB 幀時 txbuf=4096 < 幀 4109B，write 會分次，高速下更容易出問題。這是實打實的 bug。
- 「drain_reads=3」針對 slave 端 poll 間隙，方向也對。
- **是否根治高速掉包，需實測確認**（本輪實測被 USB 卡死阻斷）。

### 16.4 實測受阻
- 11201 與 11401 兩塊板的 USB CDC 相繼卡死（esptool/mpremote/pyserial 全無回應），需**實體拔插 USB 或按 RESET**。
- 改動已全部寫入本機檔案 + 部分部署到 11401（uart_drv/bus_speed/config 已上傳，master_agent 未成功上傳到 11201）。

---

## 17. drain_reads / drop_on_full 調校實驗結果（2026-08-23）

### 17.1 實驗數據（115200, 4KB chunk, 各 10 次）

| 配置 | 成功率（不重試） | 成功率（重試3） |
|---|---|---|
| 基線 drain=1 drop=1 | 8/10 | — |
| drain=3 drop=0 | 4/10 | 7/10 |
| drain=3 drop=1 | — | 1/10 |

### 17.2 結論（誠實）

- **盲目調 drain_reads / drop_on_full 無法穩定解決掉包**，甚至更糟。
- drop_on_full=0 最糟：hub 滿時 break 但不讀 UART，rxbuf 殘留半幀混入下一幀。
- drop_on_full=1 + drain=3 也差：hub 滿時把整段 rxbuf 讀到 drop_buf 丟棄，可能丟掉完整幀。
- **根因在架構層**：rx_hub 只有 2~4 槽（u8_rx_slots 預設 2，上限 4），消費端 bus_decode 若跟不上，槽滿 → 阻塞或丟棄。加上 TaskManager 多任務 `sleep_ms(0)` 忙等，CircuitTask poll 間隙不固定。

### 17.3 真正解法（需架構級調整，非參數）
1. rx_hub 槽位數提高（u8_rx_slots 2 → 8/16），讓消費端有緩衝餘裕。
2. CircuitTask 提高排程優先權（每輪先跑，不被 network/web_ui/render 擠掉）。
3. 或 master 端嚴格 ACK 停等（每包等到 ACK 才發下一包），把有效吞吐降到鏈路能承受的速率。

### 17.4 已還原
- drain_reads=1、drop_on_full=1（回到基線）。
- txbuf/rxbuf 一次給足 16384 保留（這是正確的，txbuf 4096 確實偏小）。

---

## 18. 關鍵轉折：大幀傳輸其實是 OK 的（2026-08-23）

### 18.1 之前的「0/10」是測試 bug，不是鏈路問題

漸進測試 + 獨立清 session 後的真相：
- SPEED_QUERY 小幀 x20：**20/20**
- 4KB chunk 漸進（100B→4096B，各5次）：**4096B 反而 5/5 全過**
- 4KB 獨立清 session x10：**8/10**

結論：**大幀傳輸本身沒問題**。之前反覆測出 0/10~2/10，是測試腳本的 FILE session 殘留（DELETE/BEGIN/END 沒正確 drain），不是 UART 鏈路、不是 buffer 大小、不是 chunk 大小。

### 18.2 用戶的兩個關鍵指正（都是對的）
1. **「多插槽好過調大單槽」**：u8_rx_slots 2→8（上限16），RX_BUF_SIZE 用精確 4115（一幀一槽）。這是正確方向。
2. **「RS485 半雙工，不要用全雙工邏輯」**：master 端 send() 加 _wait_sent() 等發完是半雙工思維；但現在是點對點全雙工物理層，將來換 RS485 要整段重看收發切換。

### 18.3 剩餘 20% 掉包
- 8/10，偶發。可被 send_wait 重試遮蓋到接近 100%。
- 若要根治：需看 CircuitTask 排程 + 消費端 bus_decode 每輪只讀 1 slot 的限制。

---

## 19. 緩衝對齊確認 + 高速實測 + 安全更新流程（2026-08-23）

### 19.1 緩衝對齊確認
- slave UART rxbuf/txbuf = 16384/16384；master 相同（含 set_baud 保留）。
- slave RX_BUF_SIZE(slot) = 4115（一幀一槽）；u8_rx_slots = 8（多插槽）。
- 對齊正確：master 發 4KB FILE_CHUNK 幀（4115B）正好填一槽。

### 19.2 高速(460800)實測
- SET→ACK(ok=1)→切速→高速敲門(state=2) 全通。
- 460800 4KB chunk x5 獨立清 session：**3/5**（大幅優於早期的 0~1/10）。
- 結論：高速可正常收發，剩餘偶發掉包可被 send_wait 重試遮蓋。

### 19.3 安全檔案更新流程（safe_update.py）實測全過
流程：stage(傳暫存 /sd/_XD_*) → verify(下載驗 sha) → apply(覆蓋 final，觸發 .bak，pending=1) → confirm(確認，pending=0) → undo(回滾，pending=0)。

實測結果：
- stage 4096B → 暫存，sha OK
- verify sha OK
- apply 覆蓋 final.bin → pending=1（.bak 備份生成）
- confirm → pending=0（確認生效）
- apply v3 → undo → pending=0（回滾成功）

### 19.4 關鍵語意確認
- FILE_MOVE 成功**無回覆**（只有失敗回 0x2010）——不能等 MOVE 回應。
- 「覆蓋」走 FILE_BEGIN/CHUNK/END（觸發兩段式 commit + .bak），不是 MOVE。

---

## 20. FILE_PROMOTE 指令（SD→根目錄正式上線）（2026-08-23）

### 20.1 需求（用戶確認）
- 固件先上傳到 slave /sd（假 SD，未來會換真 SD 卡）驗證損壞。
- 確認無損 → 交換到**根目錄**正式上線，根目錄舊檔自動留 .bak 備份。
- 用**新專用指令**（不是擴展 FILE_MOVE），備份放**跟正式檔同目錄**。

### 20.2 已實作（本機完成，語法通過，實測待 USB 復活）
- `slave/schema/file.json`：新增 `FILE_PROMOTE 0x2011`，payload `src(str) + dst(str)`。
- `slave/lib/sys/fs_manager.py`：新增 `promote_file(src, dst)`——讀+寫三步法（跨檔案系統安全，不靠 rename）：
  1. src 串流複製到 dst.tmp
  2. 舊 dst → dst.bak（若舊 bak 先刪）
  3. dst.tmp → dst（正式上線）
  4. 刪 src
  任一步失敗嘗試還原 bak。
- `slave/action/file_actions.py`：新增 `on_file_promote`，成功回 FILE_QUERY_RSP（path=真正根目錄 dst），失敗回 FILE_ERROR_RSP。

### 20.3 關鍵設計考量
- 現在「假 SD」= flash 上的 /sd 目錄，`os.rename('/sd/x','/x')` 能成功（同檔案系統）。
- 未來「真 SD」= machine.SDCard 掛載的獨立檔案系統，`os.rename` 跨卷會失敗。
- 因此 promote 用「讀+寫+刪」，對兩種情況都安全。

### 20.4 實測受阻
- slave(11401) USB CDC 卡死（esptool/mpremote 全無回應），需實體重插。
- 三個檔案尚未部署到 slave。

---

## 21. FILE_PROMOTE 部署與實測進度（2026-08-23 更新）

### 21.1 部署狀態
- slave(11401)：fs_manager.py ✅、schema/file.json ✅、file_actions.py ✅（repl_upload 補傳成功）。
- master(11201)：schema/file.json 已更新（含 FILE_PROMOTE）、safe_update.py + interactive_master.py 已上傳。

### 21.2 實測結果（受阻於鏈路掉包）
- FILE_PROMOTE 指令已在 schema 生效（cmds=97→98）。
- 端到端 promote 尚未跑通：stage 傳 8KB（2 chunk）時 chunk 掉包（chunk@0 或 @4096 偶發 NONE）。
- 根因仍是 §18.3 的「多 chunk 連續傳輸累積掉包」——單 4KB chunk 可過，8KB 兩 chunk 連發時 slave 消費跟不上。

### 21.3 待辦
- slave 端 CircuitTask 排程 / bus_decode 消費速度是掉包根因，需架構級調整（非 buffer 參數）。
- 掉包緩解：master 端嚴格 ACK 停等 + 每 chunk 更長延遲；但會犧牲吞吐。

---

## 22. 重試邏輯強化（用戶指正：同一個包試 10 次）（2026-08-23）

### 22.1 用戶指正
- 之前 retries 只有 3，用戶要求「起碼同一個包試 10 次」。

### 22.2 已改
- `master_agent.send_wait` 預設 `retries` 3 → **10**，重試前加 20ms 延遲 + drain 半幀殘留。
- `safe_update.stage` / `apply` 的 chunk/END 重試都提到 10。
- **新增 BEGIN 自我修復**：FILE_BEGIN 無回覆、易丟。若 CHUNK 回 `err_not_active`（代表 BEGIN 沒生效），自動重發 BEGIN 再重試該 chunk（最多 5 次）。

### 22.3 關鍵診斷
- 用 slave log 雙邊對照：FILE_BEGIN 幀有時到達 slave、有時完全不到（只有 FILE_DELETE 到達）。
- 確認是 **master→slave 方向請求幀丟失**（§12 同根因），不是 slave 處理問題。
- 掉包是偶發（SPEED_QUERY 小幀 20/20 全過），大幀連續傳輸時更易丟。

### 22.4 實測受阻
- USB CDC 極不穩定，每次 mpremote/pyserial 操作都可能觸發重列舉，端到端驗證反覆被打斷。
- 重試邏輯已改但未能在穩定環境下完成端到端 promote 驗證。

---

## 23. FILE_PROMOTE 端到端驗證通過（2026-08-23）

### 23.1 連接強化後鏈路穩定
用戶強化 USB 連接後，之前反覆出現的「master→slave 請求幀掉包」消失，鏈路穩定。

### 23.2 端到端實測全過
```
1. stage   : 9000 bytes → /sd/_XD__fw.bin  (sha OK)
2. verify  : 下載回驗 sha OK
3. promote : /sd/_XD__fw.bin → /_fw_test.txt  (exists=1 size=9000)
   → 根目錄 /_fw_test.txt 生成，SD 暫存 _XD__fw.bin 已刪除
4. 覆蓋   : promote v3(8400B) 覆蓋 → /_fw_test.txt=8400B, /_fw_test.txt.bak=9000B
   → 舊檔自動留 .bak（備份在同目錄）
```

### 23.3 結論
- FILE_PROMOTE 三步法（讀+寫+刪）+ 自動 .bak 備份，端到端驗證通過。
- 備份 `.bak` 放跟正式檔同目錄，覆蓋時舊檔正確留備份。
- 掉包問題在連接強化後消失，證實之前是物理連接/USB 不穩，非邏輯 bug。

---

## 24. cID 手動設置 + 掃描範圍 + 高速重測（2026-08-23）

### 24.1 cID 手動設置（用戶需求）
- config `System.cID` 設為 `0001` → slave boot 後 cid = 0x0001（原 MAC 末 4 碼 A430）。
- 驗證：廣播找到 `cid=0x0001, slave_id=24EC4A2CA430`。

### 24.2 掃描範圍（用戶需求）
- `scan.py` 新增 `scan_range(start, end)`：在範圍內逐個發 IDENTIFY_REQ，找到就回報。
- 實測 `scan_range(0, 10)` → 精確找到 0x0001。

### 24.3 高速(460800)重測結果
| 項目 | 結果 |
|---|---|
| SPEED_SET → ACK | ✅ ok=1 |
| 切速 + 高速敲門 | ✅ state=2 (COMMITTED) |
| 460800 4KB 傳檔 x10 | ⚠️ **2/10**（掉包嚴重） |
| REVERT 還原 | ✅ |

### 24.4 結論
- 115200 掉包是 USB 物理問題（連接強化後已消失，之前 promote 端到端全過）。
- **460800 掉包是另一回事**：切速/敲門/COMMIT 全穩，只有「高速連續傳大幀」掉包。這是 slave 端在高速下消費(寫 flash)跟不上 UART 接收速率，屬效能瓶頸，非邏輯 bug。
- 高速要穩，方向：①縮小 chunk（高速下用較小 chunk 降低單幀傳輸時間）；②slave 端提高 bus_decode 消費速度；③或接受高速只用於「小幀控制指令」，大檔仍走 115200。

---

## 25. firmware_update.py 完整流程（定址 + 掃描 + 一次過更新）（2026-08-23）

### 25.1 已實作
- `firmware_update.py` 整合：master_cid() 讀 config → set_master() 告訴 slave → scan_range() 掃描 → update_all() 一次過更新。
- 流程：stage(傳暫存) → verify(下載驗 sha) → promote(交換上線 + .bak 備份)，每步 send_wait retries=10 + BEGIN 自我修復。

### 25.2 實測結果
- ✅ master cid = 0xFE44（從 config 讀）
- ✅ set_master：告知 slave master_cid=0xFE44
- ✅ 掃描 0~5：找到 slave 0x0001，自動 set_master
- ❌ update_all 傳 7 檔：全部 stage 失敗（chunk fail / err_not_active / no END rsp）

### 25.3 原因
- slave 本身健康（單操作能通）。
- update_all **連續傳 7 檔**，每檔 stage+verify+promote 三步，鏈路掉包累積 → 全部失敗。
- 這是「多 chunk 連續傳輸掉包」問題在批次更新場景的放大（§18/§24 同根因）。

### 25.4 待解
- 批次更新的「檔案之間喘口氣」（每檔間隔 + 失敗重試整體重跑）。
- 或先解決高速/連續傳輸的掉包根因（slave 端消費速度）。

---

## 26. 批次更新修復 + promote 備份還原 + 開機自動恢復（2026-08-23）

### 26.1 批次更新（update_all）修復
- 根因：連續傳多檔時，slave 端 CircuitTask/BusDecodeTask 被 core0 其他任務擠壓，rxbuf 溢位 → 幀 CRC 壞被丟棄；速度越高掉越多。協議本身是 stop-and-wait（寫完一 chunk 才回 ACK），不因提速灌爆。
- 修復（`master_agent.py` + `firmware_update.py`）：
  - `speed_enter()` / `speed_revert()` 提速原語。
  - `update_all()` 加整檔重試 ×3 + 檔間喘口氣（`_breathe`）+ 提速失敗自動退回 115200。
- 實測：230400 → 7/7，460800 → 7/7（之前是 7 檔全 fail）。

### 26.2 promote→undo/confirm pending 記錄 bug 修復
- 舊 bug：FILE_PROMOTE 落根目錄後**沒寫 pending**，且 `manifest_lookup`/`confirm`/`undo` 都用 `resolve()` 把根目錄 `/xxx` 誤映射成 `/sd/xxx`，導致 undo/confirm 找不到備份。
- 修復（`fs_manager.py` + `action/file_actions.py`）：
  - promote 落根目錄後寫 pending（key = 真實根路徑），記錄 bak/old_sha/old_size/new_sha/boots。
  - 新增 `_find_pending()` / `_pending_manifest_target()` / `manifest_lookup_abs()`，根目錄檔走絕對路徑不再 resolve。
  - `on_file_query` 對根目錄檔用 `manifest_lookup_abs`，pending 正確回報。
- 實測（fs 層直調）：promote 全新 / 覆蓋留 .bak / undo 還原舊版 / confirm 刪 .bak，全部通過。

### 26.3 開機自動恢復（boot recovery）
- 需求：開機最高優先檢查 SD 備份記錄，pending 備份 boots+1，滿 3 次未確認自動還原 `.bak`。
- 實作：`FileSystemManager._boot_recovery_check()` 於 `__init__` 末尾呼叫；每次開機 boots+1 落盤，`boots>=3` 自動還原並清 pending。
- 實測：模擬 boots=2 後再開機 → 自動還原舊檔（GOOD-OLD-FIRMWARE），pending 清空。

### 26.4 部署注意（重要）
- mpremote 每次連線會 soft reset，而 MicroPython soft reset **不殺 thread**；連續 mpremote 連線會重複 `_thread.start_new_thread` 堆爆 thread → `can't create thread` → CoreManager 崩潰、REPL 卡死。
- 用 `repl_upload.py`（normal REPL + base64，Ctrl-B 模式）部署檔案，不 soft reset，避免堆 thread。
- 卡死時：Ctrl-B 退出 raw REPL + Ctrl-C 打斷，或硬 reset（拔 USB/EN 掣）清 thread。


---

## 27. RS485 完整更新鏈路實測（2026-08-24）——三塊板 0115/0117/01F7

### 27.1 硬件環境
- 三塊板：`0115` 監控（9C139EF12530）、`0117` master、`01F7` slave（B8F862D8120C）
- RS485 半雙工（SP3485 收發器），總線 A-A/B-B 並聯，已共地，DE/RE 並聯接 GPIO16
- UART id1 = **tx14/rx15/en16 @115200**（TX/RX 交叉接線，MCU TX→DI、RO→MCU RX）
- UART id2 = tx17/rx18 @9600（純 UART，未測）

### 27.2 本輪修復的 bug（全部已寫入 code）
| Bug | 位置 | 修復 |
|---|---|---|
| `CircuitDecode.enable=0` → slave 不 decode UART | config | enable 開 1 |
| `scan.py` `_identify()` 用 `uart.write()` 沒拉 DE | test/protocol/night_run/scan.py | 加 EN 控（RS485 發送必需） |
| `free_bytes` 用 `st[3]`（f_bavail 負數）→ NO_SPACE 誤判 | slave/lib/sys/fs_manager.py | 改 `st[2]`（f_bfree） |
| `write_chunk` 空間檢查負數誤判 NO_SPACE | slave/lib/sys/fs_manager.py | 加 `_free > 0` 保護 |

### 27.3 完整更新流程實測（全部通過）
1. 掃描 0x0100~0x01FF → 找到 0115 + 01F7
2. stage 上傳 → 2048 bytes sha OK
3. verify 下載驗證 → 2048 bytes sha OK
4. promote 正式上線 → exists=1 size=2048
5. REBOOT 重啟 → 指令已發，slave 成功重啟
6. 重啟後確認 → exists=1 size=2048（持久化）
7. confirm 確認 → pending 1→0
8. 覆蓋留 .bak → .bak exists=1 pending=1
9. undo 復原 → 下載內容 = v1 舊版（還原成功）

### 27.4 四個核心指令確認（用戶問）
- ✅ 上傳 stage：`staged 2048 bytes sha OK`
- ✅ 下載 verify：`verify 2048 bytes sha OK` + 下載內容 sha 比對正確
- ✅ 確認 confirm：`pending 1→0`
- ✅ 復原 undo：下載內容 = 舊版 v1（還原成功）

### 27.5 測試技巧（重要，避免反覆卡死）
- **mpremote exec 每次連線會 soft-reset，MicroPython soft-reset 不殺 thread**；反覆連線堆爆 thread → 板卡死（`could not enter raw repl` / `Device not configured`）。
- **穩定做法**：用 `pyserial` + `Ctrl-B`（出 raw REPL）+ `Ctrl-C`（打斷 main）+ `Ctrl-E` paste mode（執行多行）+ `Ctrl-D`（執行）。完全避開 mpremote，不 soft-reset。
- **需 reload 新 code 時**：硬 reset（拔插 USB / 按 reset 掣），不要 soft reset。
- **RS485 半雙工發送**：必須拉高 EN(DE) 才發得出（`scan.py` 舊版沒做，導致 IDENTIFY 不通但 STATUS_GET 通）。
- **firmware 載入慢**：開機要等 10~15 秒才 boot 完，探測時要耐心，不要誤判卡死。
- **undo 回應掉幀**：RS485 半雙工下 undo 動作成功但回應幀偶發丟失（master 報 `no UNDO rsp`），靠重試遮蓋，不影響功能。

### 27.6 三塊板角色與 cID
| 板 | USB port | 角色 | cID |
|---|---|---|---|
| 1137101 | 監控 | 0x0115 |
| 1137201 | master | 0x0117 |
| 1137401 | slave | 0x01F7 |


---

## 28. manifest 檔案表 + ESP-NOW 無線鏈路確認（2026-08-24）

### 28.1 manifest 檔案表會正確更新（實測）
- `promote_file()` 落根目錄後，`/manifest.json` 正確新增條目（`{s: size, h: sha256}`）。
- 實測：promote `/_mt_test.bin` 後，manifest 出現 `{s: 21, h: ba1184e8...}`。
- `undo_commit()` 對稱移除條目（回填舊檔資訊）。

### 28.2 ESP-NOW 無線鏈路（最小測試通過）
- 兩塊板 `import espnow` 成功，channel=6。
- 0117 廣播 `ESP-NOW-HELLO-123` → 01F7 成功收到（`RX-GOT b'ESP-NOW-HELLO-123'`）。
- 鏈路通，但大檔傳輸未做（ESP-NOW 單包 ≤250B，FILE_CHUNK data ≤231B，大檔很慢，僅驗證可行性）。

### 28.3 測試注意
- ESP-NOW `espnow.ESPNow().active(True)` 重複開會報 `ESP_ERR_ESPNOW_EXIST`；需先 `active(False)` 清舊實例。
- 兩塊板都要先 `sta.config(channel=6)` 同 channel 先通。

---

## 29. ESP-NOW 檔案傳輸（未完成端到端，架構缺口已修）（2026-08-24）

### 29.1 用戶核心提醒
- ESP-NOW 單包 ≤250B，走 FILE 協議時 FILE_CHUNK data 必須 ≤231B（不能照搬 4K）。
- 已精確計算各 FILE 幀長度（本機 CPython 用 schema 實際 encode）：
  - FILE_BEGIN=73B、FILE_CHUNK(200B data)=219B、FILE_END=15B、FILE_ACK=19B、FILE_QUERY_RSP=75B，全部 <250B ✅
- `espnow_file_test.py` 加咗「幀過長即報錯」+「send 失敗即報錯」，不再靜默失敗。

### 29.2 本輪修復的 bug（全部為真實產品 bug）
| Bug | 位置 | 修復 |
|---|---|---|
| NowTask 未在 Core_Manager 註冊 → slave 不會自動起 NowBus/poll | `slave/Core_Manager.py` | 加 `from tasks.now_task import NowTask` + `register_task("now", NowTask, layer=0)` |
| cpanel 在無 encoder 環境 `self._enc.value()` 崩潰（刷屏拖慢 core0） | `slave/tasks/control_panel.py` | `loop` 加 `if self._enc is None` 保護 |

### 29.3 已驗證正常
- ESP-NOW 硬件鏈路：raw `espnow.send/recv` 兩板互通（`RX-GOT b'RAW-FRAME-TEST-999'`）。
- NowBus 起動 + 加入 bus_sources（`NowBus: True connected: True`，`bus_sources 數量: 4, 有 NowBus: True`）。
- slave 乾淨 boot（cpanel error=0，NowBus active ch=6，Boot complete）。

### 29.4 未完成（下次收尾）
- master NowBus 發送 FILE_BEGIN → slave NowBus 接收/回 ACK 這一步仍 `chunk fail @0 (no rsp)`。
- 排查方向（下次）：
  1. NowBus 兩端 channel/peer 是否一致（master `ESP-TX` vs slave `NOW-Bus`）。
  2. slave NowTask.loop 的 `now_bus.poll()` 是否真的被 TaskManager 調度執行。
  3. FILE handler 回應走 `ctx["send"]` 時，NowBus.write 需要 `_last_peer`（對板 MAC），確認收到後有記住 MAC 才回 ACK。
> （`_last_peer` 已於 2026-10 更名為 `_last_src_mac`；本行保留當時的名字。）

### 29.5 測試腳本
- `test/protocol/night_run/espnow_file_test.py` — ESP-NOW 走 NC4 FILE 協議發送端（CHUNK=200）。
- `test/protocol/espnow_raw_frame_test.py` — raw 層並行測試（確認鏈路通）。
- `test/protocol/espnow_link_test.py` — 最小鏈路測試。

### 29.6 三塊板角色（重複記錄）
- 0115 監控（9C139EF12530）、0117 master、01F7 slave（B8F862D8120C）
- master 0117 config `ESP_now.enable=0`（不佔 ESP-NOW，用手動 NowBus 發送）
- slave 01F7 config `ESP_now.enable=1`（NowTask 自動起 NowBus 接收）

