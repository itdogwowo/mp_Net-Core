# 完整指令索引（NC4 全部指令域）

> **用途**：單一查詢表，收錄本專案全部指令域的完整指令定義。對接/新增指令前先查這裡。
> **分類**：協議層（01_protocol）
> **最後更新**：2026-08-21
> **權威來源**：`slave/schema/*.json`；本文件是整理後的說明，衝突以 schema 為準。
> **指令碼分配**：

```
0x10xx — sys         系統發現/控制/任務管理/定址/遠端更新
0x11xx — status      狀態查詢/配置更新
0x12xx — heartbeat   心跳
0x13xx — now         ESP-NOW
0x14xx — hw          硬體控制 + 臨時提速
0x15xx — waiting_to_trash  待清理功能
0x18xx — bench       性能測試（通用接收吞吐）
0x20xx — file        檔案傳輸/查詢
0x22xx — ota         韌體 OTA（合作方合同）
0x30xx — stream      pixel 串流
0x31xx — pixel      模式播放（LED/SERVO）
```

---

## 1) sys.json（0x10xx）— 系統

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1001 | DISCOVER | Server → MCU | `server_ip(str)` `ws_url(str)` | UDP 廣播發現從機 |
| 0x1002 | SLAVE_ANNOUNCE | MCU → Server | `slave_id(str)` `pixel_count(u16)` `hw_version(str)` | 從機回報身份 |
| 0x1004 | SYS_CTRL | Server → MCU | `wifi_enable(u8)` `core_control(u8)` | 系統控制 |
| 0x1005 | SYS_TASK_QUERY | Server → MCU | (空) | 查詢任務清單 |
| 0x1006 | SYS_TASK_RSP | MCU → Server | `tasks_json(str)` | 回報任務清單 |
| 0x1007 | SYS_TASK_SET | Server → MCU | `task_name(str)` `affinity_c0(u8)` `affinity_c1(u8)` | 設定任務核心親和性 |
| 0x1008 | WIFI_CTRL | Server → MCU | `wifi_enable(u8)` | WiFi 開關 |
| 0x1009 | WEB_CTRL | Server → MCU | `web_enable(u8)` | Web UI 開關（舊式、無回應，不動合同） |

### 時鐘同步（選用，併入 sys.json）

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x100A | TIME_SYNC | Master → Slave（可廣播） | `master_time_ms(u32)` | 送出當下的 Master `millis()` |
| 0x100B | TIME_SYNC_RSP | Slave → Master | `received_at_ms(u32)` | Slave 收到當下的本機 `millis()` |
| 0x100C | TIME_OFFSET_APPLY | Master → Slave | `offset_sign(u8)` `offset_ms(u32)` | 覆寫 Slave 端 offset（`offset_sign`: 0=正、1=負） |

### 定址 / 遠端更新鏈路（空編號 0x100D 起）

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x100D | IDENTIFY_REQ | Master→Slave | `reply_addr(u16)` | 逐 address 掃描；帶 reply_addr 告知 master_cid |
| 0x100E | IDENTIFY_RSP | Slave→Master | `cid(u16)` `slave_id(str)` `ip(str)` | 回應；`ip`=多介面 JSON |
| 0x100F | REBOOT | Master→Slave | `delay_ms(u32)` | 延遲後 `machine.reset()` |
| 0x1010 | WREPL_CTRL | Master→Slave | `action(u8)` 0=查 1=開 2=關 | 回 0x1011 |
| 0x1011 | WREPL_RSP | Slave→Master | `enabled(u8)` `info(str)` | WebREPL 狀態 |
| 0x1012 | NET_START | Master→Slave | `iface_type(u8)` 0=lan 1=wifi 2=ap 3=espnow | 依 config 啟動，回 0x1013 |
| 0x1013 | NET_START_RSP | Slave→Master | `ok(u8)` `iface(str)` `ip(str)` | 啟動結果 |
| 0x1014 | GET_IP | Master→Slave | (空) | 回 0x1015 |
| 0x1015 | IP_RSP | Slave→Master | `ip(str)` | `ip`=多介面 JSON |
| 0x1016 | SET_MASTER | Master→Slave | `master_cid(u16)` | 顯式設 master_cid |
| 0x1017 | WEBUI_CTRL | Master→Slave | `action(u8)` 0=查 1=開 2=關 | 回 0x1018 |
| 0x1018 | WEBUI_RSP | Slave→Master | `enabled(u8)` `info(str)` | WebUI 狀態 |

