# ESP Stream Controller 設計（板子自己當主控）

> **用途**：規劃「讓 ESP32-S3 板子直接對其他裝置下指令」的架構——指令收發模型、排程策略、任務設計與實作分期。任何要動 `slave/host/`、新增主控用指令的人，先讀這份。
> **分類**：筆記（03_notes）
> **狀態**：規劃中（尚未實作）
> **最後更新**：2026-09-15
> **相關文件**：[多級緩衝架構](02_buffer_architecture.md)、[核心實例](09_cores.md)、[NC4 協議](../01_protocol/01_nc4_protocol.md)、[完整指令索引](../01_protocol/02_command_index.md)、[檔案更新流程](../02_guides/10_file_update.md)、[上傳 WDT 診斷](12_upload_wdt_diagnosis.md)
> **對照實作**：`tools/PC/NetBusMaster.py`（PC 端主控，9,800 行／398KB）——**不是要移植的程式碼，是行為規格書**

---

## 1) 這份文件在解什麼問題

### 1.1 起點

`tools/PC/NetBusMaster.py` 是目前唯一的主控程式：跑在 PC 上，用 CPython 的
`threading` + `http.server` + 阻塞 socket，對 N 台裝置下指令、派檔、同步播放。
它已經穩定，但**現場一定要有一台 PC**。

### 1.2 目標

把「下指令」這件事搬到板子上：**一台 ESP32-S3 直接對其他裝置發指令**，
不依賴 PC 就能完成「發現 → 查詢 → 準備 → 同步起播 → 停止」。

### 1.3 不是目標

| 不做 | 原因 |
|---|---|
| 移植 `NetBusMaster.py` 的程式碼 | CPython 專屬（threading／http.server／阻塞 socket／miniaudio），MicroPython 沒有 |
| 在板子上做音訊解碼、PXLD 動畫生成 | 記憶體與 CPU 不划算；動畫仍由 PC 產出，板子負責搬運與下令 |
| 在板子上做完整網頁控制台 | 用板子現成的 `WebUITask` 擴充一頁（見 §9） |
| 為「多個主控」設計協商機制 | 沒有這種需求；裝置只認最後一個告訴它的主控（見 §2.3） |

---

## 2) 立論基礎：沒有角色，只有指令

### 2.1 協議本身就框定了行為

NC4 是**指令／回應**協議：每一條 CMD 都有定義好的 payload，收到什麼就回什麼。
所以「主控」與「被控」**不是兩種角色，而是兩種行為**：

- 發指令的一方：把 CMD 打包送出，等對應的回應。
- 收指令的一方：解出 CMD，執行，回對應的回應。

同一台裝置**兩邊都會**——它本來就在做「收指令 → 執行 → 回回應」，
現在只是多學會「發指令 → 等回應」。

> 這個觀點直接簡化了架構：**不需要 `Role.mode`、不需要角色互斥、不需要模式切換**。
> 只需要多一個 task 負責「發指令」與「收回應」。

### 2.2 master 的工作本質只有兩件事

PC 主控的 9,800 行拆開來看，真正與「下指令」有關的只有：

| 能力 | PC 的做法 | 板子上 | 狀態 |
|---|---|---|---|
| **發指令** | `Proto.pack` + `SchemaCodec.encode` | `slave/lib/sys/proto.py`、`schema_codec.py` | ✅ **已經有了** |
| **收回應** | `StreamParser.pop()` + 查 schema `decode` | 同上 | ✅ **已經有了** |
| 連線管理 | `start_ws_server` + 每 client 一 thread | — | ❌ 要新寫（變成 task） |
| 指令佇列 | 主執行緒順序呼叫 | — | ❌ 要新寫（變成狀態機） |
| 裝置清單 | `slave_map.json` | — | ❌ 要新寫（`/host_map.json`） |

**結論：板子早就是「協議專家」，缺的只是「一次面對很多台」的那一層。**
協議層零重寫。

> 這也是為什麼 `slave/host/` 只准 import `lib/sys/proto`、`schema_loader`、`schema_codec`，
> **不碰 `action/` 與 `tasks/`**——避免自己發出的指令繞回自己的執行層。

### 2.3 唯一需要的「方向」概念：回應往哪裡回

裝置把回應送到 `bus.master_cid`（由 `0x1016 SET_MASTER` 或 `0x100D IDENTIFY_REQ.reply_addr` 設定）。

