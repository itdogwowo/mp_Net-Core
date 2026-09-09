# 更新紀錄：遠端更新鏈路 / 臨時提速 / lib 三級分類 / 解碼性能 / 重複 import 清理

> **用途**：整合說明本次一系列更新的完整設計、指令集、檔案結構與行為語意。
> **分類**：筆記（03_notes）
> **最後更新**：2026-08-21
> **範圍**：`slave/` 韌體；`cores/`（PC 模板，已同步 import）；`test/` 與 `tools/` 尚未同步（見 §6）。

---

## 1) 總覽：本次更新包含四大塊

| 區塊 | 摘要 | 關鍵檔案 |
|------|------|---------|
| 遠端更新鏈路（第一階段） | 發現(IDENTIFY)、保險(REBOOT/WREPL/WEBUI)、網絡(NET_START)、IP(GET_IP)、master 定址(SET_MASTER) | `action/net_actions.py`、`schema/sys.json` |
| 臨時提速 | 協商式 UART 提速 + 超時回滾 | `action/hw_actions.py`、`schema/hw.json`、`lib/sys/bus_speed.py` |
| lib 三級分類 | `lib/` 拆為 `hw/ sys/ sw/` | `lib/hw/`、`lib/sys/`、`lib/sw/` |
| 解碼性能優化 | `pop_frame` 零拷貝 + 非 generator、ADDR 過濾、native handle_stream | `lib/sys/proto.py`、`app.py` |
| 重複 import 清理 | 熱路徑內（`loop`/handler）的函式內 import 提到模組頂部 | `lib/sys/task.py`、`action/hw_actions.py`、`action/net_actions.py` |

---

## 2) 定址模型（cID / master_cid）

- **`bus.cid`(uint16)**：裝置自身的協議短身份，由 `ConfigManager.ensure_cID()` 於 **T0（boot.py import 時）** 建立——`System.cID` 為空時以 `machine.unique_id()` 末 4 碼填入並持久化；取不到則 `"FFFF"`。cID 是**單一擁有**、由 ConfigManager 推動，消費者（解碼層）只讀不重算。
- **`bus.master_cid`(uint16, 內存)**：回應定址目標，預設 `0xFFFF`（廣播=未設定）。Master 透過 `SET_MASTER` 或 `IDENTIFY_REQ` 的 `reply_addr` 告知 slave；slave 記住後，所有回應的 `addr` 欄位都填 `bus.master_cid`。**只存內存，重開機丟失**（下次開機 master 再告訴）。
- **ADDR 過濾(`app.py` `handle_stream`)**：只收 `addr == ADDR_BROADCAST(0xFFFF)` 或 `addr == bus.cid` 的幀，其餘 `continue` 丟棄。這讓「逐 address 掃描」的 RX 端現成可用。

### IDENTIFY 流程（逐 address 掃描，模仿 I2C）

```
master 對 addr=X 發 IDENTIFY_REQ(0x100D, payload 帶 reply_addr)
  → 只有 cid==X 的 slave 收到
  → 記 bus.master_cid = reply_addr(非 0xFFFF 才記)
  → 回 IDENTIFY_RSP(0x100E): cid + slave_id + 多介面 IP JSON, addr 回 master_cid
```

---

## 3) 新增指令集

### 3.1 sys 群（0x10xx，空編號 0x100D 起）

| CMD | 名稱 | 方向(發起→接收) | Payload | 行為 |
|---|---|---|---|---|
| 0x100D | IDENTIFY_REQ | Master→Slave | `reply_addr(u16)` | 逐 address 素描；帶 reply_addr 告知 master_cid |
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

> Slave 端註冊 handler 的請求：0x100D / 0x100F / 0x1010 / 0x1012 / 0x1014 / 0x1016 / 0x1017。
> Slave 端只送出（不註冊 handler）的回應：0x100E / 0x1011 / 0x1013 / 0x1015 / 0x1018。
> 既有 0x1009 WEB_CTRL 保留不動（舊式、無回應，不動合同）。

### 3.2 hw 群（0x14xx，空編號 0x1403 起）— 臨時提速

| CMD | 名稱 | 方向 | Payload | 行為 |
|---|---|---|---|---|
| 0x1403 | SPEED_SET | M→S | `bus_type(u8)` `bus_id(u8)` `speed(u32)` `timeout_ms(u32)` | 記 old/target/timeout_at（**不切速**），先回 0x1404(舊速) 再 apply 切速 |
| 0x1404 | SPEED_ACK | S→M | `ok(u8)` `bus_type(u8)` `bus_id(u8)` `cur_speed(u32)` `target_speed(u32)` | 同步點（收到後兩邊一起切速） |
| 0x1405 | SPEED_COMMIT | M→S | `bus_type(u8)` `bus_id(u8)` | 鎖定新速、取消回滾 |
| 0x1406 | SPEED_REVERT | M→S | `bus_type(u8)` `bus_id(u8)` | 還原 old_baud（config 舊速） |
| 0x1407 | SPEED_QUERY | M→S | `bus_type(u8)` `bus_id(u8)` | 查狀態，回 0x1408 |
| 0x1408 | SPEED_STATUS | S→M | `state(u8)` `bus_type(u8)` `bus_id(u8)` `cur_speed(u32)` `target_speed(u32)` `remain_ms(u32)` | 狀態回報 |

- `bus_type` 沿用 `hw_manager.HW` 常數：UART=7, SPI=2, I2C=3。**第一階段只實作 UART**；SPI/I2C 回 `ok=0`（not supported）。
- `speed` 用 u32（baudrate 如 921600 超 u16）。
- `state`：0=IDLE, 1=SYNCING（已切、待 COMMIT）, 2=COMMITTED（鎖定）。

### 提速協商流程（同步點 = SPEED_ACK）

```
1. [舊速] master 發 SPEED_SET(0x1403: bus_type, bus_id, speed, timeout_ms)
2. slave 記 old_baud / target / timeout_at（進 SYNCING，**尚未切速**）
3. slave 回 SPEED_ACK(0x1404, 舊速)
4. slave 送出 0x1404 後呼叫 bus_speed_apply()：等 txdone() 排空 + margin，再 uart.init(target) 切速
   master 收到 0x1404 後立即切速（兩邊同步切）
5. [新速] master 在 timeout_ms 內「不斷敲門」驗證（SPEED_QUERY/STATUS_GET/IDENTIFY）
6. 驗證 OK → SPEED_COMMIT(0x1405) 鎖定（取消回滾，進入 COMMITTED + 啟動 idle 超時）
   ; 否則 timeout_at 到 → 自動回滾 config 舊速 → IDLE
7. 傳輸完成 → SPEED_REVERT(0x1406) 還原 old_baud
```

