# 協議合併對照 — master_timer_slave vs mp_Net-Core

> **用途**：對比「外部系統（master_timer_slave）」與「本專案（mp_Net-Core）」兩套協議，
> 釐清哪些功能是對方有、我們沒有；哪些指令我們有、但缺參數；以及哪些參數語義衝突。
> **分類**：協議層（01_protocol）
> **整理時間**：2026-08-18

---

## 0. 一分鐘結論

兩套系統的**角色架構根本不同**，但有一條 **UART 5-byte 幀 `[0xB4][b1][b2][b3][0xFF]` 幾乎一樣**，
這是合併衝突的核心。兩邊的指令也都用「schema JSON（cmd 碼 + payload 欄位清單）」描述，
差別只在：對方是 build-time 生成 C++ header，我們是 runtime 直接讀 JSON。

| 維度 | 外部系統 (master_timer_slave) | 本專案 (mp_Net-Core) |
|---|---|---|
| 角色 | **Timer（面板）/ Master（指揮）/ Slave 1–20（執行）** 三層 | 單一 codebase，「面板」與「執行」兩種角色，靠 ESP-NOW 互連 + Server 直連 |
| 面板 → 指揮 | UART 固定 5-byte | UART 5-byte + ESP-NOW `0x1501/0x1502` |
| 指揮 → 執行 | RS485（UART 460800）或 I2C，二進位 frame | NC4 二進位封包，5 種 transport（WS/UDP/TCP/UART/ESP-NOW） |
| Slave 職責 | 燈光 + 馬達（servo）動作 | 燈光（pixel/stream/jpeg）+ 馬達（GPIO） |

---

## 1. 兩邊的「指令描述格式」對照

兩邊都把指令描述成結構化資料，思路相同，但落地方式不同。

| 維度 | 外部系統 | 本專案 |
|---|---|---|
| schema 來源檔 | `firmware/shared/schema/slave_uart_commands.json` | `slave/schema/*.json`（10 個檔） |
| 誰讀 JSON | **ESP32 runtime 不讀**，只 include 生成檔 | runtime 由 `slave/lib/sys/schema_loader.py` 載入 |
| 生成流程 | `scripts/generate_slave_uart_schema.py` → 生成 `slave_uart_schema_generated.h` | 無 codegen，直接 load |
| 防 drift | `--check` 檢查生成檔有無落後 | 無（且實測已有 drift，見 §2 註記） |
| payload 型別 | `u8/u32/i32/bytes_rest`（little-endian） | `u8/u16/u32/str_u16len/bytes_fixed/bytes_rest` |

> 公平註記：本專案也有 drift — 舊文件曾記載 `jpeg.json(0x31xx)`，
> 但 `slave/schema/` 目錄內沒有 jpeg.json，且 `slave/tasks/lvgl_task.py` 註明
> 「jpeg 播放器已移除」；0x31xx 域已改由 pixel（模式播放）使用。文檔與實作對不上這件事，兩邊都發生過。

---

## 2. 封包格式對照

### 2.1 對方：Master ↔ Slave RS485 frame

```text
[0x55 × 10] [GUARD 0x02 0xFF]
[STX 0x02] [VER 0x01] [ADDR] [CMD] [SEQ_LO] [SEQ_HI] [LEN] [PAYLOAD] [CRC32_LE] [ETX 0x03]
```

| Offset | 欄位 | 長度 | 說明 |
|---:|---|---:|---|
| 0 | STX | 1 | `0x02` frame 開始 |
| 1 | VER | 1 | `0x01` |
| 2 | ADDR | 1 | `0=broadcast`；`1..20=Slave ID` |
| 3 | CMD | 1 | command ID |
| 4–5 | SEQ | 2 | transaction sequence（u16 LE） |
| 6 | LEN | 1 | payload 長度 0..240 |
| 7..7+L-1 | PAYLOAD | L | 各 CMD 定義，little-endian |
| 7+L..10+L | CRC32_LE | 4 | 由 VER 至 payload 最後一 byte |
| 11+L | ETX | 1 | `0x03` frame 結束 |

