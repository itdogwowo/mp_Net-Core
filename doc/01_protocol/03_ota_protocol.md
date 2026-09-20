# OTA 協議（0x22xx）— 零常數約定版

> **用途**：OTA 0x22xx 的完整設計說明——改版理由、指令定義、長度限制、推薦流程。
> **分類**：協議層（01_protocol）
> **最後更新**：2026-08-18
> **權威 schema**：`slave/schema/ota.json`（CMD 0x2201 ~ 0x2220，扁平指令，盡量不靠列舉值/bitmask/常數語義）
> **比對基準**：
> - 舊 I2C OTA：`協議規格_master_timer_slave_精簡版.md §二.7`（CMD 0x01~0x08）
> - 舊 RS485 OTA：`協議規格_slaveUART.md §OTA`（透過 0x40 OTA_COMMAND / 0x41 OTA_RESPONSE 子母包裝）
> - **本專案現況**：`slave/` 無任何 partition OTA 程式碼；只有 `FILE_* 0x20xx` 檔案傳輸。此 0x22xx 為合作方合同，見 `02_command_index.md §10`。

---

## 一、核心原則：「寧可多指令 / 多欄位，不要靠文件對齊的常數」

過去版本有六個地方屬於「schema 說它是 u8/u32，但真正的語義要去 changelog / spec 才知道」。這六個地方，現在全部改成「多指令 + 明確命名 bool 欄位」。

| 過去的「常數約定」依賴 | 舊做法 | 現在怎麼做（schema 自描述） |
|---|---|---|
| `OTA_INFO_QUERY.fields` bitmask | 0x01=BASIC, 0x02=CAPS... 要查表 | **拆成 5 組獨立 QUERY/RSP 指令**（VERSION / CAPS / LAST / PARTITION_STATUS / VERIFY），要什麼就打什麼指令 |
| `OTA_STATUS.state: u8` | 0=IDLE 1=WRITING... 要查表 | **拆成 `OTA_STATE_RSP` 的 4 個 bool**：`state_idle` / `state_writing` / `state_verified` / `state_error`，一個時機只有一個 = 1 |
| `OTA_STATUS.last_err: u8` | 0=OK 1=ACTIVE 2=NO_PARTITION... 要查表 | **拆成 `OTA_ERROR_RSP` 的 11 個 bool**：每個錯誤一個欄位（`err_begin_partition_conflict`、`err_write_offset_mismatch`…），一個錯誤發生時只有一個 = 1；另外附 `failed_offset` / `written_up_to` / `target_slot` 三個描述欄 |
| `OTA_INFO_RSP.features: u8 bitmask` | bit0=SECURE_BOOT, bit1=FLASH_ENCRYPT... 要查表 | **拆成 `OTA_CAPS_RSP` 的 4 個 bool**：`secure_boot` / `flash_encrypt` / `rollback_support` / `diff_ota_support` |
| `OTA_INFO_RSP.last_ota_state: u8` | 0=OK 5=REBOOT_FAIL 0xFF=NEVER 要查表 | **拆成 `OTA_LAST_RSP` 的 7 個 bool**：`last_ota_never` / `last_ota_ok` / `last_ota_begin_fail` / `last_ota_write_fail` / `last_ota_end_fail` / `last_ota_sha_mismatch` / `last_ota_reboot_fail` |
| `OTA_APPLY.restart_delay_ms=0xFFFFFFFF` 語義 | 特定位 pattern 表示「只 set_boot 不重啟」要記 | **拆成兩個獨立參數**：`set_boot_only: u8`（1=不重啟，只設定開機分割；0=按 delay 重啟）+ `restart_delay_ms: u32`（一般 uint32 時間值） |
| `verified_ok: u8` | 0=bad 1=ok（還行但不夠具體） | **拆成 `OTA_VERIFY_RSP` 的 3 種失敗原因 bool**：`verify_fail_sha` / `verify_fail_header` / `verify_fail_crc` + 一個總結 `verify_ok` |
| `OTA_STATUS.image_size / written / target_slot` | 藏在 STATUS 裡 | **拆出 `OTA_PROGRESS_QUERY → OTA_PROGRESS_RSP`**，專門拿進度，跟 STATE 狀態查詢分開 |

