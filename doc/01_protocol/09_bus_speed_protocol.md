# 臨時提速（bus_speed）完整工作流程

> **用途**：傳輸大檔案前臨時拉高 UART baud、傳完還原的**端對端完整工作流程**——涵蓋 slave 已實作行為、master 端整合步驟、時序、payload、失敗處理與實作範例。
> **分類**：協議層（01_protocol）
> **最後更新**：2026-08-21
> **適用情境**：UART 實體線（CircuitBus）上要傳大檔案（JPK 素材、韌體、資料包）時，先協商切到高 baud 傳輸，結束後還原，省下大量傳輸時間。
> **實作位置**：
> - `slave/lib/sys/bus_speed.py` — 提速狀態機（**已實作**）
> - `slave/action/hw_actions.py` — 0x1403~0x1408 handler（**已實作**）
> - `slave/tasks/circuit.py` — `CircuitTask.loop` 每輪呼叫 `bus_speed_poll()`（**已實作**）
> - `slave/schema/hw.json` — 指令 schema（**已實作**）
> - `tools/PC/NetBusMaster.py` — master 端（**尚未整合提速**，本文件 §6 提供整合範例）

---

## 0. 一分鐘結論

```
master                              slave
  │ 1. SPEED_QUERY 確認 IDLE          │
  ├─────────────────────────────────>│
  │ 2. SPEED_SET {speed, timeout}    │ 記 old_baud / target / timeout_at
  ├─────────────────────────────────>│
  │<── 3. SPEED_ACK (ok, 舊速) ──────┤ ← 同步點：雙方收到 ACK 後「立即切速」
  │ 4. STATUS_GET / IDENTIFY_REQ 敲門 │
  ├─────────────────────────────────>│
  │<── 5. 收到有效回覆 = 新速通 ──────┤
  │ 6. SPEED_COMMIT → 鎖定 (取消回滾) │
  ├─────────────────────────────────>│
  │ 7. 傳輸 (FILE_* 0x20xx)          │ ← 全程新速
  │ 8. SPEED_REVERT → 還原舊速        │
  └─────────────────────────────────>│
```

**核心設計**：
- **同步點 = SPEED_ACK**：slave 送出 ACK 後「同一 handler 內」立即 `uart.init(baudrate=target)`；master 收到 ACK 後立即切速。雙方在同一瞬間換速，不需等待。
- **唯一的保險 = `timeout_ms`**：slave 切速後若沒收到 `SPEED_COMMIT`，計時到點自動回滾 config 舊速。這個檢查是純時間檢查（`bus_speed_poll()`），**不依賴收到指令**——即使新速下收不到有效幀也會回滾。
- **不偵測亂碼、不 auto-baud**：切速瞬間外部 bus 的亂碼是自然現象，架構不處理；驗證靠「敲門指令」確認新速通。

---

## 1. 狀態機（slave 端，`bus_speed.py`）

```
IDLE ── SPEED_SET 切速 ──▶ SYNCING ── SPEED_COMMIT ──▶ COMMITTED
  ▲                          │   │                        │
  │◀─── 超時自動回滾 ────────┘   │                        │
  │◀──── SPEED_REVERT 還原 ─────┴────────────────────────┘
```

| state | 值 | 意義 | 回滾計時 |
|---|---|---|---|
| `STATE_IDLE` | 0 | 一般速度 | 無 |
| `STATE_SYNCING` | 1 | 已切速、待 COMMIT | **計時中**（`timeout_at`） |
| `STATE_COMMITTED` | 2 | 已鎖定新速 | 已取消（`timeout_at=0`） |

- `bus_speed_set()`：`bus_type != 7`（非 UART）或找不到 UART → 回 `ok=0`（not supported / 找不到）。
- `bus_speed_commit()`：狀態不是 SYNCING、或 bus_type/bus_id 不符 → 回 `ok=0`。
- `bus_speed_revert()`：bus_type/bus_id 不符 → 回 `ok=0`；否則還原 `old_baud` 進 IDLE。

---

## 2. 指令表（hw 群 0x1403~0x1408）

| CMD | 名稱 | 方向 | Payload |
|---|---|---|---|
| 0x1403 | SPEED_SET | M→S | `bus_type(u8)` `bus_id(u8)` `speed(u32)` `timeout_ms(u32)` |
| 0x1404 | SPEED_ACK | S→M | `ok(u8)` `bus_type(u8)` `bus_id(u8)` `cur_speed(u32)` `target_speed(u32)` |
| 0x1405 | SPEED_COMMIT | M→S | `bus_type(u8)` `bus_id(u8)` |
| 0x1406 | SPEED_REVERT | M→S | `bus_type(u8)` `bus_id(u8)` |
| 0x1407 | SPEED_QUERY | M→S | `bus_type(u8)` `bus_id(u8)` |
| 0x1408 | SPEED_STATUS | S→M | `state(u8)` `bus_type(u8)` `bus_id(u8)` `cur_speed(u32)` `target_speed(u32)` `remain_ms(u32)` |

