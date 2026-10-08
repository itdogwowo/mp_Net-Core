# NC4 二進位封包協議（mp_Net-Core slave）

> **用途**：`slave/` 與 Server / PC 工具之間的二進位封包協議完整說明，包含封包格式、CRC、schema 驅動 payload、傳輸層與完整指令集。
> **對象**：任何要新增指令、解析封包、寫 PC/Server 端對接工具的人。
> **分類**：協議層（01_protocol）
> **最後更新**：2026-10（補 `0x32xx` audio 域；更正域數與指令總數）
> **實作來源**：`slave/lib/sys/proto.py`（唯一真相，以此文件描述為準）
> **相關文件**：完整指令索引見 `02_command_index.md`；性能基準見 `08_performance_benchmark.md`

---

## 1) 協議版本：NC4

```
協議代號    : NC4
SOF 標記    : b"NC" (0x4E43)
CUR_VER     : 4
Header 長度 : HDR_LEN = 9 bytes
CRC 演算法  : CRC32 (binascii.crc32)
CRC 長度    : CRC_LEN = 4 bytes
Payload 上限: MAX_PAYLOAD = 8192 bytes (純負載, 不含 header/CRC)
```

> ⚠️ **MAX_PAYLOAD = 8KB 是「約定俗成」的工程上限，不是協議的硬限制。**
> 協議欄位 `LEN` 是 uint16，理論單幀 payload 可到 65535 bytes（約 64KB）。
> 但本專案刻意把 payload 上限定在 8KB，理由：
> 1. **對端也是 ESP**：雙方記憶體/發送能力有限，>8KB 的單幀送不出也收不下。
> 2. **無線環境長幀不可靠**：一段指令太長，在 WiFi / 無線這類非可靠鏈路上更容易整幀損壞、重傳成本高。8KB 是「吞吐」與「幀損風險」之間的務實平衡點。
> 3. **現有指令都遠小於 8KB**：檔案 chunk=1~2KB、OTA chunk ≤ 8KB、控制指令都 <1KB。
>
> **實作位置與修改方法**：在 `slave/lib/sys/proto.py` 開頭改 `MAX_PAYLOAD = 8192` 一處即可。
> 這是「唯一真相源」。改完後 `StreamParser` 內部會自動加 `HDR_LEN(9) + CRC_LEN(4) = 13` bytes 建立緩衝（單幀最大 = `MAX_PAYLOAD + 13` bytes），不用再手動算頭尾。
> 所有 `StreamParser` 建立點（`slave/app.py`、`slave/tasks/web_ui.py`）都已改成引用 `MAX_PAYLOAD`，不要各自寫死數字。

> ⚠️ **與舊文件差異**：`mp_Net-Light/doc/AI_CONTEXT.md` 描述的 VER=3 + CRC16-CCITT-FALSE(2B) 是**舊版**。目前 `slave/lib/sys/proto.py` 已升級為 **VER=4 + CRC32(4B)**，header 由 10B（含 CRC16 位置）改為 **9B（HDR_LEN）+ payload + 4B CRC32**。任何新對接一律以本文件為準。

---

## 2) 封包格式

```
┌───────┬───────┬────────┬────────┬────────┬────────────┬──────────┐
│  SOF  │  VER  │  ADDR  │  CMD   │  LEN   │    DATA    │   CRC32  │
│ (2B)  │ (1B)  │  (2B)  │  (2B)  │  (2B)  │  (LEN B)   │   (4B)   │
└───────┴───────┴────────┴────────┴────────┴────────────┴──────────┘
  0-1    2       3-4      5-6      7-8      9..9+LEN-1   9+LEN..9+LEN+3
```

### 欄位說明