- **兩層 timeout**：①SYNCING 層 `timeout_at`（SET 的 `timeout_ms`，敲門失敗回滾）；②COMMITTED 層 `idle_timeout_at`（進入通訊後 N 秒無有效通訊回滾，`app.handle_stream` 每收到有效幀呼叫 `bus_speed_touch()` 刷新）。目前兩層暫共用同一 `timeout_ms`。
- **同步點 = SPEED_ACK**：slave「先回 ACK(舊速) 再切速」，master 收到 ACK 後一起切速。避免舊版「先切速再回 ACK」造成 ACK 以新速發出、master 收不到的時序 bug。
- 回滾 = 純時間檢查，由 `CircuitTask.loop` 每輪呼叫 `bus_speed_poll()`；即使新速下收不到有效幀，loop 照跑、照樣回滾（解掉「收不到指令→惰性檢查不觸發」死結）。
- **`_cur_baud` 修正**：MicroPython UART 無 `baudrate` 屬性，`_cur_baud` 回 0 會導致 `old_baud=0`、REVERT 不切速。已加 `_config_baud(bus_id)` 從 config 讀舊速，`_reinit_uart()` 切速時保留 rxbuf/txbuf（避免 `uart.init(baudrate=...)` 把 buffer 縮回預設 256）。

---

## 4) lib 三級分類重構

### 分類規則（已定案）

- **`lib/hw/`（硬體）**：直接碰 `machine`/GPIO/I2C/SPI/UART 的週邊驅動。
- **`lib/sys/`（系統）**：Net-Core 框架本身，彼此互相依賴的那群。
- **`lib/sw/`（軟體）**：獨立可用、能搬去別處照用的通用工具（不依賴框架）。

### 最終目錄結構

```
lib/
├── __init__.py
├── hw/  (8 模組 + __init__.py = 9 檔)  apa102, gt1151q, husb238, mp3_tf_16p, pca9685, TFT, uart_motor, xl9555
├── sys/ (21 模組 + __init__.py = 22 檔) buffer_hub, bus_adapter, bus_sources, bus_speed, circuit_bus,
│             ConfigManager, dispatch, fast_io, fs_manager, hw_manager,
│             log_service, net_bus, network_manager, now_bus, proto,
│             schema_codec, schema_loader, sys_bus, task, task_manager, webrepl_ctl
└── sw/  (3 模組 + __init__.py = 4 檔)  PixelController, PixelMathMethod, pixel_layout
```

### import 規則

- 一律**絕對 import**：`from lib.<cat>.X import ...`。
- 動態字串：`__import__("lib.TFT", ...)` → `__import__("lib.hw.TFT", ...)`（`driver/tft_drv.py:30`）。
- 跨包依賴：TFT(hw) → bus_adapter(sys) 用 `from lib.sys.bus_adapter import ...`。
- sw 包零內部依賴（只 import stdlib/machine）。

---

## 5) 解碼性能優化

### 5.1 pop_frame（零拷貝 + 非 generator）

- `StreamParser.pop_frame()`：解出單幀回 `(ver, addr, cmd, payload_mv)`，payload 是 `_buf` 的 memoryview（零拷貝），非 generator。
- `pop()`：相容介面，包 `pop_frame()` + `bytes(payload_mv)`（payload 可跨 feed 安全持有），保留給正確性測試/需跨幀持有者。
- 熱路徑（`app.handle_stream`）改用 `pop_frame`，避免每幀 `bytes()` 配置 + generator 物件引發的 GC churn。

**實測（ESP32, MicroPython/viper 真實）：**

| 測試 | 改前(pop) | 改後(pop_frame) |
|---|---|---|
| 純解碼 8K | 1.11 MB/s | **4.00 MB/s** |
| 純解碼 4K | 0.91 | **3.62** |
| 純解碼 2K | 0.85 | **2.99** |
| 雙緒管道 4K | 0.97 | **2.33** |

### 5.2 ADDR 過濾 + native handle_stream

- `handle_stream` 加 `@micropython.native`，hot loop 用 `pop_frame` + `bus.cid` 過濾。
- `my_cid`/`disp` hoist 到 loop 外（local），每幀只做 int 比較，零 hex 轉換。

### 5.3 緩衝重用（已探討、未採用）

- 「從 hub slot 零拷貝 pop」原型量到 6.85 MB/s，但因 MicroPython `memoryview` 無 `.find`、`bytes()` 拷貝 + GIL 串行下打崩，**不採用**；實際瓶頸在 feed 拷貝，已由 pop_frame 吸收大部分。

### 5.4 重複 import 清理（熱路徑 hoist 到頂部）

掃描全部函式體內的 import，分「該提」與「該留」兩類，只提前者：

**提到模組頂部（熱路徑 / 重複觸發，無循環依賴）：**
- `lib/sys/task.py`：`fcache_get()`（每 loop 都呼叫的快取讀取）內的 `from lib.sys.sys_bus import bus` 提到頂部。
- `action/hw_actions.py`：4 個 SPEED handler 內的 `from lib.sys import bus_speed` 提到頂部。
- `action/net_actions.py`：`on_wrepl_ctrl` 內的 `from lib.sys import webrepl_ctl` 提到頂部。

**刻意保留（lazy import，動了會壞）：**
- `from lib.sys.now_bus import NowBus`（now_bus import `espnow`，硬性依賴，不能 eager）。
- `fs_manager` / `log_service` / `network_manager` 內部的 `sys_bus` / `cfg_manager` import（避免循環依賴 + 延遲載入）。
- `tft_drv` / `gt1151q_drv` / `husb238_drv` / `xl9555_drv` / `PixelController` 等可選硬體驅動（沒啟用就不吃記憶體）。

> 原因：MicroPython 的 `import` 靠 `sys.modules` 快取，模組只載入一次、不會重複佔記憶體；但**函式體內的 `from lib import X` 每次執行都做 dict 查表**。在 `loop()` 這種巨大循環內，每輪查表會累積；在 handler（收到指令才觸發）內則可接受。規則：熱路徑一律模組級 import，handler 可保留函式內 import。

---

## 6) 已知待辦與注意

- **`test/` 與 `tools/` 尚未同步新 import 路徑**（重構只改了 `slave/` + `cores/`）。這些目錄的 `from lib.X import` 目前會 import 失敗，需後續補。
- **`temp/1/` 是 legacy 樹**，有自己的 lib，不屬本次範圍，勿動。
- **OTA（0x22xx）完全不動**——屬合作方合同，不增減、不實作、不用。
- 一次性重構腳本（`refactor_lib.py`、`deploy_lib.py`）與診斷檔（`test/_diag_*.py`、`test/_verify_*.py` 等）保留供參考，可視需要清理。

---

## 7) 相關文件索引

- `01_protocol/09_bus_speed_protocol.md` — 臨時提速協商流程詳解（本文件 §3.2 的獨立版）。
- `01_protocol/01_nc4_protocol.md` — NC4 封包格式（SOF/ADDR/CMD/CRC）。
- `01_protocol/05_integration_overview.md` — 既有協議整合說明。
- `slave/schema/sys.json` / `hw.json` — 指令 schema 唯一真相。
- `slave/action/net_actions.py` / `hw_actions.py` — 新指令 handler 實作。
- `slave/lib/sys/bus_speed.py` — 提速狀態機。
- `slave/lib/sys/proto.py` — 封包 + pop_frame。

---

## 8) 檔案更新流程重設計（2026-08-21）

FILE_* 0x20xx 檔案傳輸鏈路的重新設計：接收端完全被動、傳輸無關；新增兩段式 commit、斷點續傳、manifest 分離與 delta journal。