- 對**同一個網路介面**上的裝置來說，誰最後告訴它「我是你的 master」，回應就回給誰。
- 因此板子要下令前，先對目標發 `0x1016 SET_MASTER`，把回應指向自己。
- 這是**單值、最後寫入者勝**，不做協商——若 PC 與板子同時下令，就是互相搶，
  屬於操作紀律問題，不是程式問題。

---

## 3) 指令收發模型

### 3.1 封包與定址

```
┌───────┬──────┬───────┬───────┬───────┬──────────┬─────────┐
│  SOF  │ VER  │ ADDR  │  CMD  │  LEN  │   DATA   │  CRC32  │
│  (2B) │ (1B) │ (2B)  │ (2B)  │ (2B)  │ (LEN B)  │  (4B)   │
└───────┴──────┴───────┴───────┴───────┴──────────┴─────────┘
```

- `ADDR` = 目的位址（uint16 LE），`0xFFFF` = **廣播**。
- 接收端過濾規則（`slave/app.py::handle_stream`）：**只收 `ADDR == 0xFFFF` 或 `ADDR == 本機 cID`**。

### 3.2 兩個推論（本設計的兩根柱子）

**推論 A：同步播放天生就是廣播，不需要逐台 ACK。**

`0x300A STREAM_PLAY(start_frame)` 的語義是「從第 N 幀開始，之後用你自己的節拍跑」——
節拍來自裝置本地 config 的 `System.frame_interval_ms`（`tasks/stream_task.py` 已確認）。
因此：

```
廣播 0x300A(start_frame=N, addr=0xFFFF)  →  N 台同時起播
```

一條封包、零 ACK、零逐台等待。這不需要改協議、不需要改韌體。
逐台往返只有在「準備階段」才需要。

**推論 B：回應不帶來源 id → 回應歸屬只能靠 socket。**

NC4 回應沒有「我來自誰」的欄位。所以紀律是：

| 指令性質 | 走法 | 理由 |
|---|---|---|
| 要回應的（查詢、準備、檔案） | **一對一鏈路** | 哪條 socket 回來的，就是那台 |
| 不要回應的（播放時序） | **廣播** | 不需要歸屬，也就沒有歸屬問題 |

> 若未來真的需要「單 socket 對多裝置」，得在協議加欄位——**本計劃明確不動協議**。

### 3.3 三種送出方式（取代「角色」）

| 送出方式 | 實作 | 用在哪 |
|---|---|---|
| **廣播** | 對每條已知鏈路發同一幀（`ADDR=0xFFFF`） | 播放時序、全體停止 |
| **單播** | 該裝置的專屬鏈路（`ADDR=cid` 或 `0xFFFF`） | 查詢、準備、檔案 |
| **本地注入** | 寫進 `CircuitBus(None)` 的 `rx_hub` → `BusDecodeTask` 正常解碼 | 板子自己也要跑同一條指令 |

本地注入沿用 `tasks/schedule.py::_inject()` 已驗證的做法（vBus 是任務自己建立的內部虛擬總線，
唯一寫入者就是自己，無競爭）。**播放時本機與其他台同時起跑，誤差最小。**

---

## 4) 指令方向表

「我們發什麼、預期收什麼」是本設計的核心，比任何類別圖都重要。

### 4.1 本機發出（→ 其他裝置）

| CMD | 名稱 | 方向 | 送出方式 | 預期回應 | 階段 |
|---|---|---|---|---|---|
| `0x1001` | DISCOVER | 廣播 / 單播 | UDP 9000 | 裝置主動回連 | P2 |
| `0x100D` | IDENTIFY_REQ | 單播 | WS | `0x100E` IDENTIFY_RSP | P2 |
| `0x1016` | SET_MASTER | 單播 | WS | 無 | P2 |
| `0x100A` | TIME_SYNC | 單播 | WS | `0x100B` TIME_SYNC_RSP | P2 |
| `0x1003` | SYS_INFO_GET | 單播 | WS | 無（僅裝置端 log） | P3 |
| `0x1101` | STATUS | 單播 | WS | `0x1102` STATUS_RSP | P3 |
| `0x3001` | STREAM_INFO | 廣播 | WS | 無 | P3 |
| `0x3009` | STREAM_STATE_SET | 單播 | WS | `0x3008` STREAM_READY_ACK | P4 |
| `0x300A` | STREAM_PLAY | **廣播** | WS + 本地注入 | 無 | P4 |
| `0x3005` | STREAM_PAUSE | **廣播** | WS + 本地注入 | 無 | P4 |
| `0x3002` | STREAM_STOP | **廣播** | WS + 本地注入 | 無 | P4 |
| `0x2001` | FILE_BEGIN | 單播 | WS | `0x2004` FILE_ACK | P5 |
| `0x2002` | FILE_CHUNK | 單播 | WS | `0x2004` FILE_ACK（逐塊） | P5 |
| `0x2003` | FILE_END | 單播 | WS | `0x2004` FILE_ACK | P5 |
| `0x2005` | FILE_QUERY | 單播 | WS | `0x2006` FILE_QUERY_RSP | P5 |
| `0x2008` | FILE_CONFIRM | 單播 | WS | `0x2004` FILE_ACK | P5 |