| 欄位 | 長度 | offset | 說明 |
|------|------|--------|------|
| SOF | 2B | 0 | 固定 `b"NC"` |
| VER | 1B | 2 | 協議版本，固定 `4`（`CUR_VER`） |
| ADDR | 2B | 3 | 目的地址（uint16 LE；`ADDR_BROADCAST = 0xFFFF` 為廣播） |
| CMD | 2B | 5 | 指令碼（uint16 LE），由 `/schema/*.json` 定義 |
| LEN | 2B | 7 | DATA 長度（uint16 LE） |
| DATA | 變長 | 9 | Payload，格式由 schema 定義 |
| CRC32 | 4B | 9+LEN | CRC32 檢查碼（uint32 LE） |

### CRC32 計算範圍

```
crc = binascii.crc32(data, 0)
data = VER + ADDR + CMD + LEN + DATA     ← 即 buffer[2 : 9+LEN]
(不含 SOF，不含 CRC32 自身)
```

實作（`proto.py`）：

```python
crc_val = Proto.crc32_update(b[2:HDR_LEN + ln], 0) & 0xFFFFFFFF
struct.pack_into("<I", b, HDR_LEN + ln, crc_val)
```

### 組包範例

```python
from lib.sys.proto import Proto

pkt = Proto.pack(0x1002, payload_bytes, addr=0xFFFF)
# ⚠️ 回傳值是「指向共享 buffer 的 memoryview」，必須立即消費（送出/寫入），
#    下一次 pack() 會覆蓋它。
```

---

## 3) 流式解析（黏包/拆包）

`StreamParser` 處理 TCP/WS 的黏包與拆包：

```python
parser = StreamParser(max_len=MAX_PAYLOAD)   # app.py / web_ui.py 都用 MAX_PAYLOAD（=8192）

parser.feed(data)                    # 收進內部緩衝（viper 加速 append/compact）
for ver, addr, cmd, payload in parser.pop():   # 生成器，撈出所有完整封包
    app.disp.dispatch(cmd, payload, ctx)
```

解析策略：

1. **找 SOF**：`buf.find(b"NC", start, end)`，錯位時自動掃描下一個標記（SOF 重同步）。
2. **驗證**：`ver != CUR_VER` 或 `ln > max_len` → 前移 1 byte 繼續找 SOF。
3. **湊齊整幀**：`(end - start) < 9 + ln + 4` → 等更多資料。
4. **驗 CRC32**：通過才 `yield`，失敗 → 前移 1 byte（視為錯位資料）。
5. **max_len 保護**：避免誤同步讀到超大 LEN 造成記憶體溢出。

`max_len` 由 `app.create_parser()` 設定為 **`MAX_PAYLOAD`（8192）**。

> ⚠️ 舊版本曾用 `config.json` 的 `Buffer.size` × 2，**該 key 已不存在**，
> 且 `Buffer.size`×2（16384×2）是現在實際值的 4 倍。以 `proto.py` 的 `MAX_PAYLOAD` 為準。

### 零拷貝快路徑：`pop_frame`（性能優化後）

- `StreamParser.pop_frame()`：解出單幀回 `(ver, addr, cmd, payload_mv)`，payload 是 `_buf` 的 memoryview（**零拷貝**），非 generator。
- `pop()`：相容介面，包 `pop_frame()` + `bytes(payload_mv)`（payload 可跨 feed 安全持有），保留給正確性測試/需跨幀持有者。
- 熱路徑（`app.handle_stream`）改用 `pop_frame`，避免每幀 `bytes()` 配置 + generator 物件引發的 GC churn（實測純解碼 8K 從 1.11 → 4.00 MB/s）。

---

## 4) Schema 驅動 Payload

### 4.1 支援的 payload 類型

`schema_loader._TYPE_CODE` 定義的 6 種類型（全部 little-endian）：

| type | type code | 大小 | 說明 |
|------|-----------|------|------|
| `u8` | 0 | 1B | 無號整數 |
| `u16` | 1 | 2B | 無號整數 LE |
| `u32` | 2 | 4B | 無號整數 LE |
| `str_u16len` | 3 | 2B 長度 + 內容 | 前綴 2B LE 長度，內容為 UTF-8 字串 |
| `bytes_fixed` | 4 | 固定 `"len": N` | 長度取自 schema JSON 的 `len` 欄位（如 sha256 = 32） |
| `bytes_rest` | 5 | 剩餘全部 | 吃掉 payload 剩餘所有 bytes，**必須放最後** |