### 參數語意

- **`bus_type`**：沿用 `hw_manager.HW` 常數——`UART=7`、`SPI=2`、`I2C=3`。**第一階段僅實作 UART（7）**；SPI/I2C 回 `ok=0`（not supported）。
- **`bus_id`**：⚠️ 實作上當作 **`uart_list` 的 list 索引**（`_get_uart` 直接 `lst[int(bus_id)]`）。config `UART.list` 的第 0 筆 → `bus_id=0`。要對應 config 的 `id` 欄位需由 caller 傳 index，目前以索引為準。
- **`speed`**：目標 baud（u32；如 921600 超過 u16，必須用 u32）。
- **`timeout_ms`**：切速後「沒收到 COMMIT 就自動回滾」的保險時間，**不是 apply delay**。`0` = 永不超時（不建議，會卡在 SYNCING 不自動回滾）。
- **`state`（SPEED_STATUS）**：0=IDLE、1=SYNCING（已切、待 COMMIT）、2=COMMITTED（鎖定）。
- **`remain_ms`**：SYNCING 時剩餘的保險時間（超時前）；IDLE/COMMITTED 為 0。

---

## 3. 完整時序（step-by-step）

### Step 0 — 前置檢查（可選但建議）

master 先 `SPEED_QUERY`（0x1407）確認 slave 目前是 IDLE（`state=0`），避免在別人已經 SYNCING/COMMITTED 時打斷。也可順便看 `cur_speed` 是否已是目標（已達目標就不需要提速）。

```
M→S  SPEED_QUERY {bus_type:7, bus_id:0}
S→M  SPEED_STATUS {state:0, cur_speed:115200, target_speed:115200, remain_ms:0}
```

### Step 1 — SPEED_SET（master 發起）

```
M→S  SPEED_SET {bus_type:7, bus_id:0, speed:921600, timeout_ms:3000}
```

slave 行為（`on_speed_set` → `bus_speed_set`）：
1. 記錄 `old_baud`（切速前的 `uart.baudrate`）、`target_baud`、`timeout_at = now + timeout_ms`
2. 立刻 `uart.init(baudrate=921600)` 切速
3. 狀態 → `SYNCING`
4. 回 `SPEED_ACK {ok:1, cur_speed:115200(舊速), target_speed:921600}`

> ⚠️ 注意 ACK 的 `cur_speed` 欄位填的是**切速前的舊速**（`bus_speed_set` 在切速前先讀 `_cur_baud`）。這是同步點設計的一部分：master 從 ACK 得知「我該從哪個速度切到哪個」。

### Step 2 — 同步點：雙方切速

- **slave**：`on_speed_set` 內回 ACK 之前就已 `uart.init()` 切速（同 handler 內，見 `bus_speed_set` 第 73 行）。
- **master**：收到 `SPEED_ACK` 後**立即**把對應 UART 的 baud 切到 `target_speed`（921600）。

> 這個「送出 ACK 即切速」的設計讓雙方幾乎同時換速。master 端收到 ACK 之後的一切通訊都必須用新速。

### Step 3 — 敲門驗證（確認新速通）

切速後 master 用**既有指令**敲門，確認新速下雙向都通：

- `STATUS_GET`（0x1101 `{query_type:0}`）→ 等 `STATUS_RSP`（0x1102）
- 或 `IDENTIFY_REQ`（0x100D `{reply_cid:...}`）→ 等 `IDENTIFY_RSP`（0x100E）

```
M→S  STATUS_GET {query_type:0}
S→M  STATUS_RSP {status_json:...}      ← 收到 = 新速雙向通
```

**驗證失敗的行為**：如果收不到有效回覆，**不要急著重試**——先等 slave 的保險機制。slave 在 `timeout_ms` 內沒收到 COMMIT 就會自動回滾舊速；回滾後 master 用舊速重試 `SPEED_QUERY` 會看到 `state=0, cur_speed=115200`（已還原）。

### Step 4 — SPEED_COMMIT（鎖定）

驗證 OK → `SPEED_COMMIT`，取消回滾保險：

```
M→S  SPEED_COMMIT {bus_type:7, bus_id:0}
```

slave 行為（`bus_speed_commit`）：狀態 → `COMMITTED`、`timeout_at=0`（不再回滾）。此後新速持續有效，直到 `SPEED_REVERT`。

### Step 5 — 傳輸（全程新速）

以新速執行實際傳輸，例如 FILE 域上傳（`FILE_BEGIN` 0x2001 → 迴圈 `FILE_CHUNK` 0x2002 → `FILE_END` 0x2003）或下載（`FILE_READ` 0x2007）。