### 4.2 本機接收（← 其他裝置 / 原本就在做的）

| CMD | 名稱 | 來源 | 本機處理 |
|---|---|---|---|
| `0x1001` | DISCOVER | 其他主控 | 現有 `action/sys_actions.py`（維持不變） |
| `0x1002` | SLAVE_ANNOUNCE | — | 現有 |
| `0x1005`~`0x1007` | SYS_TASK_* | 其他主控 | 現有 |
| `0x1016` | SET_MASTER | 其他主控 | 現有（會被覆蓋，見 §2.3） |
| `0x1201` | HEARTBEAT | 本機**主動送** | `action/heartbeat_actions.py`（維持） |
| `0x3009`/`0x300A`/`0x3005`/`0x3002` | 串流控制 | 其他主控 | 現有 `tasks/stream_task.py`（維持不變） |

> **重點：4.2 一行都不改。** 本計劃只新增「發出」的能力，不碰既有「接收」路徑。

### 4.3 明確不做

| PC 功能 | 為什麼不做 |
|---|---|
| MP3 解碼／播放 | 交給有 I2S 的裝置（`0x3201 AUDIO_SET`） |
| PXLD 亮度重算／動畫生成 | 記憶體與 CPU 不允許 |
| `web_remote.html` 整套前端 | 用 `WebUITask` 擴充一頁（§9） |
| keepalive 守護／`subprocess` 重啟 | 沒有 OS 概念；WDT 就是板子的守護 |
| 中途加入（mid-join）追幀 | 那是 PC 為「裝置掉線重連」設計的；現場只要「全部重新起播」 |
| 多主控協商 | 沒有需求（§2.3） |

---

## 5) 記憶體：定額，不是成長

### 5.1 NodeTable 幾乎不佔記憶體

裝置清單每筆只需要：

```
cid (u16) + ip (str) + port (u16) + play_id (u8) + 最後狀態 (u8)  ≈ 數十 bytes
```

10 台 ≈ 幾 KB。**清單本身不是問題。**

### 5.2 真正吃記憶體的是「鏈路」

`NetBus` 是一條連線一個實例，每條的固定成本：

| 項目 | 大小 | 備註 |
|---|---|---|
| `NetBus._buf` | 4115 B | `RX_BUF_SIZE`（必須容納最大 NC4 幀：8192 payload + 13） |
| `NetBus._drop_buf` | 2048 B | `min(2048, RX_BUF_SIZE)` |
| `rx_hub` | `(4115+2) × slots` | 預設 `net_rx_slots=2` → 8.2 KB |
| `cache_hub` | 0 | **惰性建立**：只有呼叫 `read_into()` 才會配；本設計不呼叫 |
| **合計** | **≈ 14 KB / 條** | |

**可最佳化**：`NetBus.__init__` 支援傳入外部 `rx_hub`。回應都是小封包（STATUS/ACK/READY），
所以每條鏈路可以給**小 hub**（例如 `AtomicStreamHub(1024, num_buffers=2)` ≈ 2 KB）：

```
最佳化後 ≈ 4.0 + 2.0 + 2.0 ≈ 8 KB / 條
```

### 5.3 為什麼總量是固定的（關鍵洞察）

| 操作 | 並發度 | 記憶體 |
|---|---|---|
| 查詢／準備 | 一次一筆（Executor 序列） | 一個 pending buffer |
| **上傳檔案** | **一次一台、一台內一次一塊** | **一個 chunk buffer** |
| 播放 | 廣播，零 buffer | 零 |