> `SchemaCodec.encode` 也支援 `i16` / `i32` 有號整數，但 `schema_loader` 沒有對應 type code（會落成 255），decode 無法還原，目前所有 schema JSON 都只用上面 6 種。

### 4.2 Schema JSON 格式

```json
{
  "group": "sys",
  "cmds": [
    {
      "cmd": "0x1002",
      "name": "SLAVE_ANNOUNCE",
      "payload": [
        {"name": "slave_id",   "type": "str_u16len"},
        {"name": "pixel_count","type": "u16"},
        {"name": "hw_version", "type": "str_u16len"}
      ]
    }
  ]
}
```

- `cmd` 支援 `"0x1001"`（hex 字串）或十進位字串。
- `SchemaStore.load_dir("/schema")` 依檔名排序載入所有 JSON；`finalize()` 依 cmd id 排序建 `dispatch_buf`（每筆 8B）+ `field_buf`（每欄 2B）+ `field_names`，供 viper 加速解碼。
- 重複 cmd id 時，後載入者覆寫。

### 4.3 編解碼 API

```python
from lib.sys.schema_loader import SchemaStore
from lib.sys.schema_codec import SchemaCodec

store = SchemaStore(); store.load_dir("/schema"); store.finalize()

cmd_def = store.get(0x1002)                    # 依 cmd int 取得定義（沒有 get_cmd!）
payload = SchemaCodec.encode(cmd_def, {"slave_id": "ESP32_A1B2", ...})
args    = SchemaCodec.decode(cmd_def, payload, store)   # 回傳 dict，恆含 _name / _cmd
```

decode 邊界行為（對接工具需注意）：

- 欄位不足（如 u32 需要 4B 但只剩 2B）→ 該欄位不寫入 dict，不 raise。
- `bytes_rest` 即使長度 0 也會寫入（回傳空 memoryview）。
- 未定義的 type code 在 decode 無分支 → 該欄位不寫入、offset 不推進，可能造成後續錯位。

---

## 5) 傳輸層

同一套 NC4 封包可跑在 5 種傳輸上，接收端統一寫進各 bus 的 `AtomicStreamHub`（見 `doc/03_notes/02_buffer_architecture.md` L2）：

| bus | 檔案 | 傳輸 | 用途 |
|-----|------|------|------|
| `CTRL-WS` | `lib/sys/net_bus.py` (TYPE_WS) | WebSocket | 主控制通道（指令 + 回覆 + 串流） |
| `UDP-DISCV` | `lib/sys/net_bus.py` (TYPE_UDP) | UDP | 發現/廣播（`discovery_port` = 9000） |
| TCP | `lib/sys/net_bus.py` (TYPE_TCP) | TCP | 備用/直連 |
| `CIRCUIT` | `lib/sys/circuit_bus.py` | UART 實體線 | 有線控制（`CircuitDecode` 設定） |
| `NOW-Bus` | `lib/sys/now_bus.py` | ESP-NOW | 無 WiFi 的短距離控制 |

每個 bus 的 rx_hub slot 佈局（收進 ring 後，`BusDecodeTask` 讀取時剝離）：

```
[0:2] = u16 LE 資料長度 n | [2:2+n] = NC4 封包資料
```

### 定址模型（cID / master_cid）

#### ★★ 定址詞彙表（**唯一命名依據，寫程式前先看這裡**）

位址太多，所以名字一律遵守 **`<誰>_<哪層>`**：

| 軸 | 值 |
|---|---|
| **誰** | `master`（我的上級）／ `src`（**這一幀的來源**）／ `dst`（目的地）／ **省略 = 我自己** |
| **哪層** | `cid`（**協議層**，2 B，填在幀頭 `addr`）／ `mac`（**射頻層**，6 B，送出去用） |