- CRC32 = IEEE reflected，poly `0xEDB88320`，init/final xor `0xFFFFFFFF`。
- 傳輸 460800 baud、8-N-1、half-duplex；`ADDR=0` 為 broadcast，Slave 不回覆。

### 2.2 本專案：NC4 封包

```text
[SOF "NC" 2B] [VER 1B] [ADDR 2B] [CMD 2B] [LEN 2B] [DATA LEN B] [CRC32 4B]
```

| 欄位 | 長度 | 說明 |
|---|---|---|
| SOF | 2 | 固定 `b"NC"`（0x4E43） |
| VER | 1 | 固定 `4` |
| ADDR | 2 | 目的地址（uint16 LE，`0xFFFF`=廣播） |
| CMD | 2 | 指令碼（uint16 LE） |
| LEN | 2 | DATA 長度（uint16 LE） |
| DATA | LEN | 由 schema 定義 |
| CRC32 | 4 | `binascii.crc32`，範圍 = VER..DATA（不含 SOF、不含 CRC 自身） |

---

## 3. 指令目錄完整對照（核心）

把對方 RS485 的完整 command catalog 逐條對到本專案 NC4。

| 對方 CMD | 名稱 | 對方 payload（LE） | 本專案對應 | 本專案 payload | 狀態 |
|---:|---|---|---|---|---|
| `0x00` | UNKNOWN | 空（reserved） | — | — | — |
| `0x01` | MODE_SET | `mode_id:u8`, `master_start_ms:u32` | 無 | — | ❌ 缺 |
| `0x02` | MODE_NEXT | `mode_id:u8`, `master_start_ms:u32` | 無 | — | ❌ 缺 |
| `0x03` | MODE_STOP | 空 | `STREAM_STOP 0x3002` | 空 | 🟡 只停串流，不停馬達 |
| `0x04` | BRIGHTNESS | `value:u8`（1..190） | `WTT_CTL 0x1501`（含 brightness） | `mode:u8`,`brightness:u8`（0..36） | 🟡 範圍/單位不同 |
| `0x05` | STORY_SET | `set_type:u8`（0=LED,1=SERVO） | 無 | — | ❌ 缺 |
| `0x06` | POWER_OFF | 空 | 無 | — | ❌ 缺 |
| `0x07` | POWER_ON | 空 | 無 | — | ❌ 缺 |
| `0x08` | AUDIO_LEVEL | `level:u8`（0..255）,`beat:u8`（0/1） | 無 | — | ❌ 缺 |
| `0x09` | AUDIO_ACTIVE | `active:u8` | 無 | — | ❌ 缺 |
| `0x10` | STATUS_QUERY | 空 | `STATUS_GET 0x1101` | `query_type:u8` | 🟡 回傳語義不同 |
| `0x11` | STATUS_REPORT | `state:u8`,`reported_mode_id:u8` | `STATUS_RSP 0x1102` | `status_json:str` | 🟡 無 state 機語義 |
| `0x12` | PROBE | 空 | `DISCOVER 0x1001` / `SLAVE_ANNOUNCE 0x1002` | 見 schema | ✅ |
| `0x13` | PROBE_REPLY | 空 | `SLAVE_ANNOUNCE 0x1002` | `slave_id`,`pixel_count`,`hw_version` | ✅ |
| `0x20` | TIME_SYNC_REQUEST | `time_ms:u32` | 無 | — | ❌ 缺 |
| `0x21` | TIME_SYNC_REPLY | `received_at_ms:u32` | 無 | — | ❌ 缺 |
| `0x22` | TIME_OFFSET_APPLY | `offset_ms:i32` | 無 | — | ❌ 缺 |
| `0x30` | INFO_QUERY | `request:bytes_rest` | `HW_QUERY 0x1402` | `type:u8`,`id:u8` | 🟡 只查 pin/pwm |
| `0x31` | INFO_REPLY | `response:bytes_rest` | 無 | — | ❌ 缺 |
| `0x32` | LIVE_TEXT | `text:bytes_rest` | 無 | — | ❌ 缺（文字指令通道） |
| `0x33` | LIVE_REPLY | `response:bytes_rest` | 無 | — | ❌ 缺 |
| `0x40` | OTA_COMMAND | `packet:bytes_rest` | `FILE_BEGIN/CHUNK/END 0x20xx` | 見 schema | 🟡 是檔案非韌體，缺 REBOOT/VERIFY/ABORT |
| `0x41` | OTA_RESPONSE | `packet:bytes_rest` | `FILE_ACK 0x2004` | `file_id`,`offset` | 🟡 |
| `0x70` | DIAGNOSTIC | `data:bytes_rest` | 無 | — | ❌ 缺 |
| `0x71` | ERROR_REPORT | `data:bytes_rest` | 無 | — | ❌ 缺 |