---

## 2) status.json（0x11xx）— 狀態

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1101 | STATUS_GET | Server → MCU | `query_type(u8)` | 請求狀態（0=全部，1=精簡） |
| 0x1102 | STATUS_RSP | MCU → Server | `status_json(str)` | 回傳 JSON 狀態 |
| 0x1103 | STATUS_UPDATE | Server → MCU | `config_json(str)` | 更新配置 |
| 0x1104 | STATUS_UPDATE_ACK | MCU → Server | `success(u8)` `message(str)` | 更新結果 |

---

## 3) heartbeat.json（0x12xx）— 心跳

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1201 | HEARTBEAT | MCU → Server | `slave_id(str)` `uptime_ms(u32)` `mem_free(u32)` `ws_connected(u8)` | 從機主動心跳 |
| 0x1202 | HEARTBEAT_ACK | Server → MCU | `server_time(u32)` `success(u8)` | Server 確認存活 |

---

## 4) now.json（0x13xx）— ESP-NOW

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1301 | NOW_INIT | Server → MCU | (空) | 初始化 ESP-NOW |
| 0x1302 | NOW_SEND_HB | Server → MCU | `target_mac(str)` `count(u8)` | 送心跳測試 |
| 0x1303 | NOW_STATS | Server → MCU | (空) | 查詢 ESP-NOW 統計 |

---

## 5) hw.json（0x14xx）— 硬體 + 臨時提速

### 硬體控制

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1401 | HW_CTL | Server → MCU | `type(u8)` `id(u8)` `label(str)` `value(u16)` | 硬體控制 |
| 0x1402 | HW_QUERY | Server → MCU | `type(u8)` `id(u8)` | 硬體查詢 |

### 臨時提速（協商式 UART 提速 + 超時回滾）

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1403 | SPEED_SET | M→S | `bus_type(u8)` `bus_id(u8)` `speed(u32)` `timeout_ms(u32)` | 記 old/target/timeout_at，回 0x1404 後立即切速 |
| 0x1404 | SPEED_ACK | S→M | `ok(u8)` `bus_type(u8)` `bus_id(u8)` `cur_speed(u32)` `target_speed(u32)` | 同步點（送出即切） |
| 0x1405 | SPEED_COMMIT | M→S | `bus_type(u8)` `bus_id(u8)` | 鎖定新速、取消回滾 |
| 0x1406 | SPEED_REVERT | M→S | `bus_type(u8)` `bus_id(u8)` | 還原 old_baud（config 舊速） |
| 0x1407 | SPEED_QUERY | M→S | `bus_type(u8)` `bus_id(u8)` | 查狀態，回 0x1408 |
| 0x1408 | SPEED_STATUS | S→M | `state(u8)` `bus_type(u8)` `bus_id(u8)` `cur_speed(u32)` `target_speed(u32)` `remain_ms(u32)` | 狀態回報 |

> - `state`：0=IDLE、1=SYNCING（已切、待 COMMIT）、2=COMMITTED（鎖定）。
> - `bus_type` 沿用 `hw_manager.HW` 常數：UART=7、SPI=2、I2C=3。**第一階段只實作 UART**；SPI/I2C 回 `ok=0`（not supported）。
> - `speed` 用 u32（baudrate 如 921600 超 u16）。
> - `timeout_ms` 是「沒 COMMIT 就回滾」的保險，不是 apply delay。
> - 流程細節見 `09_bus_speed_protocol.md`。

---

## 6) waiting_to_trash.json（0x15xx）— 待清理功能

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1501 | WTT_CTL | Server → MCU | `mode(u8)` `brightness(u8)` | 待清理功能控制 |
| 0x1502 | WTT_STATUS | MCU → Server | `mode(u8)` `brightness(u8)` `time(u8)` | 狀態回報 |