| 名字 | 哪一層 | 誰的 | 一句話 |
|---|---|---|---|
| `cid` | 協議 | 我自己 | 我的協議短位址（`bus.cid`）|
| `slave_id` | — | 我自己 | **就是我的 MAC**（`machine.unique_id()` 的 hex）。⚠️ 歷史名，見下 |
| `master_cid` | 協議 | 上級 | 我上級的協議位址；**未設 = `0xFFFF`** |
| `master_mac` | 射頻 | 上級 | 我上級的 MAC；其他管子可為 `None` |
| `reply_cid` | 協議 | 掃描者 | `0x100D` 的欄位：**這一封** `0x100E` 要回給哪個 cid |
| `addr` | 協議 | — | **NC4 幀頭欄位**（2 B），內容是一個 `cid` |
| `src_mac` | 射頻 | 這一幀 | 這一幀**從哪個 MAC 來**（射頻層附帶，不用對方講）|
| `last_src_mac` | 射頻 | 上一幀 | 「剛剛講話的那個人」（`NowBus._last_src_mac`）|
| `dst_mac` | 射頻 | 目的地 | 要送去哪個 MAC（`write_to(dst_mac, frame)`）|
| `ADDR_BROADCAST` | 協議 | — | `0xFFFF`（協議層廣播）|
| `BCAST_MAC` | 射頻 | — | `FF:FF:FF:FF:FF:FF`（射頻層廣播）|

**三個一定要分清楚的**：

1. **`cid` 不是 `mac`** —— 一個 2 B 協議位址、一個 6 B 射頻位址，活在兩層，**不可互推**
   （cid 可以手寫，面板就是 `"0001"`）。
2. **`src_mac` 不是 `master_mac`** —— 前者是「**這一幀**誰講的」，後者是「**我上級**是誰」。
   兩者只有在「上級剛好正在講話」時才相同。**延後發射（抖動）時必須用前者**，
   因為那時「剛剛講話的人」早就換了。
3. **`slave_id` 就是 MAC** —— 它是 `machine.unique_id()` 的 hex，**真機驗證與 ESP-NOW 的
   STA MAC 相同**。保留這個歷史名是因為它用在 heartbeat / `0x100E` / peers 表 key 等
   很多地方；**看到它就當成「這台的 MAC」**。

> **`0x100D.reply_addr` 已於 2026-10 更名為 `reply_cid`** —— 舊名看不出它帶的是 cid。
> **wire 不變**（schema 欄位名不上線，只有順序與型別上線），只有原始碼／文件的名字變。

#### ⚠️ 常見誤解：`reply_cid` 為什麼不是 `reply_mac`？

問法通常是：「掃描要回覆，不就需要 MAC 嗎？沒有 MAC 怎麼送得回去？」

**答案：需要 MAC，但那個 MAC 不走 payload —— 它由射頻層免費附帶。**

| | 誰決定 | 掃描回覆的值 |
|---|---|---|
| **協定 `addr`**（幀頭）| `reply_cid`（payload 帶的）| 掃描者的 cid → 讓**別台把這封丟掉** |
| **射頻去向**（`dst_mac`）| `src_mac`（射頻層學的）| 掃描者的 MAC → **送得到** |

```python
# now_bus.poll() —— 每一幀到達時自動發生，這就是「配對 MAC 的方法」
peer, msg = self._esp.recv(0)      # peer = 來源 MAC（不用對方講）
self.learn_peer(peer)              # add_peer(mac) → 通道建立
```

**為什麼這欄位不能改成 MAC（三個理由，第一個是決定性的）：**

1. ★ **UART / RS485 根本沒有 MAC** —— 共用匯流排上沒有「射頻來源」可學，
   而廣播掃描的幀頭 `addr` 是 `0xFFFF` → **`reply_cid` 是唯一能知道「回給誰」的途徑**。
   改成 MAC，整條流程在共用匯流排上就實作不出來（本設計的前提是**傳輸層無關**）。
