# 協議整合總規格（mp_Net-Core × master_timer_slave）

> **用途**：兩邊共同閱讀與執行之**統一合約**，涵蓋全部整合指令。
> **分類**：協議層（01_protocol）
> **最後更新**：2026-10
> **基準**：**mp_Net-Core（本專案）** 之 NC4 封包與 `slave/schema/*.json` 為準，對方（fastLED master_timer_slave）遷就我方實作。
>
> ⚠️ **本文件的範圍**：只涵蓋「與對方 RS485 舊指令群對應」的**三組整合指令**（OTA／SYNC／PIXEL）。
> 它**不是** NC4 的完整指令集——`0x12xx` 心跳、`0x13xx` ESP-NOW、`0x14xx` 硬體、
> `0x15xx` 待清理、`0x18xx` bench、`0x20xx` 檔案、`0x30xx` 串流、`0x32xx` 音訊
> **都不在這裡**，請查 `02_command_index.md`（12 個域、112 條指令）。
>
> **子文件**（細節定義）：
> - `03_ota_protocol.md` — OTA 0x22xx 設計與改版理由
> - `04_pixel_protocol.md` — Pixel／Mode 0x31xx 整合細節
> - `06_migration_guide.md` — 人讀版：對方每條指令變成什麼、參數怎麼對
> - `slave/schema/ota.json`、`slave/schema/pixel.json`、`slave/schema/sys.json` — 權威 schema 定義

---

## 0. 一分鐘結論

整合共三組指令，取代對方 RS485 舊指令群：

| 組 | 範圍 | 指令數 | 取代對方 |
|---|---|---|---|
| **OTA** | `0x22xx` | 21 | `0x40/0x41` 子母包 + 內層 `0x01~0x08`（韌體層重寫） |
| **SYNC** | `0x100A~0x100C`（併入 sys） | 3 | `TIME_SYNC_REQUEST / TIME_SYNC_REPLY / TIME_OFFSET_APPLY`（選用） |
| **PIXEL** | `0x31xx` | 8 | `MODE_SET / MODE_NEXT / MODE_STOP / POWER_OFF / POWER_ON / STATUS_QUERY / STATUS_REPORT / STORY_SET` |

> 🚨 **兩項整合目標目前尚未實作，對接前務必先讀**：
>
> | 項目 | 現況 | 後果 |
> |---|---|---|
> | `0x22xx` **OTA（全部 21 條）** | **只有 schema，沒有一條 handler**（`slave/` 無任何 partition OTA 程式碼） | 照本文件實作 OTA 會**完全收不到回應** |
> | `0x100C TIME_OFFSET_APPLY` | 只有 schema，未實作 | 同上 |
> | `0x3103 / 0x3104` **MODE_GET / MODE_GET_RSP** | 只有 schema，**無 handler、也未由任何任務發出** | 見下方 §2 註記——這正是取代對方 `STATUS_QUERY/STATUS_REPORT` 的那一對 |
>
> 這三項在 `02_command_index.md` 與 `03_ota_protocol.md` 都有標示，本文件先前漏了。
>
> PIXEL 域在整合定案（6 條）之後又長出 `0x3107 / 0x3108 MODE_DETAIL_QUERY/RSP`，
> 故現為 8 條。實際可用的起步同步手段是 `MODE_SET.start_delay_ms`。

三項共同原則：

1. **零常數約定**：狀態、錯誤、能力全部用自描述 bool 或命名欄位，不靠「查表才知道的 u8/u32 語義」。
2. **schema 為唯一真相**：指令與 payload 以 `slave/schema/*.json` 為準，文件只是說明。
3. **時鐘同步為選用**：起步同步用 `MODE_SET.start_delay_ms`（相對延遲）；時鐘同步只做時鐘對齊與跨 Slave 時間一致性。
4. **Slave 被動原則**：Slave 不主動推送，所有回覆都是回應 Master 的查詢／動作。

---

## 1. 指令總表

### 1.1 OTA（0x22xx）— 韌體更新

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

> 細節（payload 定義、錯誤 bool 全表、長度限制、推薦流程）見 `03_ota_protocol.md`。

### 1.2 SYNC（時鐘同步，選用）— 已併入 sys.json 0x10xx

| CMD | NAME | 方向 | Payload |
|---:|---|---|---|
| `0x100A` | `TIME_SYNC` | Master → Slave（可廣播） | `master_time_ms:u32` |
| `0x100B` | `TIME_SYNC_RSP` | Slave → Master | `received_at_ms:u32` |
| `0x100C` | `TIME_OFFSET_APPLY` | Master → Slave | `offset_sign:u8`, `offset_ms:u32` |

> `offset` 用「符號 + 量值」兩欄（無 `i32` 型別）；選用，僅做時鐘對齊，不綁定起步時機。

### 1.3 PIXEL（0x31xx）— 模式播放