所以**沒有指數成長**：鏈路數是定額，工作緩衝區是常數（1 塊）。
總量 = `鏈路數 × 8~14KB + 常數緩衝`，落在一個可預測的範圍內。

> 因此**不需要**動態成長／輪詢降級那套機制。
> 需要調的只有一個旋鈕：**同時維持幾條鏈路**（依現場台數與實測 `gc.mem_free()` 定）。

### 5.4 P0 要量的數字（不要用猜的）

1. 開機後 `gc.mem_free()`
2. `StreamTask` + `pixel_stream` hub 上線後的 `gc.mem_free()`（這才是可用的預算）
3. 一條鏈路建好後的實際差值（驗證 §5.2 的估算）

---

## 6) 排程策略：三類指令

**「哪些可以並發、哪些必須排隊」——答案是後者。**

| 類別 | 代表指令 | 送出方式 | 並發度 | 等待 |
|---|---|---|---|---|
| **A. 時序** | `0x300A` / `0x3005` / `0x3002` | 廣播 + 本地注入 | 1 條封包 | **不等回應** |
| **B. 查詢／準備** | `0x100A`、`0x1101`、`0x3009` | 單播 | 同時 1 筆 | 等回應，逾時標失敗 |
| **C. 檔案** | `0x2001` / `0x2002` / `0x2003` | 單播 | 同時 1 筆、一次一塊 | 每塊等 `0x2004` |

### 6.1 為什麼 B/C 必須一次一筆

- **記憶體**：並發 N 台的 chunk buffer 沒有必要（上傳本來就是一台一台做）。
- **WDT**：`System.watchdog.timeout_ms = 8000`，且 `TaskManager.runner_loop` 會在 boot 完成後 lazy-arm。
  一次 loop 內跑完整個檔案傳輸 = 8 秒到就重置板子（`doc/03_notes/12_upload_wdt_diagnosis.md` 已吃過這個苦）。
- **socket 節奏**：lwIP 的 `SEND_CAP = 4096`；同時對多條 socket 猛灌會互相排擠，反而全慢。

### 6.2 非阻塞紀律（TaskManager 的硬約束）

`TaskManager.runner_loop` 是**協作式單執行緒**：所有 task 的 `loop()` 輪流跑，**不能阻塞**。

| 規則 | 數值 | 說明 |
|---|---|---|
| 單輪 `loop()` 目標 | < 5 ms | 超過會拖慢 `RenderTask`（20ms 節拍）與 `StreamTask` |
| 單次最長阻塞 | < 100 ms | `NetBus.connect()` 內部 `settimeout(5)`；**連線要分段做，不能一次連 N 台** |
| 每輪處理上限 | 1 個 pending 動作 | 狀態機推進一格就讓出 |
| GC | 每輪不配置大物件 | 沿用專案慣例：memoryview + 預配置 buffer |

---

## 7) 模組設計

```
slave/host/                        ← 新增
├── __init__.py
├── stream_controller.py           StreamControllerTask(Task)：
│                                     on_start 建鏈路、loop 推進指令佇列
├── peer_link.py                   PeerLink：一條鏈路 = NetBus(TYPE_WS) 包裝
│                                     connect / poll / send(cmd, args, addr) / 回應表
└── node_table.py                  裝置清單 + /host_map.json 持久化 + DISCOVER 廣播／敲門
```

**單檔也可以**：三者合計若在 600 行內，合成一支 `stream_controller.py` 更符合專案慣例
（`tasks/*.py` 多為單檔 300~900 行）。**先寫在一起，超過再拆。**

### 7.1 PeerLink 的責任邊界（重要）

| 做 | 不做 |
|---|---|
| 建連線、斷線重連 | 不註冊進 `bus_sources` |
| 送 NC4 幀（含 WS 幀頭） | 不呼叫 `app.handle_stream` |
| 收幀、`StreamParser` 解析、查 schema decode | 不碰 `action/` |
| 記錄 pending（送出什麼、等什麼回應） | 不持有跨 loop 的大 buffer |

> **為什麼不進 `bus_sources`**：`BusDecodeTask` 會把 `bus_sources` 裡每個來源的資料餵給
> `app.handle_stream`，而那裡有 **ADDR 過濾**（只收 `0xFFFF` 或本機 cID）。
> peer 回應若走這條，會被丟棄或被當成本機指令執行。PeerLink **自己解析**才乾淨。

### 7.2 與既有程式的關係