> `0x15xx` 命名為 waiting_to_trash：這組 cmd 碼/欄位待日後重整協議時清理（見 `slave/action/waiting_to_trash_actions.py` 註解）。

---

## 7) bench.json（0x18xx）— 性能測試

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x1811 | BENCH_READY | 發 → 收 | (空) | 清空計數器，回 0x1814 {ok:0} 證明已空 |
| 0x1812 | BENCH_DATA | 發 → 收 | `data(bytes_rest)` | 測試資料包（4KB），CRC 通過 → ok+1，不回覆 |
| 0x1813 | BENCH_RESULT | 發 → 收 | (空) | 回 0x1814 {ok:N} 統計結果，並清空計數器 |
| 0x1814 | BENCH_REPORT | 收 → 發 | `ok(u32)` | 唯一回覆指令（READY 回 ok=0、RESULT 回 ok=N） |

---

## 8) file.json（0x20xx）— 檔案傳輸

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x2001 | FILE_BEGIN | 發 → 收 | `file_id(u16)` `total_size(u32)` `chunk_size(u16)` `sha256(bytes_fixed 32)` `path(str)` | 開始傳輸；同 path+size+sha 且存在 .tmp 時自動斷點續傳 |
| 0x2002 | FILE_CHUNK | 發 → 收 | `file_id(u16)` `offset(u32)` `data(bytes_rest)` | 傳輸塊 |
| 0x2003 | FILE_END | 發 → 收 | `file_id(u16)` | 傳輸完成 → 驗 sha → 兩段式 commit |
| 0x2004 | FILE_ACK | 收 → 發 | `file_id(u16)` `offset(u32)` | 逐 chunk 確認（offset 回聲） |
| 0x2005 | FILE_QUERY | 發 → 收 | `path(str)` | 查詢檔案 |
| 0x2006 | FILE_QUERY_RSP | 收 → 發 | `exists(u8)` `sha256(bytes_fixed 32)` `size(u32)` `path(str)` `free(u32)` `pending(u8)` | 檔案資訊 + 卷剩餘空間 + 是否有待確認覆蓋 |
| 0x2007 | FILE_READ | 發 → 收 | `path(str)` `offset(u32)` `length(u16)` | 讀取檔案片段（下載） |
| 0x2008 | FILE_CONFIRM | 發 → 收 | `path(str)` | 確認覆蓋：刪 `.bak` + 清 pending delta |
| 0x2009 | FILE_DELETE | 發 → 收 | `path(str)` | 刪除檔案 |
| 0x200A | FILE_UNDO | 發 → 收 | `path(str)` | 復原覆蓋：刪新檔 + `.bak` 改回 + 清 pending delta |
| 0x200B | FILE_SCAN | 發 → 收 | `target(u8)` | 掃描檔案系統（0=本地 flash、1=SD） |
| 0x200D | FILE_MOVE | 發 → 收 | `src(str)` `dst(str)` | 通用改名/移動（走 manifest，不碰 delta；限同卷） |
| 0x200E | FILE_PARTIAL_QUERY | 發 → 收 | `path(str)` | 查詢斷點續傳進度 |
| 0x200F | FILE_PARTIAL_RSP | 收 → 發 | `partial(u8)` `written(u32)` `total_size(u32)` `sha256(bytes_fixed 32)` `path(str)` | 續傳進度（partial=1 才有效） |
| 0x2010 | FILE_ERROR_RSP | 收 → 發 | 7 個 `err_*` bool + `failed_offset(u32)` `written_up_to(u32)` `path(str)` | 失敗回覆（schema 自描述，無列舉常數） |
| 0x2011 | FILE_PROMOTE | 發 → 收 | `src(str)` `dst(str)` | 把 SD 暫存檔「交換」到根目錄正式上線（自動留 `.bak` 備份，見下方說明） |

> `FILE_ERROR_RSP` 錯誤 bool 群：`err_no_space` / `err_write_fail` / `err_offset_mismatch` / `err_id_mismatch` / `err_sha_mismatch` / `err_not_active` / `err_busy`。