2. **payload 裡的 MAC 可以說謊** —— 射頻來源是發送者的實際位址，payload 只是對方填的字串。
   用 payload MAC 當目的地＝任何人都能叫你去打第三台。
3. **兩個位址本來就活在兩層**（見上表），硬塞成一個會讓「哪一層」消失。

**什麼時候真的需要「存下來的 MAC」？** —— **主動發送**時（`0x1002` 公告、心跳）：
那條路**沒有幀進來**，所以沒有 `src_mac` 可學 → **只能靠 `master_mac`**。
`master_mac` 的存在就是為了這個（它取自 `0x1016` 收幀當下的 `src_mac`）。

**「沒有 master 就只能回 FF」？** 只有**協定 `addr`** 會 FF（`bus.master_cid` 初值）；
掃描這條路徑有 `reply_cid` 擋著 → **不會 FF**，射頻去向也從來不是 FF。

> 端到端證據：`test/protocol/test_node_pairing.py` **§16**（10 項，只換掉射頻與時鐘，
> 其餘走真程式碼：`poll()` → `rx_hub` → `_src_mac` → `_TxOut` → `defer` → `_fire_due` → `send`）。

#### 個別成員

- **`bus.cid`（uint16）**：裝置自身的協議短身份，由 `ConfigManager.ensure_cID()` 於 T0（boot.py import 時）建立——`System.cID` 為空時以 `machine.unique_id()` 末 4 碼填入並持久化；取不到則 `"FFFF"`。cID 是**單一擁有**、由 ConfigManager 推動，消費者（解碼層）只讀不重算。
- **`bus.master_cid`（uint16）**：回應定址目標，預設 `0xFFFF`（廣播=未設定）。**只有 `0x1016 SET_MASTER` 能改它**（+ 開機 `load_node()` 還原 / 解除 `clear_node()`）——slave 記住後，所有回應的 `addr` 欄位預設都填 `bus.master_cid`。**會落盤（btree `@node.master_cid`），重開機仍在**。
  - ⚠️ `0x100D IDENTIFY_REQ` 的 `reply_cid` **不再改這裡**。它只是**那一封** `0x100E` 的回信位址（`_reply(..., addr=reply_cid)` 的單次參數）。以前它會寫進 `master_cid`（因為 `_reply()` 沒有目的位址參數），副作用是任何一次廣播掃描都等於「認掃描者當 master」＝無聲搶奪；該寫入已移除。回覆 addr 的覆寫值優先於 `master_cid`。
  - **「不改方向」≠「回不去」**：掃描回覆的**射頻去向**是「這一幀的來源」（`src_mac`），
    與 `master_cid` 無關 → 未配對的裝置照樣回得去（協定 addr 就是 `0xFFFF`）。
- **`bus.master_mac`（hex 字串或 `None`）**：上級的射頻位址。**不是用 payload 傳的** ——
  取自 `0x1016` **收幀當下的 `src_mac`**（與 `0x100E` 帶回 cid 的方式完全對稱）。
  用途：`NowBus.write()` 解析 `addr == master_cid` 時用它；也是主動發送的目的地。
- **ADDR 過濾（`app.py` `handle_stream`）**：只收 `addr == ADDR_BROADCAST(0xFFFF)` 或 `addr == bus.cid` 的幀，其餘 `continue` 丟棄。這讓「逐 address 掃描」的 RX 端現成可用。

---

## 6) 完整指令集

指令碼分配（依 `slave/schema/` 實際內容；**12 個域、112 條指令**）：