### 狀態欄位對照（對方 STATUS_REPORT 的 state）

| state 值 | 對方語義 | 本專案對應 |
|---|---|---|
| `0` | UNKNOWN | 無 |
| `1` | IDLE | 無（無狀態機，只有 metrics JSON） |
| `2` | RUNNING | 無 |
| `3` | COMPLETED | 無 |
| `4` | DEV（Master 斷線本機測試模式） | 無 |

---

## 4. 反向對照：本專案有、對方沒有（求公平）

| 本專案指令群 | 用途 | 對方 |
|---|---|---|
| `0x30xx` STREAM_* + `0x3003` Direct Mode | 逐幀像素串流 | 無（Slave 本機跑燈效，不傳像素） |
| `0x20xx` FILE_* | 檔案傳輸/查詢/斷點續傳 | 只有 OTA 韌體，無通用檔案傳輸 |
| `0x31xx` PIXEL_*（模式播放：MODE_LIST/GET/SET/STOP/DETAIL） | LED／SERVO 模式清單與播放控制 | 對方有 RS485 MODE_SET/STORY_SET（已併入） |
| `0x12xx` HEARTBEAT + `0x13xx` ESP-NOW | 心跳 + 無 WiFi 控制 | 無（靠 RS485 poll） |
| `0x14xx` HW_CTL | 通用硬體控制（type/id/label/value） | 無通用硬體通道 |
| `0x10xx` SYS_TASK_SET | 雙核任務親和性管理 | 無（單核 main loop） |
| `0x18xx` BENCH | 性能測試（通用接收吞吐） | 無 |

---

## 5. 最重要的參數衝突（合併必談）

### 5.1 5-byte UART 的 `b1` 位元定義兩邊打架 ⚠️ 第一優先

同一條 5-byte 幀 `[0xB4][b1][b2][b3][0xFF]`，第二個 byte 的位元意義：

| bit | 對方 | 本專案（`slave/tasks/action_task_1.py:48-54`） |
|---|---|---|
| bit7 | `loop`（1=循環目前模式） | `MODE_SPECIAL`（特殊模式） |
| bit6 | `story set`（0=LED、1=SERVO） | `MODE_RESERVED`（馬達目標位置：1=頂部、0=底部） |
| bit5–0 | modeId 0–63 | mode 0–63 ✅ 一致 |

> 同一個 bit6，對方拿來選 LED/SERVO 組，本專案拿來指示馬達往上/往下。合併時必須先決定這一 byte 的統一編碼。

### 5.2 亮度三套體系

| 位置 | 對方 | 本專案 |
|---|---|---|
| Timer 端 slot | `0–31` | UART 幀 `0–31`（`_UART_BRIGHTNESS_MAX = 31`） |
| Slave 實際 | `1–190`（Master 換算） | 無換算 |
| **新整合 `MODE_SET.brightness`** | — | **`0–30`**（`0xFF`=不設置，見 `04_pixel_protocol.md §2.4`） |
| WTT 指令（已棄用） | — | `0–36`（`slave/action/waiting_to_trash_actions.py:41`） |