| CMD | NAME | 方向 | Payload |
|---:|---|---|---|
| `0x3101` | `MODE_LIST_QUERY` | Master → Slave | `mode_type:u8`（0=全部、1=LED、2=SERVO） |
| `0x3102` | `MODE_LIST_RSP` | Slave → Master | `mode_type:u8`（回音）, `count:u8`, `entries:bytes_rest`（子格式：**每筆 2B = 內部 16-bit 模式 id，u16 LE**；**沒有 total_ms 欄位**） |
| `0x3103` | `MODE_GET` | Master → Slave | — |
| `0x3104` | `MODE_GET_RSP` | Slave → Master | `mode_type:u8`, `mode_id:u8`, `elapsed_ms:u32`, `total_ms:u32`, `running:u8` |
| `0x3105` | `MODE_SET` | Master → Slave | `mode_type:u8`, `mode_id:u8`, `start_delay_ms:u16`, `brightness:u8` |
| `0x3106` | `MODE_STOP` | Master → Slave | `action:u8`（0=暫停、1=全關閉） |
| `0x3107` | `MODE_DETAIL_QUERY` | Master → Slave | `mode_type:u8`, `mode_id:u8` |
| `0x3108` | `MODE_DETAIL_RSP` | Slave → Master | `mode_type:u8`, `mode_id:u8`, `total_ms:u32`, `name:str_u16len` |

> `mode_type` 語義：`0`=系統（UNKNOWN/DEV）、`1`=LED 組、`2`=SERVO 組、**`3`=AUDIO 組**，其餘保留。
> 細節見 `04_pixel_protocol.md`。

---

## 2. 取代對照表（對方舊指令 → 我方）

| 對方 RS485 舊指令 | 我方整合指令 | 備註 |
|---|---|---|
| `0x01 MODE_SET` | `0x3105 MODE_SET` | 起步改用 `start_delay_ms` 相對延遲 |
| `0x02 MODE_NEXT` | `0x3105 MODE_SET` | payload 相同，合併 |
| `0x03 MODE_STOP` | `0x3106 MODE_STOP (action=0)` | 暫停 |
| `0x06 POWER_OFF` | `0x3106 MODE_STOP (action=1)` | 全關閉＋省電 |
| `0x07 POWER_ON` | （移除） | 恢復一律用 `MODE_SET` |
| `0x05 STORY_SET` | `0x3105 MODE_SET.mode_type` | LED=`1`、SERVO=`2`，併入欄位 |
| `0x10 STATUS_QUERY` | `0x3103 MODE_GET` | — |
| `0x11 STATUS_REPORT` | `0x3104 MODE_GET_RSP` | state 收斂為 `mode_type` + `running` |
| `0x20/0x21/0x22 TIME_SYNC_*` | `0x100A/0x100B/0x100C` | 選用 |
| `0x40/0x41 OTA_COMMAND/OTA_RESPONSE` + 內層 `0x01~0x08` | `0x22xx OTA_*` | 對方韌體層重寫（CRC32→SHA256、SEQ→offset、子母包→扁平） |
| — | `0x3101/0x3102 MODE_LIST_*` | 新增，對方原本無模式清單查詢（列表只含 ID + 總時間） |
| — | `0x3107/0x3108 MODE_DETAIL_*` | 新增，逐個模式查名稱等細節 |
| `0x04 BRIGHTNESS` | ✅ `0x3105 MODE_SET.brightness` | 對方 `value:u8`（1–190）→ 我方 `brightness:u8`（0–30）；`0xFF`=不設置 |
| `0x08/0x09 AUDIO_*` | （我方不建立） | 不整合 |
| `0x30~0x33 INFO/LIVE` | （不整合） | 對方既有另案處理 |
| `0x70/0x71 DIAGNOSTIC/ERROR_REPORT` | （移除） | Slave 被動，不主動回報 |

---

## 3. 待決事項（整合會議拍板）

| # | 事項 | 現況 |
|---|---|---|
| 1 | 播完（COMPLETED）表達 | `running=0` + `elapsed>=total` 輪詢推得；是否新增主動通知待決 |
| 2 | 亮度（BRIGHTNESS）通道 | ✅ 併入 `MODE_SET.brightness`（0–30，`0xFF`=不設置）；對方 1–190 需映射 |
| 3 | 暫停後續播 | 一律重頭開始；續播需 `MODE_SET` 加 `resume_from_ms` |
| 4 | 對方韌體 OTA 重寫排程 | 0x22xx 遷移時程 |

---

## 4. 相關文件索引

| 文件 | 內容 |
|---|---|
| `01_protocol/01_nc4_protocol.md` | NC4 封包格式（SOF/ADDR/CMD/CRC/schema/傳輸層） |
| `01_protocol/02_command_index.md` | 完整指令索引（本檔三組指令的收錄處） |
| `01_protocol/03_ota_protocol.md` | OTA 0x22xx 完整設計 |
| `01_protocol/04_pixel_protocol.md` | PIXEL 0x31xx 完整定義（含時鐘同步） |
| `01_protocol/06_migration_guide.md` | **人讀版**：對方每一條指令變成什麼、參數怎麼對、架構怎麼理解 |
| `01_protocol/07_merge_comparison.md` | 兩套系統全景比對（含對方 RS485 catalog 留存） |
| `slave/schema/ota.json` | OTA 權威 schema |
| `slave/schema/pixel.json` | PIXEL 權威 schema |
| `slave/schema/sys.json` | 系統 + 時鐘同步權威 schema（0x100A~0x100C） |