```
0x10xx — sys         系統發現/控制/任務管理/定址/遠端更新
0x11xx — status      狀態與設定（取 / 設 / 存）
0x12xx — heartbeat   心跳
0x13xx — now         ESP-NOW
0x14xx — hw          硬體控制 + 臨時提速
0x15xx — waiting_to_trash  待清理功能
0x16xx — （已移除）  原 Router 執行期指令，職責併入 0x11xx + config
0x18xx — bench       性能測試（通用接收吞吐）
0x20xx — file        檔案傳輸/查詢
0x22xx — ota         韌體 OTA（合作方合同）
0x30xx — stream      pixel 串流
0x31xx — pixel      模式播放（LED/SERVO）
0x32xx — audio       音訊播放（WAV 串流，dj_task）
```

> - `0x16xx` 已於 2026-09 移除，編號保留不再使用（歷史說明見 `02_command_index.md` §7）。
> - `0x32xx` 音訊域的完整指令表見 `02_command_index.md` §13；設計定案見
>   `03_notes/13_audio_wav_stream_plan.md`、使用說明見 `02_guides/13_audio_wav_module.md`。

> 各域詳細指令表已收錄在 `02_command_index.md`，本文件不再重複列出，直接前往查詢。

---

## 7) 新增指令流程

新增一個指令需要修改 **4 個位置**（詳細見 `Skills/mp-netcore`）：

1. **`/schema/<group>.json`**：在 `cmds` 陣列新增 cmd 定義（或建新 JSON）。
2. **`/action/<group>_actions.py`**：寫 `on_xxx(ctx, args)` handler + 在 `register(app)` 註冊 `app.disp.on(0xXXXX, on_xxx)`。
3. **`/action/registry.py`**：import 新模組並呼叫 `register(app)`。
4. **（可選）** `/tasks/` + `main.py` 或 `Core0.py`：若有背景任務。

驗證：

```python
# 離線 loopback
pkt = Proto.pack(0xXXXX, SchemaCodec.encode(cmd_def, {...}))
app.handle_stream(parser, pkt, transport_name="Test", send_func=print)
```

---

## 8) 與 mp_Net-Light 協議對照

| 項目 | mp_Net-Light（AI_CONTEXT.md 舊版） | mp_Net-Core（NC4，目前） |
|------|----------------------------------|----------------------|
| VER | 3 | **4** |
| Header | 2+1+2+2+2 = 9B（不含 CRC） | 2+1+2+2+2 = **9B** |
| CRC | CRC16-CCITT-FALSE，2B | **CRC32（binascii.crc32），4B** |
| CRC 範圍 | VER..DATA | **VER..DATA（buffer[2:9+LEN]）** |
| 指令域 | 0x10xx sys / 0x11xx status / 0x12xx heartbeat+fs / 0x20xx file / 0x30xx stream | 0x10xx sys / 0x11xx status / 0x12xx heartbeat / 0x13xx now / 0x14xx hw / 0x15xx wtt / 0x18xx bench / 0x20xx file / 0x22xx ota / 0x30xx stream / 0x31xx pixel / **0x32xx audio**（共 12 域；`0x16xx` 已移除） |
| Payload 類型 | 同 | 同（**schema 實際只用 6 種**：u8/u16/u32/str_u16len/bytes_fixed/bytes_rest。`i16/i32` 只有 `SchemaCodec.encode` 支援，schema JSON 沒有對應 type code，見 §4.1 註記） |

> `mp_Net-Light` 的 `ADD_NEW_CMD_FLOW.md` / `RUN_NETWORK_SERVER.md` 描述的組包/解析流程與本專案相同，只差 VER/CRC 常數。對接工具請以 `slave/lib/sys/proto.py` 為準。

---

## 9) 相關檔案

- `slave/lib/sys/proto.py` — 封包打包/解析（唯一真相）
- `slave/lib/sys/schema_loader.py` — Schema 載入/排序/type code
- `slave/lib/sys/schema_codec.py` — payload 編解碼
- `slave/lib/sys/dispatch.py` — cmd → decode → handler
- `slave/lib/sys/net_bus.py` / `circuit_bus.py` / `now_bus.py` — 傳輸層
- `slave/schema/*.json` — 指令定義
- `slave/action/*.py` — handler 實作
- `slave/app.py` — 裝配（create_parser / handle_stream）