```
M→S  FILE_BEGIN {file_id, total_size, chunk_size, sha256, path}
S→M  FILE_ACK {file_id, offset}          ← 每塊
M→S  FILE_CHUNK {file_id, offset, data}
...
M→S  FILE_END {file_id}
```

> 傳輸期間 slave 維持 `COMMITTED`，不會回滾。`CircuitTask.loop` 照跑（`bus_speed_poll()` 對 COMMITTED 不做任何事）。

### Step 6 — SPEED_REVERT（還原）

傳輸完成 → `SPEED_REVERT`，把 slave 切回 config 舊速：

```
M→S  SPEED_REVERT {bus_type:7, bus_id:0}
```

slave 行為（`bus_speed_revert` → `_revert`）：`uart.init(baudrate=old_baud)` → 狀態 → `IDLE`。

> ⚠️ **master 端也要同步切回舊速**（如同 Step 2 的雙向切速）。SPEED_REVERT 沒有 ACK（`on_speed_revert` 不回覆），master 可用 `SPEED_QUERY` 確認 `state=0, cur_speed=115200` 後再切回自己端。

### Step 7 — 驗證還原（可選）

```
M→S  SPEED_QUERY {bus_type:7, bus_id:0}
S→M  SPEED_STATUS {state:0, cur_speed:115200, ...}
```

---

## 4. 失敗情境與處理表

| 情境 | slave 行為 | master 應對 |
|---|---|---|
| SPEED_SET 的 bus_type ≠ 7 | 回 `SPEED_ACK {ok:0}`（not supported） | 確認 bus_type 用 `hw_manager.HW.UART=7` |
| SPEED_SET 找不到 UART（bus_id 超界） | 回 `SPEED_ACK {ok:0}` | 確認 bus_id 是 `uart_list` 索引（config `UART.list` 順序） |
| `uart.init()` 切速失敗（如 baud 不支援） | 回 `SPEED_ACK {ok:0, cur_speed:舊速, target_speed:目標}`，不切速 | 換一組 baud |
| 切速後敲門失敗（新速不通） | 維持 SYNCING，`timeout_ms` 到 → 自動回滾舊速 → IDLE | **不要重試新速**；等 `timeout_ms` 過去，用舊速 `SPEED_QUERY` 確認 `state=0` 後，換 baud 或檢查接線/極性 |
| master 忘了 COMMIT（slave 一直 SYNCING） | `timeout_ms` 到 → 自動回滾 | 同上；這是 `timeout_ms` 存在的意義 |
| 傳輸中想中止 | slave 維持 COMMITTED 直到 REVERT | 發 `SPEED_REVERT` 還原（或重開機；重開機後狀態自然回 IDLE，因為狀態存內存 `bus.shared`） |
| SPEED_COMMIT 打錯 bus | `bus_speed_commit` 回 `ok=0`（bus 不符），維持 SYNCING | 檢查 bus_type/bus_id |
| SPEED_REVERT 打錯 bus | `bus_speed_revert` 回 `ok=0` | 檢查 bus_type/bus_id |

> **「收不到回覆」時的判斷順序**：先 `SPEED_QUERY`（舊速）看 slave 狀態——`state=1` 表示 slave 還在等 COMMIT；`state=0` 表示已回滾或從未切速。依狀態決定下一步，不要盲目重發。

---

## 5. 狀態機與既有任務的互動

- **回滾檢查在 `CircuitTask.loop`**（`slave/tasks/circuit.py:152` `bus_speed.bus_speed_poll()`），每輪都跑，**不依賴收到指令**。這解掉一個死結：若新速下 slave 收不到 master 任何指令，惰性檢查（等指令觸發）永遠不會跑；改成每輪純時間檢查後，即使收不到也會回滾。
- **狀態存在 `bus.shared["_bus_speed"]`**（內存 dict），重開機即消失回 IDLE。不需要持久化。
- 提速只影響**該 UART bus 的 baud**；其他 bus（WiFi/ESP-NOW/SPI）不受影響。

---

## 6. master 端實作範例（對齊 `tools/PC/NetBusMaster.py`）

master 端目前**尚未整合**提速，以下為建議整合方式。NetBusMaster 已有 `send_pkt(targets, cmd_id, args)`（用 SchemaStore 打包送出），直接用它發指令；等待回應用現有的 parser/`dispatch_logic` 機制（`SPEED_ACK 0x1404`、`SPEED_STATUS 0x1408` 需在 `dispatch_logic` 加上對應處理，參考既有 `0x2004 FILE_ACK` 的寫法）。