> **FILE_PROMOTE（0x2011）— SD → 根目錄固件交換**：把 `src`（通常 `/sd/...` 暫存）的內容「正式上線」到 `dst`（根目錄 `/app.py` 等系統檔）。用「讀+寫+刪」三步法，跨檔案系統安全（未來接真 SD 卡、獨立掛載點也能用，不靠 `os.rename`）：
>   1. `src` 串流複製到 `dst.tmp`
>   2. 舊 `dst` → `dst.bak`（備份，舊 bak 先刪；此步失敗自動還原）
>   3. `dst.tmp` → `dst`（正式上線）
>   4. 刪 `src`（SD 暫存清除）
>   成功回 `FILE_QUERY_RSP`（`path=dst`、`exists=1`、`size`）；失敗回 `FILE_ERROR_RSP`。備份 `.bak` 保留在同目錄，供 FILE_UNDO 回滾。

> **chunk_size 參考值**：實測 sweet point = **4096**；其他 transport 參考值 RS485≈224 / I2C≈56。

> **兩段式 commit**（同名覆蓋）：FILE_END 驗 sha 通過後，先寫 `pending` delta → 舊檔改名 `.bak` → 新檔 `.tmp` 改名正式檔 → 更新 manifest。此時 `FILE_QUERY_RSP.pending=1`；直到收到 FILE_CONFIRM（刪 `.bak`）或 FILE_UNDO（復原 `.bak`）才清掉 pending。全新檔案無 `.bak`，直接單段式 rename。

> **斷線續傳**：`.tmp` 留在 SD + delta journal 記 `partial{tmp,total_size,sha256}`。重連後打 FILE_PARTIAL_QUERY 拿 `written`，從該 offset 續傳；正確性由 FILE_END 的整檔 sha256 比對保證。

---

## 9) ota.json（0x22xx）— 韌體 OTA

> 合作方合同（fastLED master_timer_slave 整合），**不動、不增減、不實作、不用**。
> 完整設計見 `03_ota_protocol.md`。

| CMD | NAME | 方向 | Payload 摘要 |
|---:|---|---|---|
| `0x2201` | `OTA_BEGIN` | Master → Slave | `image_size:u32`, `chunk_size:u16`, `sha256[32]`, `fw_ver:str` |
| `0x2202` | `OTA_WRITE` | Master → Slave | `offset:u32`, `data:bytes_rest` |
| `0x2203` | `OTA_END` | Master → Slave | — |
| `0x2204` | `OTA_ACK` | Slave → Master | `offset:u32`, `written:u32` |
| `0x2205` | `OTA_ABORT` | Master → Slave | — |
| `0x2206` | `OTA_VERSION_QUERY` | Master → Slave | — |
| `0x2207` | `OTA_VERSION_RSP` | Slave → Master | `fw_ver`, `app_sha256[32]`, `running_slot`, `running_seq`, `free_slot`, `partition_size` |
| `0x2210` | `OTA_CAPS_QUERY` | Master → Slave | — |
| `0x2211` | `OTA_CAPS_RSP` | Slave → Master | `max_chunk_size:u16` + 4 bool（secure_boot / flash_encrypt / rollback_support / diff_ota_support） |
| `0x2212` | `OTA_LAST_QUERY` | Master → Slave | — |
| `0x2213` | `OTA_LAST_RSP` | Slave → Master | 7 bool（last_ota_*）+ `last_ota_fw_ver`, `last_ota_sha256[32]` |
| `0x2214` | `OTA_PARTITION_STATUS` | Master → Slave | — |
| `0x2215` | `OTA_PARTITION_STATUS_RSP` | Slave → Master | `slot0_seq`, `slot0_valid`, `slot1_seq`, `slot1_valid`, `running_idx` |
| `0x2216` | `OTA_VERIFY` | Master → Slave | — |
| `0x2217` | `OTA_VERIFY_RSP` | Slave → Master | `verify_ok` + 3 bool（verify_fail_sha / header / crc）+ `verified_sha256[32]`, `target_slot_seq:u32` |
| `0x2218` | `OTA_PROGRESS_QUERY` | Master → Slave | — |
| `0x2219` | `OTA_PROGRESS_RSP` | Slave → Master | `image_size:u32`, `written:u32`, `target_slot:str` |
| `0x221A` | `OTA_STATE_QUERY` | Master → Slave | — |
| `0x221B` | `OTA_STATE_RSP` | Slave → Master | 4 bool（state_idle / writing / verified / error）+ `target_slot:str` |
| `0x221C` | `OTA_ERROR_RSP` | Slave → Master | 11 bool（err_*）+ `failed_offset:u32`, `written_up_to:u32`, `target_slot:str` |
| `0x2220` | `OTA_APPLY` | Master → Slave | `set_boot_only:u8`, `restart_delay_ms:u32` |