> 本專案自己內部就有「31 vs 36」兩種上限；對方是「31 → 190」兩段式換算。新整合已定案：`MODE_SET.brightness` 統一為 `0–30`（`0xFF`=不設置），對方 1–190 需映射到 0–30。舊 WTT 亮度（0–36）棄用。

### 5.3 `WTT_CTL 0x1501` 缺欄位

本專案有這條設 mode/brightness 的指令，但對方同一動作必帶的欄位我們全缺：

| 對方必帶欄位 | 本專案 `0x1501` 現況 |
|---|---|
| `loop`（循環旗標） | ❌ 無 |
| `story set`（組別） | ❌ 無 |
| `master_start_ms`（同步開始時間） | ❌ 無 |
| `beat`（拍點） | ❌ 無 |

現有 `0x1501` payload 只有 `mode:u8` + `brightness:u8`。

---

## 6. 對方有、本專案完全沒有的功能（逐條）

| # | 功能 | 對方做法 | 本專案現況 |
|---|---|---|---|
| 1 | Story Set（LED/SERVO 組別切換 + 復位安全） | `STORY_SET` + bit6 + `storyMode_motor_reset` | 無「組別」概念，燈光與馬達各自獨立 |
| 2 | 播放同步雙時間戳 | `master_start_ms` + offset 換算 | 無開始時間同步參數 |
| 3 | MODE_NEXT（自動前進） | 遠端指令 | 只有本機按鈕的 `_next_mode()` |
| 4 | MODE_STOP（統一安全停止） | 停 story + 停 motor + 清燈 + 重設 | 只有 `STREAM_STOP`（停串流） |
| 5 | POWER_OFF / POWER_ON（省電） | 開 WiFi 前先 stop + off | 只有 `WIFI_CTRL 0x1008`，無省電與連鎖流程 |
| 6 | 音訊 level/beat | `AUDIO_LEVEL`(0..255 + beat) | `mp3_tf_16p.py` 只有 `play_track/set_volume(0..30)` |
| 7 | 時鐘同步 | `TIME_SYNC_REQUEST/REPLY/OFFSET_APPLY` | 只有 HEARTBEAT 帶 `uptime_ms`，無時鐘差計算 |
| 8 | 執行狀態語義 | `RUNNING/COMPLETED/IDLE/DEV` | `STATUS_GET` 只回 JSON metrics |
| 9 | 硬體資料查詢 | `INFO:PWR/RGB0/1/IPC/IPA/IPB/PSC` | `HW_QUERY` 只回 pin/pwm duty |
| 10 | ColorPicker live | `LC:` 系列 | 無即時調色，只有 `STREAM_FRAME` direct mode |
| 11 | OTA 專用流程 | REBOOT/VERIFY/ABORT/CAPS | 只有 FILE 傳輸，無韌體更新語義 |
| 12 | 保留值過濾 | `0x94/0x95/0x77` 先過濾再解 bit | UART B4 解析只看 SOF/EOF，不認保留 command |

---

## 7. 附錄 A：對方 RS485 command catalog（原始，供留存）