> 最終結果：**0x22XX 不再有任何「u8/u32 要查表才知道語義」的常數約定**（除了 `offset: u32` / `image_size: u32` 這類本來就是單純數值的欄位）。Schema 檔案本身就是完整的合約描述。

---

## 二、新版 0x22XX 指令一覽

所有指令統一 `OTA_` 前綴；一共 9 對 QUERY/RSP + 5 個動作指令，總計 **23 個 CMD slot**（0x2201~0x2220 之間保留 0x2208、0x2209、0x220B、0x220D、0x220F、0x220E 給未來擴充）。

### 2.1 動作指令（Master → Slave）

| CMD | NAME | 對應 ESP-IDF 動作 | 參數 | 成功回覆 | 失敗回覆 |
|---:|---|---|---|---|---|
| 0x2201 | `OTA_BEGIN` | `esp_ota_get_next_update_partition()` + `esp_ota_begin()` | `image_size:u32`, `chunk_size:u16`, `sha256:bytes[32]`, `fw_ver:str` | (ACK 或由下一筆 WRITE 開始 ACK) | `OTA_ERROR_RSP`（`err_begin_partition_conflict` / `err_begin_no_partition` / `err_begin_size_too_big` / `err_already_active`） |
| 0x2202 | `OTA_WRITE` | `esp_ota_write()` loop | `offset:u32`, `data:bytes_rest` | **`OTA_ACK`**（`offset` + `written`） | `OTA_ERROR_RSP`（`err_write_offset_mismatch` / `err_write_flash_fail` / `err_write_chunk_too_big` + `failed_offset`） |
| 0x2203 | `OTA_END` | `esp_ota_end()`（header magic + segment CRC 驗證） | — | `OTA_STATE_RSP` (`state_verified=1`) 或接著打 `OTA_VERIFY` 拿詳細結果 | `OTA_ERROR_RSP`（`err_end_validate_fail` / `err_end_not_writing`） |
| 0x2205 | `OTA_ABORT` | 放棄目前 `ota_handle_t`；下次 BEGIN 一定重頭 erase | — | `OTA_STATE_RSP` (`state_idle=1`) | `OTA_ERROR_RSP`（如果不在 session 中） |
| 0x2220 | `OTA_APPLY` | `esp_ota_set_boot_partition()` + 可選 `esp_restart()` | `set_boot_only:u8`, `restart_delay_ms:u32` | (收到即斷線重啟 / 若 set_boot_only=1 則不重啟，回 `OTA_STATE_RSP` state_idle) | `OTA_ERROR_RSP` (`err_apply_set_boot_fail` / `err_not_in_verified_state`) |

> ⚠️ `OTA_APPLY` 兩個參數的語義不再依賴 magic number：
> - `set_boot_only = 1` → **只執行 set_boot_partition，完全不呼叫 restart**（不論 delay_ms 寫什麼）
> - `set_boot_only = 0` → **正常流程**：set_boot_partition 後 sleep(restart_delay_ms) → restart

### 2.2 查詢指令（Master → Slave，Slave 回 *_RSP）

#### 基本資訊 / 版本（~2ms）

| CMD | QUERY | CMD | RSP | 內容 |
|---|---|---|---|---|
| 0x2206 | `OTA_VERSION_QUERY` (空 payload) | 0x2207 | `OTA_VERSION_RSP` | `fw_ver`, `app_sha256[32]`, `running_slot`, `running_seq`, `free_slot`, `partition_size` |

#### 能力 / Chunk 大小限制（<1ms）

| CMD | QUERY | CMD | RSP | 內容（全部 bool，不再有 bitmask） |
|---|---|---|---|---|
| 0x2210 | `OTA_CAPS_QUERY` | 0x2211 | `OTA_CAPS_RSP` | `max_chunk_size:u16`, `secure_boot:u8`, `flash_encrypt:u8`, `rollback_support:u8`, `diff_ota_support:u8` |

#### 上次 OTA 結果（~1ms，NVS cached）