---

## 10) stream.json（0x30xx）— 像素串流

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x3001 | STREAM_INFO | MCU → Server | `total_blocks(u32)` `frames_per_block(u32)` `fps(u8)` | 串流資訊 |
| 0x3002 | STREAM_STOP | Server → MCU | (空) | 停止串流 |
| 0x3003 | STREAM_FRAME | Server → MCU | `pixel_data(bytes_rest)` | Direct Mode 直接推幀（注意：schema JSON 未定義，由 action 直接註冊） |
| 0x3004 | STREAM_SEEK | Server → MCU | `target_block(u32)` `target_frame(u32)` | 跳轉 |
| 0x3005 | STREAM_PAUSE | Server → MCU | `pause(u8)` | 暫停/恢復 |
| 0x3008 | STREAM_READY_ACK | MCU → Server | `block_id(u32)` | 準備完成 |
| 0x3009 | STREAM_STATE_SET | Server → MCU | `file_name(str)` `block_id(u32)` `play_mode(u8)` | 設定播放檔案與區塊 |
| 0x300A | STREAM_PLAY | Server → MCU | `start_frame(u32)` | 開始播放 |

---

## 11) pixel.json（0x31xx）— 模式播放

> 原 jpeg.json（0x31xx）已移除；0x31xx 域改由 pixel（模式播放）使用。權威定義見 `slave/schema/pixel.json`。
> 詳細定義見 `04_pixel_protocol.md`。
> **gmode 貫通（M5）**：`MODE_SET/MODE_STOP` 經 GlobalMode（`lib/sys/global_mode.py`）扇出 ——
> mode JSON 的 `audio` 段（tracks+limit）與燈效用同一個 `start_delay_ms` 同步起播；
> `mode_type=3` = AUDIO 組（純音效，`/audio/modes/*.json`），`MODE_LIST_QUERY` 依 id 高 byte 過濾。

| CMD | 名稱 | 方向 | Payload | 說明 |
|-----|------|------|---------|------|
| 0x3101 | MODE_LIST_QUERY | Master → MCU | `mode_type(u8)` | 查模式清單（0=全部、1=LED、2=SERVO） |
| 0x3102 | MODE_LIST_RSP | MCU → Master | `mode_type(u8)` `count(u8)` `entries(bytes_rest)` | 清單（mode_type 回音 query；entries 每筆 = 內部 16-bit 模式 id，u16 LE，見 04_pixel_protocol §2.2） |
| 0x3103 | MODE_GET | Master → MCU | (空) | 查目前狀態 |
| 0x3104 | MODE_GET_RSP | MCU → Master | `mode_type(u8)` `mode_id(u8)` `elapsed_ms(u32)` `total_ms(u32)` `running(u8)` | 目前狀態 |
| 0x3105 | MODE_SET | Master → MCU | `mode_type(u8)` `mode_id(u8)` `start_delay_ms(u16)` `brightness(u8)` | 切換模式（brightness 0–30，0xFF=不設置） |
| 0x3106 | MODE_STOP | Master → MCU | `action(u8)` | 停止（0=暫停、1=全關閉） |
| 0x3107 | MODE_DETAIL_QUERY | Master → MCU | `mode_type(u8)` `mode_id(u8)` | 查單一模式細節 |
| 0x3108 | MODE_DETAIL_RSP | MCU → Master | `mode_type(u8)` `mode_id(u8)` `total_ms(u32)` `name(str_u16len)` | 模式細節（含名稱 UTF-8） |