| CMD | 名稱 | 方向 | Broadcast | Reply | Payload（LE） | Payload bytes | Frame/wire bytes |
|---:|---|---|:---:|---|---|---:|---:|
| `0x00` | UNKNOWN | reserved | 否 | — | 空 | 0 | 12/24 |
| `0x01` | MODE_SET | M→S | 是 | — | `mode_id:u8`, `master_start_ms:u32` | 5 | 17/29 |
| `0x02` | MODE_NEXT | M→S | 是 | — | `mode_id:u8`, `master_start_ms:u32` | 5 | 17/29 |
| `0x03` | MODE_STOP | M→S | 是 | — | 空 | 0 | 12/24 |
| `0x04` | BRIGHTNESS | M→S | 是 | — | `value:u8` | 1 | 13/25 |
| `0x05` | STORY_SET | M→S | 是 | — | `set_type:u8` | 1 | 13/25 |
| `0x06` | POWER_OFF | M→S | 是 | — | 空 | 0 | 12/24 |
| `0x07` | POWER_ON | M→S | 是 | — | 空 | 0 | 12/24 |
| `0x08` | AUDIO_LEVEL | M→S | 是 | — | `level:u8`, `beat:u8` | 2 | 14/26 |
| `0x09` | AUDIO_ACTIVE | M→S | 是 | — | `active:u8` | 1 | 13/25 |
| `0x10` | STATUS_QUERY | M→S | 否 | `STATUS_REPORT` | 空 | 0 | 12/24 |
| `0x11` | STATUS_REPORT | S→M | 否 | — | `state:u8`, `reported_mode_id:u8` | 2 | 14/26 |
| `0x12` | PROBE | M→S | 否 | `PROBE_REPLY` | 空 | 0 | 12/24 |
| `0x13` | PROBE_REPLY | S→M | 否 | — | 空 | 0 | 12/24 |
| `0x20` | TIME_SYNC_REQUEST | M→S | 是 | `TIME_SYNC_REPLY` | `time_ms:u32` | 4 | 16/28 |
| `0x21` | TIME_SYNC_REPLY | S→M | 否 | — | `received_at_ms:u32` | 4 | 16/28 |
| `0x22` | TIME_OFFSET_APPLY | M→S | 是 | — | `offset_ms:i32` | 4 | 16/28 |
| `0x30` | INFO_QUERY | M→S | 否 | `INFO_REPLY` | `request:bytes_rest` | 0–240 | 12–252/24–264 |
| `0x31` | INFO_REPLY | S→M | 否 | — | `response:bytes_rest` | 0–240 | 12–252/24–264 |
| `0x32` | LIVE_TEXT | M→S | 是 | — | `text:bytes_rest` | 0–240 | 12–252/24–264 |
| `0x33` | LIVE_REPLY | S→M | 否 | — | `response:bytes_rest` | 0–240 | 12–252/24–264 |
| `0x40` | OTA_COMMAND | M→S | 否 | — | `packet:bytes_rest` | 0–240 | 12–252/24–264 |
| `0x41` | OTA_RESPONSE | 雙向 | 否 | `OTA_RESPONSE` | `packet:bytes_rest`；空 payload 是 poll | 0–240 | 12–252/24–264 |
| `0x70` | DIAGNOSTIC | S→M | 否 | — | `data:bytes_rest` | 0–240 | 12–252/24–264 |
| `0x71` | ERROR_REPORT | S→M | 否 | — | `data:bytes_rest` | 0–240 | 12–252/24–264 |

### 對方核心 payload 欄位定義

| 欄位 | 型別／範圍 | 意思 |
|---|---|---|
| `mode_id` | `u8`；須小於當前 StorySet 的 mode count | 要播放的 StoryMode ID |
| `master_start_ms` | `u32` LE | Master `millis()` 時間域的共同開始點 |
| `value` | `u8`；1..190 | FastLED 全局亮度 |
| `set_type` | `u8`；0=LED, 1=SERVO | StoryMode 組 |
| `level` | `u8`；0..255 | 音量 level |
| `beat` | `u8`；0/1 | 拍點 trigger |
| `active` | `u8`；0/1 | 關閉/開啟 audio-reactive 輸入 |
| `state` | `u8`；0=UNKNOWN,1=IDLE,2=RUNNING,3=COMPLETED,4=DEV | 執行狀態 |
| `reported_mode_id` | `u8` | Slave 當前 mode |
| `time_ms` | `u32` LE | `TIME_SYNC_REQUEST` 送出時的 Master 時間 |
| `received_at_ms` | `u32` LE | Slave 收到 sync request 的本地時間 |
| `offset_ms` | `i32` LE | `slave_local_clock - master_clock` |