| 項目 | 內容 |
|------|------|
| 新增指令 | `0x2008 FILE_CONFIRM`、`0x200A FILE_UNDO`、`0x200D FILE_MOVE`、`0x200E FILE_PARTIAL_QUERY`、`0x200F FILE_PARTIAL_RSP`、`0x2010 FILE_ERROR_RSP` |
| 加欄位 | `FILE_QUERY_RSP`(0x2006) 加 `free` `pending`；`FILE_SCAN`(0x200B) 加 `target` |
| 兩段式 commit | 同名覆蓋不再直接刪舊檔：寫 pending → 舊檔 `.bak` → 新檔上位 → 更新 manifest；CONFIRM/UNDO 收尾 |
| 斷點續傳 | `.tmp` + delta `partial` 紀錄；正確性由 FILE_END 整檔 sha256 保證 |
| manifest 分離 | 本地 `/manifest.json` + SD `/sd/.manifest.json`，不融合，write-through 維護 |
| delta journal | `/sd/.delta.json`，`partial` + `pending` 兩段 |
| 自測 | `tools/selftest_file.py` loopback，真機 17 通過 0 失敗 |

關鍵檔案：`slave/lib/sys/fs_manager.py`、`slave/action/file_actions.py`、`slave/schema/file.json`（`echo/lib/fs_manager.py` 已同步）。完整用法見 `02_guides/10_file_update.md`。

> 已知限制：Slave 端回應幀仍走廣播（`Proto.pack` 不帶 addr）。單一 master 沒問題，但真正 MCU↔MCU 對等（多節點共享介質）需補「來源位址 + 回給來源」，建議單獨一輪做，避免與檔案流程耦合。

---

## 9) 2026-08-23 新增：FILE_PROMOTE + buffer 調校 + 測試工具（晚間）

> 更新日期 2026-08-23。這輪圍繞「雙板 UART 檔案傳輸 + 固件交換上線」做了三塊：①新增 FILE_PROMOTE 指令；②UART 接收 buffer 對齊 + 多插槽；③master 端互動/安全更新工具。

### 9.1 FILE_PROMOTE（0x2011）— SD → 根目錄固件正式上線

新增獨立指令，把「先上傳到 SD 驗證、確認無損再交換到根目錄正式上線」的需求落地。設計要點：

| 面向 | 內容 |
|------|------|
| 指令 | `FILE_PROMOTE 0x2011`，payload `src(str)` + `dst(str)` |
| 語意 | 把 `src`（/sd 暫存）內容「正式上線」到 `dst`（根目錄系統檔），舊 `dst` 自動留 `.bak` |
| 跨卷安全 | 用「讀+寫+刪」三步法，**不靠 `os.rename`**（未來接真 SD 卡、獨立掛載點也能用） |
| 流程 | ①src 串流複製到 dst.tmp → ②舊 dst→dst.bak（失敗自動還原）→ ③dst.tmp→dst → ④刪 src |
| 成功回覆 | `FILE_QUERY_RSP`（path=dst、exists=1、size） |
| 失敗回覆 | `FILE_ERROR_RSP`（err_write_fail=1） |

實作檔案：`slave/schema/file.json`、`slave/lib/sys/fs_manager.py::promote_file()`、`slave/action/file_actions.py::on_file_promote`。

### 9.2 UART 接收 buffer 對齊 + 多插槽

- `slave/driver/uart_drv.py`：UART `rxbuf/txbuf` 都改 16384（原先 txbuf 只有 4096，裝不下最大幀 8205B）。
- `slave/lib/sys/proto.py`：`RX_BUF_SIZE` 4096 → **4115**（一幀剛好一槽，避免拆幀）。
- `slave/lib/sys/circuit_bus.py`：`u8_rx_slots` 預設 2→8、上限 4→16（多插槽扛消費延遲，而非單槽變大）。
- `slave/lib/sys/bus_speed.py`：`_reinit_uart()` 切速時保留 rxbuf/txbuf（`uart.init(baudrate=...)` 會把 buffer 縮回預設 256）。

> 判斷：這批 buffer 調校方向正確，115200 下 4KB chunk 傳輸已穩定（8/10，重試可到近 100%）。高速 460800 可正常收發（3/5），剩餘掉包是 CircuitTask 排程 / bus_decode 消費速度問題，尚未根治（見 `08_night_test_results.md` §18）。

### 9.3 master 端工具（`test/protocol/night_run/`）

| 檔案 | 用途 |
|------|------|
| `master_agent.py` | master 測試 agent：NC4 組/拆幀 + SPEED/FILE 指令 + 手動 decoder + `send_wait` 重試 |
| `safe_update.py` | 安全檔案更新流程：`stage`/`verify_stage`/`apply`/`promote`/`confirm`/`undo`/`cleanup` |
| `interactive_master.py` | 互動式選單（仿 NetBusMaster 風格）：敲門/檔案傳輸/固件更新/查詢/刪除/提速 |
| `repl_upload.py` | 透過 normal REPL(ctrl-B) base64 寫檔的工具（繞過 TaskManager 佔用 raw REPL） |
| `espnow_transfer.py` | ESP-NOW 板間傳檔框架（未端到端實測） |

### 9.4 尚未完成

- **端到端 FILE_PROMOTE 實測**：卡在多 chunk 連續傳輸掉包（單 4KB chunk 可過，8KB 兩 chunk 連發偶發失敗）。
- **掉包根因**：slave 端 `bus_decode` 每輪只讀 1 slot（`decode_budget_slots` 預設 1）+ CircuitTask 排程，是架構級瓶頸，需進一步調整。
- **RS485 半雙工**：master 端時序要照 `_Rs485Uart`（listen-before-talk + DE 切換 + txdone）重寫；目前是點對點全雙工。
- **無線 ESP-NOW 傳檔**：鏈路驗證過、腳本備好，端到端未測。

---

## 10) 2026-08-24 新增：pixel 效果子系統重構 + RenderTask 節拍 wrap 修復

> 這輪圍繞 pixel 燈效做了兩塊：①效果框架與目錄解耦、json 成為唯一真源；②修掉 RenderTask 計時器 wrap 導致「跑一段時間燈自己停」的 bug。

### 10.1 效果子系統重構（框架 / 目錄 / json 三權分立）

| 檔案 | 角色 |
|------|------|
| `slave/lib/sw/effect_core.py` | 框架：`Effect` 基類 + 登記表 + 波表快取 + `check_conflicts()` |
| `slave/pixel/effects/effects.py` | 效果目錄：畫波效果 + py 補充類別 + `register()` + 自檢 |
| `slave/pixel/effects/effects.json` | **唯一真源**：id/name/params（含 program 畫波）都在這手寫 |

設計要點：

- **json 是唯一真源**：id / name / params（含 program 畫波）全在 `effects.json` 手寫。
- **畫波效果不需要 py 類別**：program 寫 json，由內建 `Effect` 播放（波表預算 + viper + 無浮點）。
- **只有畫波寫不出來的效果才寫 py**：`register(類別)`，靠 name 與 json 配對（如 `pearl_chain` 珍珠鏈：畫完波後「批量派發 + 控制間距」）。
- **id/name/配對衝突不 raise**：啟動時 `check_conflicts()` 列印警告（對齊 boot GPIO 檢查），人肉判斷修正。
- 波形段 `F` 語義：**`F/10 = 段內週期數`**（`F=5` 半週期=純升或純降、`F=10` 完整週期=升+降）。
- 相位 `phi`（0-4095 ≈ 0-360°）：`1023`=峰、`2047`=中點、`3071`=谷。