---

## 12) audio.json（0x32xx）— 音訊播放（WAV 串流，dj_task）

> 設計定案見 `doc/03_notes/13_audio_wav_stream_plan.md`。音檔 = PC 端預轉 WAV
> （16-bit PCM/44.1kHz/stereo，檔名自述 `name_<rate>_<bits>_<ch>.wav`）。

### 播放控制

| CMD | 名稱 | 方向 | Payload | 說明 |
|---|---|---|---|---|
| 0x3201 | AUDIO_SET | Master → MCU | `file_name(str_u16len)` `play_mode(u8)` `volume(u8)` | 準備單檔（file_name = playlist 的 name；play_mode: 0=播完停 1=循環；volume 0–100） |
| 0x3202 | AUDIO_PLAY | Master → MCU | `start_ms(u32)` | 起播；>0 = 中途加入 |
| 0x3203 | AUDIO_STOP | Master → MCU | (空) | 停止（靜音 + 釋放檔案） |
| 0x3204 | AUDIO_PAUSE | Master → MCU | `pause(u8)` | 暫停/恢復（XSMT 即時靜音） |
| 0x3205 | AUDIO_SEEK | Master → MCU | `target_ms(u32)` | 跳轉（對齊幀邊界） |
| 0x3206 | AUDIO_VOLUME | Master → MCU | `volume(u8)` | 主音量 0–100（DSP 增益於混音層應用） |
| 0x3207 | AUDIO_READY_ACK | MCU → Master | `ok(u8)` `duration_ms(u32)` | SET 驗證結果（`ok=0` = 檔不存在/不兼容/header 不符） |
| 0x3209 | AUDIO_PROGRAM_SET | Master → MCU | `tracks(bytes_rest)` | 獨立多軌節目（JSON：`{"tracks":[{file,loop,volume,start_ms}], "limit":0-100}`，與 mode JSON `audio` 段同構） |

### 播放列表管理（playlist.json 索引）

| CMD | 名稱 | 方向 | Payload | 說明 |
|---|---|---|---|---|
| 0x320A | AUDIO_LIST_QUERY | Master → MCU | (空) | 命令通道查列表（異系統相容） |
| 0x320B | AUDIO_LIST_RSP | MCU → Master | `total(u8)` `count(u8)` `entries(bytes_rest)` | 每筆 = `name(str_u16len)` + `duration_ms(u32)` + `compat(u8)`；`total>count` = 8K 截斷 → 用檔案通道拉全量 |
| 0x320C | AUDIO_LIST_RESCAN | Master → MCU | (空) | 重掃 `/sd/audio/*.wav` 重建 playlist.json（分批後台；播放中延後） |
| 0x320D | AUDIO_LIST_REMOVE | Master → MCU | `name(str_u16len)` `delete_file(u8)` | `delete_file=0` 只移索引（隱藏）；`=1` 索引+SD 檔案一起刪 |
| 0x320E | AUDIO_LIST_READY | MCU → Master | `ok(u8)` `count(u8)` | RESCAN/REMOVE 共用 ACK |

> 查詢雙通道：命令（0x320A/B，異系統）+ 檔案通道（自家 Master 用既有
> `0x2005 FILE_QUERY` 拿 sha256/size → `0x2007 FILE_READ` 分段下載
> `/sd/audio/playlist.json`，sha 沒變就不用重拉）。

---

## 相關文件

- `01_nc4_protocol.md` — 封包格式 / CRC / schema / 傳輸層
- `03_ota_protocol.md` — OTA 0x22xx 完整設計（改版理由、payload、長度限制、推薦流程）
- `04_pixel_protocol.md` — PIXEL 0x31xx 完整定義（含 mode_type 語義、byte 佔位）
- `05_integration_overview.md` — 協議整合總規格（與 master_timer_slave 的統一合約）
- `09_bus_speed_protocol.md` — 臨時提速完整工作流程（協商 / 時序 / 失敗處理 / master 整合）