| CMD | QUERY | CMD | RSP | 內容（7 個 bool，不再有 u8 列舉） |
|---|---|---|---|---|
| 0x2212 | `OTA_LAST_QUERY` | 0x2213 | `OTA_LAST_RSP` | `last_ota_never`, `last_ota_ok`, `last_ota_begin_fail`, `last_ota_write_fail`, `last_ota_end_fail`, `last_ota_sha_mismatch`, `last_ota_reboot_fail` + `last_ota_fw_ver`, `last_ota_sha256[32]` |

#### 雙 slot 分割區狀態（~3ms，otadata 讀取 + crc）

| CMD | QUERY | CMD | RSP | 內容 |
|---|---|---|---|---|
| 0x2214 | `OTA_PARTITION_STATUS` | 0x2215 | `OTA_PARTITION_STATUS_RSP` | `slot0_seq`, `slot0_valid`, `slot1_seq`, `slot1_valid`, `running_idx` |

#### Flash 內容重讀驗證（⚠️ 1000~2000ms，最貴；只有 OTA_END 後值得打）

| CMD | QUERY | CMD | RSP | 內容（拆 3 種失敗原因 bool） |
|---|---|---|---|---|
| 0x2216 | `OTA_VERIFY` | 0x2217 | `OTA_VERIFY_RSP` | `verify_ok:u8` + `verify_fail_sha:u8` / `verify_fail_header:u8` / `verify_fail_crc:u8` + `verified_sha256[32]`, `target_slot_seq:u32` |

> END 後想驗證就打 0x2216 OTA_VERIFY，不想驗證就直接 APPLY。語義清晰。

#### 寫入進度（Master polling 顯示 UI 進度列；<1ms）

| CMD | QUERY | CMD | RSP | 內容 |
|---|---|---|---|---|
| 0x2218 | `OTA_PROGRESS_QUERY` | 0x2219 | `OTA_PROGRESS_RSP` | `image_size:u32`, `written:u32`, `target_slot:str` |

#### 目前狀態（Master polling 確認狀態；<1ms）

| CMD | QUERY | CMD | RSP | 內容（4 個 bool，不再有 state:u8 列舉） |
|---|---|---|---|---|
| 0x221A | `OTA_STATE_QUERY` | 0x221B | `OTA_STATE_RSP` | `state_idle:u8` / `state_writing:u8` / `state_verified:u8` / `state_error:u8` + `target_slot:str` |

> 正常情況下同一時間只有一個 bool = 1。如果 Slave 發生內部狀態異常（理論不該有），可能多個 bool = 1，Master 直接視為 state_error 處理即可。

### 2.3 失敗回覆（Slave → Master，所有動作失敗時的共用 RSP）

| CMD | NAME | 11 個錯誤原因 bool | 三個上下文欄位 |
|---|---|---|---|
| 0x221C | `OTA_ERROR_RSP` | `err_begin_partition_conflict` / `err_begin_no_partition` / `err_begin_size_too_big` / `err_write_offset_mismatch` / `err_write_flash_fail` / `err_write_chunk_too_big` / `err_end_validate_fail` / `err_end_not_writing` / `err_apply_set_boot_fail` / `err_not_in_verified_state` / `err_already_active` | `failed_offset:u32`（錯誤發生的位置，0=跟 offset 無關）、`written_up_to:u32`（已成功寫入的位置，重試時從這個 offset 開始即可）、`target_slot:str` |

> 好處：Master 端不用 `if (last_err == 5) ...` 這種 switch 查表，直接 `if (rsp.err_write_offset_mismatch)` 讀欄位名就知道問題。

### 2.4 成功 per-chunk ACK（Slave → Master）

| CMD | NAME | 內容 |
|---|---|---|
| 0x2204 | `OTA_ACK` | `offset:u32`（Master 送的 offset 回聲）, `written:u32`（目前累積長度） |

> 這是唯一一個「只有純數值、不需要 enum」的回覆，語義跟 file.json FILE_ACK 完全一致。

---

## 三、長度限制（bytes_rest / payload / chunk_size）