---

## 8. 本專案 5-byte UART Display 協定（現況，供留存）

來源：`slave/tasks/action_task_1.py`

```text
幀格式：[0xB4] [mode(8-bit)] [brightness(0-31)] [time] [0xFF]
```

| 欄位 | 內容 |
|---|---|
| byte0 | `0xB4` SOF |
| byte1 | mode：Bit7=特殊模式、Bit6=馬達目標位置(1=頂/0=底)、Bit5-0=模式值 0-63 |
| byte2 | brightness 0-31（APA102 5-bit） |
| byte3 | time（倒數秒） |
| byte4 | `0xFF` EOF |

相關常數（`action_task_1.py:48-54`）：

```text
MODE_SPECIAL  = 0x80   # Bit 7
MODE_RESERVED = 0x40   # Bit 6
MODE_VALUE    = 0x3F   # Bits 5-0
_UART_BRIGHTNESS_MAX = 31
```

---

## 9. 合併建議事項（待決策）

> ⚠️ **本節為 2026-08 當時的建議，部分已於 2026-09 定案。** 現況對照見下方各項註記；
> 定案後的正式合約是 `05_integration_overview.md`（OTA／SYNC／PIXEL 三組）。

1. **統一 5-byte UART 的 mode byte**：bit6/bit7 語義衝突，需先拍板採用哪套定義（§5.1）。
2. **新增 Master 轉譯層指令**：對方 Master↔Slave 的 `MODE_SET/MODE_NEXT/MODE_STOP/STORY_SET/POWER_OFF/POWER_ON/AUDIO_LEVEL/TIME_SYNC/INFO:*/LC:` 這批，需在 NC4 指令集新開一組或直接吃對方文字指令。
   > **已定案**：實際做法是**併入既有域**，沒有另開新群。這裡原本舉例的 `0x16xx`
   > 後來被用作 Router 執行期指令，且該群**已於 2026-09 整組移除**，編號不再使用。
3. **補亮度換算**：加 `slot 0–31 → 實際 1–190` 換算，並統一本專案內部 31 vs 36 的混亂（§5.2）。
   > **已定案**：新整合 `MODE_SET.brightness` 統一為 `0–30`（`0xFF`=不設置），舊 WTT 0–36 棄用。
4. **補同步參數**：`0x1501` 加 `loop/storySet/startTime`（§5.3）。
   > **已定案**：不走 `0x1501`，改用 `0x3105 MODE_SET`（含 `start_delay_ms`）。
   > 注意 `0x100C TIME_OFFSET_APPLY` **目前只有 schema、尚未實作**。
5. **補狀態機**：`0x1502` / STATUS 加 `RUNNING/COMPLETED/IDLE/DEV` 語義（§3 狀態表）。
   > 狀態收斂為 `0x3104 MODE_GET_RSP` 的 `running` + `elapsed_ms`/`total_ms`。

---

## 10. 相關檔案索引

### 外部系統（在 `/Users/tungkinlee/Downloads/`）

- `協議規格_master_timer_slave_精簡版.md` — 總覽（Timer/Master/Slave + I2C）
- `協議規格_slaveUART.md` — RS485 正式規格（frame 格式、command catalog、時鐘同步、OTA）

### 本專案（`mp_Net-Core/`）

- `01_protocol/01_nc4_protocol.md` — NC4 封包 + 指令集（唯一真相描述）
- `slave/schema/*.json` — 指令定義
- `slave/action/*.py` — handler 實作
- `slave/tasks/action_task_1.py` — 5-byte UART Display 協定 + 馬達控制
- `slave/tasks/control_panel.py` — 面板裝置（ESP-NOW 0x1401/0x1501 廣播）
- `slave/action/waiting_to_trash_actions.py` — WTT 指令（0x1501/0x1502）