### 10.2 RenderTask 節拍 wrap bug（燈跑一陣子自己停、無 log）

**症狀**：本地燈效無限循環播放一段時間後，燈靜止/熄滅，且**不印任何 log**（不是 buffer 爆、不是重啟）。

**根因**：`slave/tasks/render.py` 的 RenderTask 節拍推進用錯 API：

```python
# ❌ 錯：普通整數加法，next_tick_us 不會 wrap
self.next_tick_us += self.interval_us
```

而 `time.ticks_us()` 在 ESP32 MicroPython 是**會週期性 wrap 的 32-bit 值**。`+=` 讓 `next_tick_us` 一路往上加，與 wrap 回小值的 `now` 相位錯開後，`ticks_diff(now, next_tick_us)` 永遠為負 → `>= 0` 永不成立 → RenderTask 每輪都 `return`，靜默停止取幀。

**修復**：

```python
# ✅ 對：ticks_add 會正確 wrap
self.next_tick_us = time.ticks_add(self.next_tick_us, self.interval_us)
```

> 已 grep 全 `slave/` 確認只有 `render.py` 這一處誤用；其餘 tick 推進都用 `ticks_add` / `ticks_diff`。

### 10.3 相關文件

- `02_guides/11_developing_effects.md` — 開發燈效完整教學（三種寫法 / Effect API / 波形段 / 色彩 / write 模式 / 框架 API / 四層資料）。
- `02_guides/08_pixel_subsystem.md` — pixel 四層資料 + 播放模型。
- `slave/lib/sw/effect_core.py` — 效果框架。
- `slave/pixel/effects/effects.py` — 效果目錄（含 `pearl_chain` / `example_eyes` 範例）。

---

## 11) 2026-08-24 新增：UART-412 馬達接入 pixel + 停止填中性值（dStay）

> 這輪把 UART-412 馬達（ATTiny412 電機控制器）接入 pixel 系統，並把「停止/熄燈」改成填中性值（對齊舊專案 mp_LEDController 的 dArc 概念）。

### 11.1 馬達走 pixel 系統（讀 W 通道）

- `UartMotor`（`slave/lib/hw/uart_motor.py`）實作 controller 介面：`pixel_type="uartMotor1"`、`frame_size`（×4）、`st_load_and_convert()`（從 big_buffer 提取 W 通道 8-bit）、`st_show()`。
- 效果用 `write:"w"`（或 rgbw）→ W 通道 = 速度 byte（0x80 停、<0x80 正轉、>0x80 反轉）。
- 初始化鏈：`driver/motor_drv.py`（讀 config `uartMotor`）→ `boot.py` 註冊 → `pixel_drv.py` 聚合進 pixel_list → `pixel_task.TYPE_MAP` 加 `uartMotor1`。

### 11.2 UART-412 協議關鍵（單台串接，不用廣播）

- 廣播模式受 `MAX_DEVICE=32` 限制（原碼 `while i < MAX_DEVICE+2`），address > 32 收不到。
- `show_all()` 改為**單台 frame 串接**：`ff addr value fe` × N 一次過 uart.write（address 不連續也不填空洞）。
- **歸零保護**：UART-412 的 `value=0` = 全速正轉（updateMotor: IN1 PWM 254）！`st_load_and_convert` 讀到 0 → 映射中性值（死區 0x80），避免 reset/熄燈暴走。

### 11.3 停止 = 填中性值（dStay，對齊舊項目 dArc）

- 舊專案 `LEDController.reset()` 回到 config 的 `dArc`（不是 0）；本專案命名 **`dStay`**（default Stay，12-bit 0-4095）。
- `PixelStreamer.clear_all()`：每個 controller 填自己的 `neutral_value`（燈=0 熄滅、motor=0x80 死區停）。
- 三處停止流程統一改用：`render.py`（is_streaming 熄燈）、`pixel_task._stop()`、`Core_Manager` 退出。
- config 每台設備可設 `dStay`：WS2812/APA102/PCA9685 預設 0；uartMotor 預設 2048（= 0x80）。

### 11.4 相關文件

- `02_guides/08_pixel_subsystem.md` — §4.1 Pixel Render 架構簡介（雙核 + hub + controller + 停止填中性值 + motor 接入）。
- `02_guides/11_developing_effects.md` — §7 新增「用 write:w 驅動馬達」。
- `slave/lib/hw/uart_motor.py`、`slave/driver/motor_drv.py`、`slave/lib/sw/PixelController.py`（clear_all / neutral_value）。

---

## 12) RenderTask 停止/暫停的電機行為補完（dStay 顯式化 + 中性幀只推一次）

> 電機（uartMotor）一直透過 `PixelStreamer` 通用 controller 介面參與播放（`show_all` 讀 W 通道），
> 本輪補上停止/暫停路徑的兩個缺口，並把 `dStay`（對齊舊專案 PWM 的 dArc 概念）顯式寫進 config。

### 12.1 停止路徑：clear_all 不再每 loop 推幀（電機 UART 洪水）

- 舊版 `render.py` 停止分支的 `clear_all()` 在 100ms 節流檢查**之前**執行 → 每個 runner 週期（數百 Hz～1kHz）都推一幀完整中性幀，電機 UART 被 stop frame 灌爆。
- 改為狀態轉換旗標 `_neutral_pushed`：只在「進入停止狀態」時推一次（燈熄、電機 0x80 停），硬體會保持在中性值。

### 12.2 暫停 = 電機也停（`PixelStreamer.stop_motors()`）

- 舊版 `is_paused` 分支完全不推幀 → 電機保持最後速度 byte，暫停期間持續運轉。
- 新增 `PixelStreamer.stop_motors()`：只把 `pixel_type="uartMotor1"` 的 controller 填 `neutral_value`（0x80 停）歸位、**燈保持最後一幀**，再推一幀；同樣只推一次。
- `pixel_task` 的 pixel_pause 現在同步 `bus.shared["is_paused"]`，讓本地燈效暫停也走同一條電機歸位路徑（與 stream 0x3005 暫停一致）。

### 12.3 config 顯式化

- `slave/config.json` 的 `uartMotor.list` 每台加 `"dStay": 2048`（12-bit，>>4 = 0x80 死區停；原為 code 預設值，現與 PWM 的 dArc 一樣在 config 可見）。
- `motor_drv.py` docstring 補 `dStay` 欄位說明。

---

## 13) 移除舊專案遺留的 dArc 設定（全部改用 dStay）

> 舊專案 mp_LEDController 的 `dArc`（reset 回到中性值）在本專案已改名 `dStay`，
> 但 config 仍殘留舊欄位（code 端完全沒有讀 `dArc`，是死設定）。本輪全部清掉。