- `slave/tasks/*`、`slave/lib/sys/net_bus.py` **一行都不改**（`NetBus` 是通訊命脈）。
- `Core_Manager.py` 只加**一行註冊**（比照其他 task），不需要模式分支。
- 新 task 可被 `0x1007 SYS_TASK_SET` 動態開關（既有機制，免費得到）。

---

## 8) 關鍵流程

### 8.1 開機

```
1. 讀 /host_map.json → 裝置清單（已知 cid / 上次 IP / port）
2. 建鏈路（依清單，逐條建、每條之間讓出 loop）
3. DISCOVER 廣播（UDP 9000，帶自己的 IP + WS port）→ 新裝置回連
4. 對每台：0x1016 SET_MASTER（回應指回自己）→ 0x100A 量延遲 → 記進清單
5. 進入 loop()
```

> ⚠️ 第 3 步有**方向問題**：現行韌體的裝置收到 DISCOVER 後會**主動連回** `ws_url`，
> 而板子目前**沒有 WS server**（只有 `WebUITask` 的 HTTP）。兩條路見 §11。

### 8.2 同步起播（核心流程）

```
階段 1  準備（逐台、序列、要回應）
  for each peer:
      send 0x3009 STREAM_STATE_SET(file_name, block_id=0, play_mode)
      wait 0x3008 STREAM_READY_ACK          ← 一次一筆
      （逾時 → 該台標「未就緒」，不阻擋其他台，但要在結果裡列出）

階段 2  對時（選配，逐台）
  send 0x100A TIME_SYNC → 收 0x100B → 算單向延遲 → 換算前導幀

階段 3  起播（廣播、不等回應）
  send_broadcast 0x300A STREAM_PLAY(start_frame)
  ＋ 本地注入（板子自己也要跑同一條）

階段 4  收尾
  send_broadcast 0x3002 STREAM_STOP（延遲 post_play_stop，
                                      讓最後一幀定格 —— 裝置端行為已存在）
```

`start_frame` 的補償沿用 PC 版語義：`play_fps × 單向延遲 + 前導幀`。

### 8.3 查詢狀態（Executor 的典型用法）

```
佇列：[ (A, 0x1101), (B, 0x1101), (C, 0x1101) ]
每輪 loop：推進 1 格（送出 → 等 0x1102 → 記錄 → 下一台）
逾時 1.0s（可配置）→ 該台標「無回應」，繼續下一台（不整批失敗）
```

### 8.4 檔案上傳（一次一台、一台內一次一塊）

```
for each target:                        ← 外層序列
    send 0x2001 FILE_BEGIN(size, chunk_size, sha256, path)
    wait 0x2004 FILE_ACK                ← 建立檔案、回可用 offset
    off = 0
    while off < size:                   ← 內層逐塊
        read chunk（本地檔案 / SD）
        send 0x2002 FILE_CHUNK(file_id, offset=off, data)
        wait 0x2004 FILE_ACK            ← 每塊一等
        off += len(chunk)
        （每輪 loop 只推一格；不讓出就會踩 WDT）
    send 0x2003 FILE_END(file_id, sha256)
    wait 0x2004 FILE_ACK
```

---

## 9) 操作介面

沒有 PC，指令從哪裡來？

| 介面 | 做法 | 階段 |
|---|---|---|
| **板子 WebUI** | `WebUITask` 已有 `/api/cmd`（POST JSON → `disp.dispatch`）。新增 `/api/host` 端點，把 `{action, ...}` 丟進指令佇列 | P3 |
| 實體按鍵／編碼器 | `ControlPanelTask` 已有按鈕事件鏈；新增按鍵映射 | P5（選配） |
| 開機巨集 | `/host_macro.json` 定時序列，沿用 `tasks/schedule.py` 的 `{ms, cmds}` 格式與 vBus 注入 | P4（選配） |

> ⚠️ `/api/cmd` 是「**對自己**下指令」（走 `app.disp.dispatch`），
> 與「**對其他裝置**下指令」（走指令佇列 → PeerLink）是兩件事，**必須分成兩個端點**，不要混用。

---

## 10) 分期計畫