```python
# 以 NetBusMaster 為基礎的提速包裝（示意）
class SpeedUp:
    BUS_UART = 7

    def __init__(self, master, bus_id=0):
        self.m = master
        self.bus_id = bus_id
        self.old_baud = None
        self.target = None

    def query(self):
        """SPEED_QUERY → 回 SPEED_STATUS 或 None。"""
        self.m.send_pkt(self.m.selected_targets, 0x1407,
                        {"bus_type": self.BUS_UART, "bus_id": self.bus_id})
        # 等 0x1408（用現有 event/timeout 機制），回 (state, cur, target, remain)
        return self.m._wait_evt("speed_status", 1.0)   # 示意

    def enter(self, target_baud=921600, timeout_ms=3000):
        """Step 1+2：SPEED_SET → 等 SPEED_ACK → 立即切 master 端 baud。"""
        self.target = target_baud
        self.m.send_pkt(self.m.selected_targets, 0x1403, {
            "bus_type": self.BUS_UART, "bus_id": self.bus_id,
            "speed": target_baud, "timeout_ms": timeout_ms,
        })
        ack = self.m._wait_evt("speed_ack", 1.0)       # 等 0x1404
        if not ack or ack.get("ok") != 1:
            raise RuntimeError("SPEED_SET failed: %r" % ack)
        self.old_baud = ack["cur_speed"]               # ACK 帶的舊速
        # ⚠️ 同步點：收到 ACK 後立即切 master 端 UART baud
        self._set_master_baud(target_baud)

    def verify(self):
        """Step 3：敲門（STATUS_GET 0x1101）確認新速雙向通。"""
        self.m.send_pkt(self.m.selected_targets, 0x1101, {"query_type": 0})
        rsp = self.m._wait_evt("status_rsp", 1.0)      # 等 0x1102
        return rsp is not None

    def commit(self):
        """Step 4：SPEED_COMMIT 鎖定。"""
        self.m.send_pkt(self.m.selected_targets, 0x1405,
                        {"bus_type": self.BUS_UART, "bus_id": self.bus_id})

    def revert(self):
        """Step 6：SPEED_REVERT 還原 + master 端同步切回舊速。"""
        self.m.send_pkt(self.m.selected_targets, 0x1406,
                        {"bus_type": self.BUS_UART, "bus_id": self.bus_id})
        # 確認 slave 已回 IDLE 後，master 端再切回舊速（順序重要，避免半速狀態）
        self._set_master_baud(self.old_baud)

    def run(self, fn, target_baud=921600, timeout_ms=3000):
        """完整流程：enter → verify → commit → fn() → revert（含失敗還原）。"""
        self.enter(target_baud, timeout_ms)
        try:
            if not self.verify():
                raise RuntimeError("verify failed, slave will auto-revert")
            self.commit()
            return fn()
        finally:
            try:
                self.revert()
            except Exception:
                pass
```

使用：

```python
def upload_big_file(master):
    # ... 現有 FILE 上傳邏輯（0x2001/0x2002/0x2003）...
    pass

sp = SpeedUp(master)
sp.run(upload_big_file)        # 整個上傳跑在 921600
```

> ⚠️ **失敗時程**：`verify()` 失敗時 slave 會自己回滾（`timeout_ms` 內），master 端**不要**立刻把 baud 切回——先用舊速 `SPEED_QUERY` 等 `state=0` 再切，避免兩端速度不一致。

---

## 7. 參數建議

| 參數 | 建議值 | 說明 |
|---|---|---|
| `speed`（目標 baud） | 460800 / 921600 | 對端 ESP 能穩定支援的高 baud；現場實測後取穩定值 |
| `timeout_ms` | 1000~3000 | 太短 → 敲門還沒回就回滾；太長 → 卡在 SYNCING 越久。以「敲門 + 回覆」的往返時間為基準 |
| FILE `chunk_size`（配合提速） | 可加大到 4KB | 提速後每塊可傳更多；仍受 `MAX_PAYLOAD`(8KB) 與 lwIP 發送約束（見 `08_performance_benchmark.md`） |
| `bus_id` | config `UART.list` 的索引 | 目前實作 = list 索引，非 config `id` 欄位 |

---

## 8. 擴充 SPI / I2C（後續）

- 擴充 `bus_speed._get_uart` 為依 `bus_type` 分派到 `spi_list`/`i2c_list`。
- SPI/I2C 需 deinit + 重建（非 `.init` 一鍵），重建時保留原 GPIO/polarity/phase/addr 等建構參數（從 `bus.shared["SPI"]`/`["I2C"]` 讀）。
- 注意 SPI 為 TFT/SD 共用，提速期間需考量對顯示的影響。

---

## 9. 相關文件

- `02_command_index.md` — 完整指令索引（0x14xx 指令表收錄處）
- `03_notes/01_changelog.md` — 更新紀錄（本次提速更新的整合說明）
- `08_performance_benchmark.md` — 網路/協議性能基準（chunk 大小與發送約束）
- `01_nc4_protocol.md` — NC4 封包格式（speeds 指令走同一封包）