- `slave/config.json`、`ports/P4/ESP32-P4-ETH/config.json`、`test/protocol/night_run/config.1401.{test,backup}.json`：
  - PWM 條目 `"dArc": 0` → `"dStay": 0`
  - PCA9685 條目：移除 GPIO 內層的 `"dArc": 0`，改在 item 層放 `"dStay": 0`（driver 讀的位置：`pca9685_drv.py` 的 `item.get("dStay", 0)`）
- LED（WS2812/APA102/PCA9685）`dStay` 顯式設 0 與 code 預設一致；uartMotor 維持 2048。
- grep 全 `*.json` 已無 `dArc`；docs 中「對齊舊專案 dArc 概念」的歷史說明保留。

---

## 14) Pixel 模式識別碼合併為 16-bit + 串流優先互斥 + MODE_SET 非阻塞

> wire 協定照舊（`mode_type:u8` + `mode_id:u8` 分開讀），進系統後合併成
> 單一 16-bit id；本地模式與串流改為「串流優先、結束自動恢復」。

### 14.1 (mode_type, mode_id) → 內部單一 16-bit id

- `pixel_actions._combine()`：內部模式識別碼 = `(mode_type << 8) | mode_id`（0..65535）；
  `modes/*.json` 的 `id` 即此合併值（例：LED 組 mode 5 → wire `(1,5)` → id `0x0105`）。
- `MODE_LIST_RSP.entries` 改為每筆 **2 bytes**（u16 LE 合併 id；舊實作是 raw u8 id，
  協議文件原訂的 6-byte 格式從未實作）。`MODE_SET`/`MODE_DETAIL_QUERY` 的
  `mode_type`/`mode_id` 欄位與 schema 不變。
- master（`NetBusMaster.py`）：`_query_modes` 解 u16 entries；發 0x3105/0x3107 時把
  合併 id 拆回 `(mode_type, mode_id) = (id >> 8, id & 0xFF)`。

### 14.2 串流優先 + 結束自動恢復（`pixel_task.py`）

- `loop()`：`stream_active=True`（串流載入/播放中）→ 本地模式讓位（保持 `_playing`，
  不停止）；`stream_active=False` 且本地模式還在播 → 自動恢復並重新宣告
  `is_streaming/is_ready`（RenderTask 恢復取幀）。修掉舊版「串流開始不踢本地模式、
  兩個生產者同時寫 SPSC hub」的混幀風險，以及「串流結束本地模式不會自動接回」。
- `_start/_stop/pixel_pause`：串流播放中不碰渲染旗標（`is_streaming/is_ready/is_paused`
  是串流的）、不熄燈，避免誤傷串流。

### 14.3 MODE_SET 非阻塞延遲

- `on_mode_set` 不再 `time.sleep_ms()`（舊版在 core0 通訊鏈上阻塞最多 10 秒，
  全 core0 任務卡死）；改記 `pixel_remote_start_at` 時間戳，由 PixelTask
  延遲到期才播放。MODE_STOP 可取消未到期的延遲 MODE_SET。

---

## 15) 模式播放參數：play_repeat（每輪連播次數）+ range（播放範圍）