| 階段 | 內容 | 驗收標準 | 估時 |
|---|---|---|---|
| **P0 量測** | 量 §5.4 的三個數字；量 `0x300A` 廣播對 2 台的實際同步誤差 | 有實測數字寫回本文件 §5 | 0.5d |
| **P1 骨架** | `PeerLink` + `StreamControllerTask` 空殼 + `Core_Manager` 註冊一行 | 板子連上另一台 → 發 `0x100A` → 收到 `0x100B` 並印出；TaskManager 面板看得到 task、`loop` 時間正常 | 1d |
| **P2 叢集** | `node_table` + DISCOVER 廣播／敲門 + `SET_MASTER` | 開機能帶起 ≥ 3 台；拔電重插自動回復 | 1~1.5d |
| **P3 指令層** | 指令佇列狀態機（序列、逾時、重試）+ `/api/host` | WebUI 一顆按鈕 → 對全部裝置查詢 → 結果上表 | 1.5d |
| **P4 同步播放** | 巨集（§8.2）+ 本地注入 | 3 台以上同步起播，**誤差 < 1 幀**；停止、暫停正確 | 1d |
| **P5 檔案搬運（選配）** | `0x2001/2/3` 序列分塊 + 餵狗 + 斷點續傳 | 從本機派一個動畫到全部裝置，過程中 WDT 不觸發 | 2~3d |
| **P6 韌性（選配）** | 心跳、半開偵測（`NetBus.idle_ms()` 現成）、離線重連、狀態上 WebUI | 無 PC 環境連跑 8 小時不掉線 | 1d |

**P0~P4 是「可用」的最小集合（≈ 5 天）。**

---

## 11) 風險與待確認事項

| # | 風險／未知 | 影響 | 緩解 |
|---|---|---|---|
| R1 | **裝置不會自己連回來**（板子沒有 WS server） | P2 的裝置發現走不通 | 見下方決策點 |
| R2 | 鏈路數 × 8~14KB 超出可用 heap | 可帶的台數少 | P0 先量；旋鈕只有「同時幾條鏈路」一個（§5.3） |
| R3 | 回應歸屬依賴「一 socket 一裝置」 | 未來擴充會卡住 | 協議層既有約束；要改就是改協議 |
| R4 | 檔案搬運期間 WDT | 板子被重置 | 嚴格分塊 + 每輪讓出 + 不並發 |
| R5 | `bus.master_cid` 是內存值、最後寫入者勝 | PC 與板子同時下令會互搶 | 開機流程固定先發 `0x1016`；屬操作紀律 |

### 決策點（開工前必須定案）

**R1 的兩條路：**

| 選項 | 做法 | 代價 |
|---|---|---|
| **R1-a** | 板子實作**最小 WS server**：讀 HTTP header → 回**固定**的 `Sec-WebSocket-Accept`（裝置端用的 key 是寫死的 `dGhlIHNhbXBsZSBub25jZQ==`，回應必然是常數 `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`，不必算 SHA1）→ 只需 unmask 入向幀 | 約 80 行 |
| **R1-b** | 放棄「裝置連回來」，全**主動撥出**到已知 IP 清單 | 不需 server，但裝置 IP 必須已知（靜態 IP 或先寫進 `/host_map.json`） |

> 現場若是「固定 IP 或可先寫清單」，**R1-b 明顯較省**；
> 若要「開機自動找到未知裝置」，必須走 R1-a。

---

## 12) 檔案清單

| 階段 | 新增 | 修改 |
|---|---|---|
| P1 | `slave/host/__init__.py`、`peer_link.py`、`stream_controller.py` | `Core_Manager.py`（＋1 行註冊） |
| P2 | `slave/host/node_table.py` | — |
| P3 | — | `slave/tasks/web_ui.py`（＋`/api/host`） |
| P4 | `slave/host/macro.py`（選配） | — |
| P5 | — | `slave/host/stream_controller.py`（＋檔案狀態機） |
| 文件 | 本文件 | `doc/README.md`（索引）、`doc/03_notes/01_changelog.md`（實作後） |

---

## 13) 與 PC 主控的關係

兩者**共存不互斥**，分工不同：

| | PC 主控（`tools/PC/NetBusMaster.py`） | ESP Stream Controller（本計劃） |
|---|---|---|
| 強項 | 大檔案、動畫生成、多裝置高並發、網頁控制台 | 免 PC、開機即跑、低延遲時序 |
| 弱項 | 需要一台 PC、開機慢 | 記憶體小、不能解碼音訊 |
| 定位 | **製作與派送**（prep） | **現場執行**（run） |

合理流程是：**PC 準備內容 → 板子現場執行**。
所以**不需要**把 PC 的功能全部搬到板子上——這也是 §1.3「不是目標」的原因。