### 3.1 三層限制仍然存在，但現在靠 `OTA_CAPS_RSP.max_chunk_size` 明確告知

```
Layer 3 (Master 端可控制) — 0x2202 OTA_WRITE.data (bytes_rest)
  ⇒ 長度 <= OTA_BEGIN.chunk_size <= OTA_CAPS_RSP.max_chunk_size
Layer 2 (transport 原生) — RS485=240B / I2C=64B / Python WS=RAM limit
Layer 1 (flash 物理) — 最小 4B 對齊；esp_ota 內部 4KB cache
```

### 3.2 不同 transport 的 `OTA_CAPS_RSP.max_chunk_size` 預設值（Slave 端實作者自行填入）

| transport | max_chunk_size 推薦 | 推算理由 |
|---|---|---|
| RS485 UART（slaveUART.md §OTA） | **224** | 240 (RS485 LEN) − 16 (header/其他) = 224；舊版實際值 |
| I2C（精簡版 §二.6） | **56** | 舊版 fast chunk 56 bytes；I2C slave FIFO 限制 |
| Python WebSocket（mp_Net-Core） | **4096** | 對齊 flash sector；MicroPython 正常約 200KB 可用 RAM |
| diff_ota_support = 1 | **≤ 1024** | 增量更新為了 hash tree 對齊，chunk 要小 |

### 3.3 長度違反時的標準回覆

一律回 **`OTA_ERROR_RSP`** 搭配對應的錯誤 bool：

| 違規 | OTA_ERROR_RSP 哪個 bool = 1 |
|---|---|
| `OTA_BEGIN.image_size > OTA_VERSION_RSP.partition_size` | `err_begin_size_too_big` |
| `OTA_BEGIN.chunk_size > OTA_CAPS_RSP.max_chunk_size` | `err_begin_size_too_big`（或 Slave 直接 silent cap，但推薦用 error 強烈提醒） |
| `OTA_WRITE.data.length > max_chunk_size` | `err_write_chunk_too_big`，written_up_to 不增加 |
| `OTA_WRITE.offset != session.written` | `err_write_offset_mismatch` + `failed_offset=OFFSET` + `written_up_to=session.written` |

---

## 四、推薦 Master 標準流程

```
【前置查詢（START 前，全部約 7ms）】
 0x2206 OTA_VERSION_QUERY   → OTA_VERSION_RSP
     → app_sha256 相同就跳過整個 OTA
     → partition_size >= image_size 才繼續
 0x2210 OTA_CAPS_QUERY      → OTA_CAPS_RSP
     → max_chunk_size 當成 OTA_BEGIN.chunk_size 上限
     → secure_boot=1 要確認 image 有簽章
 0x2212 OTA_LAST_QUERY      → OTA_LAST_RSP
     → last_ota_reboot_fail=1 就不要重傳 firmware，直接報修
 0x2214 OTA_PARTITION_STATUS → OTA_PARTITION_STATUS_RSP
     → (除錯 UI 用，正常流程可省略省 3ms)

【本體流程】
 0x2201 OTA_BEGIN  { image_size, chunk_size, sha256, fw_ver }
     → 失敗：收到 OTA_ERROR_RSP (err_begin_* = 1)
 【for each chunk】:
     0x2202 OTA_WRITE { offset, data(<max_chunk_size) }
       → 成功 : 0x2204 OTA_ACK (offset, written)
       → 50ms timeout：同一包最多重試 3 次（per-chunk，不是整批重來）
       → 失敗   : 0x221C OTA_ERROR_RSP (err_write_* = 1)
                   先 0x2205 ABORT 再報錯
 0x2203 OTA_END
     → 失敗：OTA_ERROR_RSP (err_end_validate_fail / err_end_not_writing = 1)

【END 後查詢（驗證階段）】
 0x221A OTA_STATE_QUERY → OTA_STATE_RSP (state_verified=1 ?)
 0x2216 OTA_VERIFY      → OTA_VERIFY_RSP  (~1000~2000ms)
     → verify_ok=1 + verified_sha256 == OTA_BEGIN.sha256 才繼續
     → 任一 verify_fail_*=1  → 0x2205 ABORT，丟棄不要 APPLY

【套用 + 重啟】
 0x2220 OTA_APPLY { set_boot_only=0, restart_delay_ms=200 }
     或 { set_boot_only=1 } 如果要先做健康檢查再手動重啟

【reconnect 後最終確認（約 2ms）】
 0x2206 OTA_VERSION_QUERY → OTA_VERSION_RSP
     → fw_ver & app_sha256 與期望相同，才算真正成功
```