> modes/*.json 新增兩個播放控制：`play_repeat` 控制「一輪出現時播幾次」，
> `range` 控制「map 條目只播群組內的哪一段」。

### 15.1 `play_repeat`（mode 層，預設 1）

- 每輪出現時連播 N 次：效果播完（生成器耗盡）→ `_restart_player` 重播，
  直到次數滿才 `_find_next` 換下一個；生成器不支援 restart → 自動剷除重建。

### 15.2 `range`（map 條目層，選用）

- `PixelLayout.sub_offsets()`：群組內 slice 範圍（Python 語義，end 不含，
  群組相對，對齊 set_value 的 k 語義）→ 預先算好的子 offsets array('H')。
- `PixelLayout.scatter_offs()`：用預先算好的 offsets 散射（scatter 拆出的
  低層路徑，range 用；無 range 的條目仍走原 scatter）。
- 同一群組可拆多段配不同效果（重複檢查改為 group+range 組合）；
  範圍外像素「不修改」，可多段累加組合。

---

## 16) play_loop（循環次數，-1=無限）+ maxF（每次播放最大幀數）

> 每輪出現時的播放控制擴充：`play_loop` 取代 `play_repeat`（舊名相容），
> 並支援 `-1` 無限循環；新增 `maxF` 截斷單次播放幀數。

- `play_loop`：每輪出現時連播 N 次（播完 restart 重播）；`-1` = 無限循環，
  一直播到 `pixel_stop`/MODE_STOP（0x3106）/串流介入。`play_repeat` 仍相容（別名）。
- `maxF`：每次播放最大幀數（commit 幀計數）；達上限 → 強制結束本次循環
  （配合 `play_loop` 可做「每次循環播固定幀數、無限循環」）。0/缺省 = 不限制。
- `slave/pixel/modes/demo_eyes.json` 更新為新欄位範例（`play_loop:-1` + `maxF:500` +
  map 拆兩段 range：eyes 0:16、wave 16:32）。
- 注意：mode JSON 欄位間逗號不可省（`"maxF": 500` 後要逗號，否則載入失敗）。

---

## 17) 播放語意重定義（play_loop / play_count / play_interval）+ 短效果自己循環

> 三個播放欄位改用使用者指定的語意；並處理「同 mode 內效果長短不一」的餘下部分
> ——短效果自己循環重播，直到最長效果結束。

### 17.1 欄位語意（新）

| 欄位 | 語意 | 值 |
|---|---|---|
| `play_loop` | **總共 loop/出現幾次循環** | `0`=不播、`N`=最多 N 次、`-1`=常駐每輪（預設 -1） |
| `play_count` | **同一個 loop 中播放幾次** | `1..N`=連播 N 次、`-1`=無限連播（預設 1） |
| `play_interval` | **相隔多少個循環播一次** | `0`=每個循環都播、`1`=隔 1 循環（預設 0） |

- 舊語意 `play_count`（前 N 輪）→ 新 `play_loop`；舊 `play_interval`（1=每輪）→
  新 `play_interval` 0-based（0=每輪）。**demo_eyes.json 已遷移**
  （`play_loop:-1, play_count:1, play_interval:0`）。
- `play_interval=0` 除零問題修正：`(pass-1) % (interval+1)`，0 即每循環，不再崩潰。

### 17.2 短效果自己循環（長短不一的餘下部分）

- `_tick_player`：entry 生成器耗盡 → `restart()` 重播（短效果繼續動），直到
  **全部 entry 都至少跑完一次**（= 最長效果結束）本次循環才結束，全部一起重播/換下一個。
- 生成器不支援 `restart()` → 耗盡即定格（保持最後一幀，相容舊行為）。
- `play_count` 連播 / `maxF` 截斷維持。

### 17.3 mode 檔遷移（舊語意 → 新語意）

- 對照：舊 `play_count`（前 N 輪）→ 新 `play_loop`；舊 `play_interval`（1=每輪）→
  新 `play_interval` 0-based（`N-1`）；舊 `play_repeat`/`play_loop`（連播）→ 新 `play_count`。
- 已遷移：`slave/pixel/modes/demo_eyes.json`、`tools/PC/download/{80F1B2D0ADA8,30EDA0296EDC}/pixel/modes/{demo_eyes,diffusion}.json`
  （全部 → `play_loop:-1, play_count:1, play_interval:0`）。
- ⚠️ **部署順序**：新語意的 mode 檔必須配新韌體一起上裝置——舊韌體讀
  `play_interval:0` 會除零崩潰、`play_count:1` 會變「只前 1 輪」。

---

## 18) 看門狗 WDT（config 控制 + Ctrl+C 自動解除 + 開機按鍵 bypass）

> ESP32 的 `machine.WDT` 無法手動停止（無 deinit，soft reset 不清，斷電才清）。
> 故設計不「停」狗，而是用**餵狗執行緒**達成等效解除——只有「真卡死」才重置。

### 18.1 架構

- `lib/sys/watchdog.py`：`init_watchdog()`（config 讀取 + 按鍵 bypass + 建立 WDT +
  啟動 keeper 執行緒）+ 純決策函式 `_should_feed()`（PC 可測）。
- `TaskManager.runner_loop` 每圈寫 `core0_tick` / `core1_tick` 心跳
  （core1 首次啟動設 `core1_started`）；keeper 執行緒不可用時退回 runner 直接餵狗。
- `Core_Manager.launcher()` 在 `tm.finalize()` 後呼叫 `init_watchdog()`。

### 18.2 餵狗決策（keeper 每 ~1s）

| 情境 | 決策 |
|---|---|
| `wdt_hold=True`（REPL 手動）或 `engine_run=False`（**Ctrl+C 強制暫停**，Core_Manager finally 設定） | 餵（WDT 等效解除，REPL 測試無限時間） |
| 引擎在跑且 core0 心跳新鮮（core1 已啟動時也新鮮） | 餵 |
| 引擎在跑但心跳 stale（任務真卡死） | **不餵 → 8s 後重置** |

### 18.3 逃生門（測試不被鎖）

1. config `System.watchdog.enable: 0`（預設）——開發/單元測試完全不建立 WDT。
2. 開機按住 `btn_bypass_gpio`（預設 GPIO42）→ 不建立 WDT——現場測試不用改 config。
3. 使用者 Ctrl+C 暫停 → keeper 自動繼續餵狗——**不用先改/存 config**。

### 18.4 限制

- timeout 上限 ~8388ms（clamp 8000）；單次任務阻塞不能超過 timeout。
- soft reset（Ctrl+D）不清 WDT；斷電/硬體 reset 才清。boot 早期 keeper 即啟動，
  重啟循環不會發生。

---

## 19) WDT 自動關閉（Ctrl+C 時 ConfigManager 自動存檔，下次開機生效）

> 使用者強制暫停（Ctrl+C）時，系統自動把 `System.watchdog.enable` 存成 0——
> **不用預先改/存 config，連一次 reset 都不用硬食**。

### 19.1 流程

```
Ctrl+C（👋 User stop requested）
  ├─ 1. Core_Manager finally：engine_run=False
  │        → keeper 繼續餵狗 → 本次 session 不重置（REPL 無限時間）
  └─ 2. auto_disable_on_interrupt()（新）：
           bus.shared["System"]["watchdog"]["enable"] = 0
           cfg_manager.save_from_bus(update_key="System.watchdog.enable")
           （ConfigManager 無損單值更新，不動其他欄位）
        → 下次任何開機都不再建立 WDT → 測試永遠不被鎖
```

- 只有 WDT 原本開啟（enable=1 且 wdt service 存在）才寫 config，避免無謂寫入。
- 要恢復 WDT：REPL 執行
  `from lib.sys.watchdog import watchdog_set_enable; watchdog_set_enable(True)`。
- `watchdog_set_enable(enabled)`：改 config + 無損存檔（下次開機生效），
  本次 session 的 WDT 由 keeper 繼續餵，不受影響。
- 已驗證：ConfigManager 無損更新對 `System.watchdog.enable` 0↔1 roundtrip 成功、
  其他欄位完好（PC 文字層測試）。

---

## 20) WDT v2：移除 keeper 執行緒，改主線程直接餵狗（穩定性優先）

> 對 v1 keeper 設計的保留意見成立：第三條執行緒 + 跨核心讀共享 dict 是
> ESP32 MicroPython（GIL/GC/執行緒分配）的新增失敗面。v2 回到最簡單模型——
> **零額外執行緒、零跨核心**，代價是「硬食一次」重置。

### 20.1 新架構

- `init_watchdog()`：只建立 WDT + 註冊 service（不啟動任何執行緒）。
- `TaskManager.runner_loop(0)`（主線程）：每圈直接 `wdt.feed()`——
  同執行緒建立/餵，WDT 物件完全不出主線程。
- 移除：keeper 執行緒、`_should_feed()`、core0/core1 心跳戳記、
  `wdt_hold`、`wdt_keeper_fallback`。

### 20.2 行為

| 情境 | 結果 |
|---|---|
| 系統正常 | runner 每圈餵狗 → 永不觸發 |
| 任務真卡死（runner 停） | 不餵 → ~timeout 後重置（自動復原） |
| 使用者 Ctrl+C | config 自動存 `enable=0` → WDT 在 timeout 後**重置一次（硬食一次）** → 下次開機不再建立 WDT → 永久解鎖 |
| 不想等 timeout | Ctrl+C 後 REPL 執行 `machine.reset()` 立即重啟 |

- 提示訊息（Ctrl+C 後印出）：「已自動停用…~8 秒後 WDT 將觸發重置一次；想立即重啟可執行 machine.reset()」。
- core1（計算核）卡死不偵測（v1 的心跳 gate 一併移除）；如需可日後用
  「core0 讀 core1 心跳」補回，但建議先觀察實際需求。

---

## 21) WDT v3：自動重新武裝（像 bus_speed 超時回滾——沉默即回安全態）

> 「WDT 關閉」變成暫時狀態：測試模式（enable=0）下，若連續 `auto_rearm_ms`
> （預設 60s）沒有任何有效指令封包（= 沒人操作）→ 自動存 enable=1 並重啟，
> WDT 保護自己回來。完整故事：Ctrl+C 硬食一次 → 測試 → 離開後 1 分鐘保護自動恢復。

### 21.1 完整故事（時間線）

```
1. 進 REPL → Ctrl+C → 自動存 enable=0 → 硬食一次重置（§19）
2. 開機 = 測試模式（WDT 關）。有人操作（master 送指令 / REPL 工作）→ 保持關
3. 沒人操作（無任何有效封包）連續 auto_rearm_ms（60s）→ 自動存 enable=1 + 重啟
4. 下次開機 → WDT 保護回來（部署安全）→ 回到正常模式
```

### 21.2 實作

- `watchdog.touch()` / `idle_ms()`：app.handle_stream 收到任何有效封包時呼叫
  （與 bus_speed_touch 同位置、同執行緒）——「有人操作」= 收到封包。
- `watchdog.should_rearm(idle, boot_age, now, rearm_ms)`：純決策（PC 可測）。
  沉默 ≥ rearm 且開機已過寬限（≥ rearm）→ re-arm。開機寬限避免
  「開機後從未收到封包」在寬限期內誤觸發。
- `tasks/watchdog_task.py`（WatchdogTask）：core0 主線程任務，無新增執行緒；
  僅 enable=0 且 auto_rearm_ms>0 時由 Core_Manager 註冊。觸發 →
  `watchdog_set_enable(True)` + `machine.reset()`。
- **REPL 暫停期間 WatchdogTask 不跑 → 不會誤重啟正在 REPL 工作的 session**。
- config：`System.watchdog.auto_rearm_ms`（預設 60000；0 = 關閉此行為）。
- 語意：持續有人操作（master 持續發指令）→ 不 re-arm（「有人使用」= 測試/操作
  模式）；沉默 1 分鐘 → 回安全態。
- 已驗證：`should_rearm` 5 情境（寬限/逾時/有通訊/邊界/從未收到）全過。

---

## 22) WDT v4：硬食一次改為 finally 立即重啟 + re-arm 防無限迴圈

> 實測發現 v3 的問題：「硬食一次」是 Ctrl+C 後 **8 秒 WDT 偷襲**——使用者在
> 想代碼時被突然重啟。修正：硬食改在 **finally 主動立即執行**（可預測）。

### 22.1 Ctrl+C 行為（finally / KeyboardInterrupt 分支）

- 舊：存 enable=0 → 等 WDT 8 秒後觸發（偷襲，打斷思考）。
- 新：存 enable=0 → **立即 `machine.reset()` 一次**（硬食一次，可預測）；
  下次開機進入測試模式（無 WDT），之後 Ctrl+C 不再有任何重啟（session 無限）。
- 測試模式（無 WDT）Ctrl+C → 不做任何事（`auto_disable_on_interrupt` 只在
  WDT 啟用時動作）。
- 存檔失敗 → 不重啟，印錯（WDT 會在 timeout 後自然觸發）。

### 22.2 re-arm 防無限重啟迴圈

- WatchdogTask 觸發 re-arm 時：**只有 `watchdog_set_enable(True)` 成功才
  `machine.reset()`**；失敗 → 印錯 + 下個週期再試——避免「存不進 → 重啟 →
  又 enable=0 → 又倒數」的無限重啟迴圈。
- re-arm 每個開機 session 最多觸發一次（成功後 enable=1，WatchdogTask 不再註冊）。

---

## 23) WDT v5：移除 WatchdogTask 獨立任務，re-arm 檢查併入 runner_loop 大循環

> 獨立任務不值得（還要條件註冊）。re-arm 檢查只是每圈幾行——直接寫進
> `TaskManager.runner_loop(0)`，是大循環的一步，每圈執行一次。

### 23.1 變更

- `tasks/watchdog_task.py` 刪除；Core_Manager 不再條件註冊任務。
- `watchdog.arm_rearm(rearm_ms)`：init_watchdog 在測試模式（enable=0 且
  auto_rearm_ms>0）時啟動倒數（開機寬限 = rearm_ms）。
- `watchdog.poll_rearm()`：runner_loop(0) 每圈呼叫（與餵狗同一 try 區塊）。
  沉默逾時 → 存 enable=1（成功才 `machine.reset()`）+ 觸發前先清 `_rearm_ms`
  （每個 session 只觸發一次，防重複/防無限迴圈）。
- 維持：無額外執行緒、無跨核心、無獨立任務——全部在主線程大循環內。

---

## 24) WDT bypass 腳位進 GPIO 衝突檢查（預設改 null）

> `btn_bypass_gpio` 加入 boot.py Phase 1 的 `gpio_claim`/`gpio_validate`——
> 撞腳開機直接報錯（不再靜默）。電位語意：**接低電位（GND）= bypass（WDT 關）**；
> 浮空/高電位 = 正常（開機設 PULL_UP）。

- `watchdog.gpios()`：有設定 `btn_bypass_gpio` 才 claim（driver 名 "wdt"，
  label "wdt_bypass"）；`null`/未設定 → 不 claim。
- `boot.py` DRIVERS 加 `("wdt", g_wdt)`。
- `slave/config.json` 預設 `btn_bypass_gpio: null`（預設關閉——使用者不太需要
  此功能；要現場測試用才設空閒腳位）。原預設 42 會與 PIN 的 btn 衝突
  （同一腳兩個 driver），故移除預設值。
- 已驗證（PC 模擬 boot Phase 1）：null → 通過；42 → 衝突報錯
  （`GPIO 42: btn (PIN) 與 wdt_bypass (wdt) 衝突`）；19（hiNew 空閒腳）→ 通過。
- 注意：ESP32-S3 的 GPIO 19/20 = 原生 USB D-/D+；若板子用 USB 上傳，
  建議改設其他空閒腳（如 1、2）。

---

## 25) GPIO 檢查改為「正確印明細 + 走 level」：衝突永遠顯示、例行清單降噪

> `gpio_validate()` 不再 `raise ValueError`（raw exception，看不到明細）——
> 改為正確印出「哪隻腳、哪個外設對撞」並回 False；正常 GPIO 清單降為
> level 2（debug_level≥2 才顯示），衝突永遠顯示（不受 debug_level 影響）。

- `gpio_validate()`：衝突 → `print` 明細（例：`GPIO 42: btn (PIN) 與 wdt_bypass (WDT) 衝突`）
  + 回 False，不 raise；無衝突 → True。
- `gpio_dump()`：改用 `dprint(level=2)`——例行資訊降噪。
- `boot.py`：`if not bus.gpio_validate(): raise SystemExit("[BOOT] GPIO 衝突 — 修正 config.json 後重開機")`。
- `_DRIVER_LABELS` 補 `"wdt": "WDT"`（衝突訊息顯示 WDT 而非原始 key）。
- 已驗證（PC）：無衝突 → True + level 1 靜默 / level 2 顯示；衝突 → 明細 +
  False（不 raise）；debug_level=0 衝突仍顯示。

---

## 26) Watchdog / 播放引擎正式 PC 測試（test/sys/）+ 修掉 2 個真 bug

> 新增 `test/sys/test_watchdog.py`（17 項）與 `test/sys/test_pixel_task_engine.py`
> （11 項），fake machine.WDT/Pin/reset/ConfigManager + fake 播放器，不依賴硬體。

### 26.1 測試發現並修掉的 bug

1. **`arm_rearm(0)` 誤啟倒數**：`max(1000, int(rearm_ms or 0))` 在 `auto_rearm_ms=0`
   （關閉此行為）時會 arm 成 1000ms。修正：`rearm_ms <= 0` → 不 arm（回 False）。
2. **`auto_disable_on_interrupt()` 永遠回 False**：動作有執行（存 config + 重啟）
   但回傳值恆 False。修正：動作成功回傳 `ok`。

### 26.2 覆蓋範圍

- `init_watchdog` 全分支：enable=0（含 rearm 啟動）/ enable=1 / 按鍵 bypass
  （低電位跳過、高電位正常）/ timeout clamp（8000 上限、1000 下限）。
- `watchdog_set_enable`：改 bus + 存 config + 無 watchdog 區塊自動建立。
- `auto_disable_on_interrupt`：WDT 開啟 → 存 enable=0 + 立即重啟；
  測試模式 → 不動作；存檔失敗 → 不重啟。
- `should_rearm`/`touch`/`idle_ms`/`poll_rearm`：寬限/沉默/有通訊/觸發一次/
  存檔失敗不重啟。
- **TaskManager.runner_loop(0) 整合**：背景執行緒跑 runner → 每圈 `wdt.feed()` +
  `poll_rearm()` 確實被呼叫。
- 播放引擎：短效果循環、play_loop/play_count/play_interval/maxF/欄位解析/range。

### 26.3 執行

```bash
python -B -m unittest discover -s test/sys -p "test_*.py"    # 28 項
python -B -m unittest discover -s test/motor -p "test_uart_motor.py"   # 36 項
python -B test/pixel/test_pixel_math.py   # 27 pass
python -B test/pixel/test_pixel_color.py  # 18 pass
python -B slave/lib/sw/pixel_layout.py    # 自檢
```

---

## 27) master 移除自動重連：離線只標記，重連一律人手發起（2026-09-02）

> 背景：設備端出現 `ECONNABORTED → LAN 連接成功 → DISCOVER` 抖動循環（非重啟）。
> 追查發現 master 的健康檢查在「離線/無響應」判定後會週期自動敲門
> （unicast DISCOVER 0x1001）叫設備連回——離線期間每 10s 一直發，slave 端
> `on_connect_request` 的 `ws_stale_ms` 防抖門檻又會自我斷線重連，兩邊形成
> 「敲門 → 重連 → 再敲門」的循環（詳見 `doc/03_notes/12_upload_wdt_diagnosis.md`）。
>
> 使用者要求：**master 不應該主動自動發起重連，重連應由人手發起。**

- `tools/PC/NetBusMaster.py`：
  - 刪除健康檢查自動敲門：`_knock_offline_devices()`、`_knock_ip()` 及
    `_knock_last`/`_offline_knocked` 狀態、config `reconnect_knock_interval_s`。
  - 刪除 `main_loop` 啟動時依紀錄自動敲門（原本每次開 master 都會叫設備上線），
    改印提示「設備未上線時，用選單 1 手動掃描/敲門」。
  - 手動叫回路徑不變：選單 1 = 廣播掃描 / 定向 IP / 依紀錄敲門（`_knock_recorded_devices`）。
- `tools/PC/slave_map.json`：移除過時的 `reconnect_knock_interval_s` key。
- 待真機驗證：離線後 master 不再發 DISCOVER、手動敲門能正常叫回、大檔部署
  不再抖動（見 12 號筆記 §5 接手清單）。

---

## 28) 連線存活判斷改走 WS 通道本身：移除 master 全部定時 health 檢查 + 修 Scan 重啟（2026-09-02）

> 使用者原則：「判斷 ws 自己通道的連接狀態（佢本身就有連接判斷），冇乜
> 回應唔回應，唔需要頻繁發起 health 檢查——檢查連線係我手動執行的動作，
> 或者播放途中的動作」。半開連線的歷史：兩端都以為連住 → slave 唔放新連線；
> 之後 slave 加咗防抖門檻（`ws_stale_ms`）允許斷線重連，所以另一端先會見到
> 不斷重新連接 WS。master 停止自動敲門後，門檻只會喺手動敲門時行到。

### 28.1 NetBusMaster：刪除整個主動健康檢查

- 移除 `_health_check_loop` / `_probe_device` / health 執行緒 / `stop()`；
  移除 `DeviceMonitor.last_probe_at` 與 `transfer_active` 旗標（連帶
  `_transfer_begin`/`_transfer_end`/`step_3_deploy` 設旗標位置）。
- 離線判定 = WS 通道事件：
  - `handle_client` recv 收到 FIN/RST/錯誤 → finally → `unregister_connection`
    → 標離線 + panel log「📴 離線 (WS 連線中斷)」。
  - `send_pkt` 發送失敗（RST/EPIPE/半開重傳超時）→ 關 socket 觸發同一清理路徑。
- `handle_client` TCP keepalive 補 Windows 分支：`SIO_KEEPALIVE_VALS`
  （idle 10s / 每 3s 探）→ 半開連線由通道本身 ~20s 內偵測到。
- `_scan_files`（Step 0 → 4 重建文件索引）：送 `0x2009` 剷 `/manifest.json`
  → 等 WS 斷線（= slave 已剷除並 self-reset）→ 等回線 → 輪詢 `fs_scan_busy`
  歸零逐台回報（唔加新指令，重用舊指令 + 通道斷線做確認）。

### 28.2 WebMaster：移除定時保活/逾時判定

- `heartbeat_loop` 不再每 2s 送 0x1101 STATUS_GET、不再「30s 無回應標離線」，
  只保留 device_list UI 廣播。離線 = `/ws/{slave_id}` finally → unregister。

### 28.3 slave：修「4. 重建文件索引 (Scan)」——唔加新指令，剷除→重啟→開機重掃

- 根因：FsScanTask 係 one-shot——掃完 `_shutdown()` 把 affinity 設 `(0,0)`
  停咗自己；之後 0x200B 只設 `fs_scan_requested` 旗標，冇人消費。
- 修正 1：`fs_manager.scan_all()` 設旗標後 `tm.set_affinity("fs_scan", (0,1))`
  重新武裝 → TaskManager 重啟任務 → 開掃（0x200B console 手動重掃用）。
- 修正 2（最終方案，重用舊指令 0x2009、唔加新指令）：
  - master `_scan_files()`（Step 0 → 4）送 `0x2009 FILE_DELETE /manifest.json`；
  - slave `on_file_delete` 特例：剷走 manifest 後**唔回覆**、`[FileScan]` log、
    `machine.reset()`；
  - master 以 **WS 斷線 = 已執行** 做確認（0x2004 係 chunk ACK、0x2006 係
    查詢回覆，語意都唔啱呢個場境）；設備回線後輪詢 `fs_scan_busy` 歸零 →
    「✅ 文件索引重建完成」。
  - 重啟同時天然避開 one-shot 問題：boot 重新註冊 fs_scan 任務 affinity (0,1)。
- SD（/sd/.manifest.json）——delta 維護 + 主動掃描重建：
  - 設計原則：SD manifest **平時 delta 維護**（協議上傳/下載先紀錄），
    唔主動掃；只有 0x200B(target=1) 主動掃描先重建自己張表。
  - `fs_manager.scan_sd()`：置 `fs_scan_sd_busy` 旗標（finally 清零）+
    `[FileScan]` log + 每檔 `sleep_ms(0)` 讓步（render 同喺 core1）。
  - `status_actions` `fs_scan_busy` provider 覆蓋 local/SD 兩種掃描。
  - master `_scan_files` 拆三個範圍：1=本地（剷除+重啟）/ 2=SD
    （0x200B target=1 → busy=1 確認開始 → busy=0 確認完成）/ 3=兩樣。
- 看門狗分析：`fs_scan` 跑 **core1**（`default_affinity=(0,1)`）、每次 loop 只
  hash 一檔、每 256KB 讓步；WDT 由 core0 餵 → 掃描唔會觸發 WDT。
- 後備 workaround（舊韌體）：手動刪 `/manifest.json` → 軟重啟 → 開機自動重建
  （詳見 `doc/03_notes/12_upload_wdt_diagnosis.md` §6）。