---

## 五、舊版做法對照 — 哪些跟進、哪些不跟進

| 舊版項目 | 新版對應 | 跟進 / 不跟進 | 理由 |
|---|---|---|---|
| START / DATA / END 三段架構 | 0x2201 / 0x2202 / 0x2203 | ✅ 跟進 | 主流 OTA 框架固定三段 |
| STATUS / ABORT / CAPS / VERIFY | 每個都拆成獨立指令與 RSP bool 欄位 | ✅ 跟進 + 強化（拆 enum） | 原本有指令，但我們把它們的常數約定都拔掉了 |
| 子母包 0x40/0x41 | 維持扁平 top-level CMD 0x22xx | ❌ 不跟進 | Python 端不需要 wrapper 子協議 |
| 內層 OTA packet CRC32 | 不做 | ❌ 不跟進 | 外層 transport 已有 CRC |
| DATA packet SEQ (HI/LO) | 用 `offset:u32`（file.json FILE_CHUNK 風格） | ⚠️ 半跟進 | 避免重複 offset 機制，又不用加 seq enum |
| REBOOT 單獨指令 | 併入 `OTA_APPLY.set_boot_only + restart_delay_ms` | ✅ 跟進（欄位化） | 但移除 0xFFFFFFFF magic number 約定 |
| PARTITION_STATUS 獨立指令 | 0x2214 / 0x2215 恢復成獨立指令 | ✅ 跟進 | 原本就想保留，之前塞進 RSP 反而晦澀 |
| fields bitmask（INFO_QUERY.fields） | 刪除，改成多個 QUERY 指令 | ❌ 完全不跟進（這次主要目的） | 回歸常數約定零依賴 |
| features/state/last_err u8 列舉 | 全部拆成多個 bool 欄位 | ❌ 完全不跟進（這次主要目的） | Schema 自描述 |
| restart_delay_ms 0xFFFFFFFF 語義 | 拆成 `set_boot_only:u8` 獨立參數 | ❌ 完全不跟進（這次主要目的） | 不再有 magic number |

---

## 六、新版 CMD 編號速查表

```
0x2201  OTA_BEGIN                 (動作)
0x2202  OTA_WRITE                 (動作)
0x2203  OTA_END                   (動作)
0x2204  OTA_ACK                   (成功回覆 per-chunk)
0x2205  OTA_ABORT                 (動作)
0x2206  OTA_VERSION_QUERY
0x2207  OTA_VERSION_RSP
---- (保留 0x2208, 0x2209 未來擴充) ----
0x2210  OTA_CAPS_QUERY
0x2211  OTA_CAPS_RSP
0x2212  OTA_LAST_QUERY
0x2213  OTA_LAST_RSP
0x2214  OTA_PARTITION_STATUS
0x2215  OTA_PARTITION_STATUS_RSP
0x2216  OTA_VERIFY
0x2217  OTA_VERIFY_RSP
0x2218  OTA_PROGRESS_QUERY
0x2219  OTA_PROGRESS_RSP
0x221A  OTA_STATE_QUERY
0x221B  OTA_STATE_RSP
0x221C  OTA_ERROR_RSP             (失敗共用回覆)
---- (保留 0x221D, 0x221E, 0x221F 未來擴充) ----
0x2220  OTA_APPLY                 (動作)
```

## 相關文件

- `02_command_index.md` — 完整指令索引（本文件的指令表收錄處）
- `03_notes/03_ota_design_reference.md` — ESP-IDF partition OTA 機制參考底稿
- `05_integration_overview.md` — 協議整合總規格（與 master_timer_slave）
- `06_migration_guide.md` — 對方 OTA 指令逐條遷移對照
