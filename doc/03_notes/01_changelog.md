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
  > ⚠️ **欄位名已於 2026-10 更名為 `reply_cid`**（wire 不變，只有原始碼／文件的名字變）。
> ⚠️ **語意已被 §30（2026-10）取代**：`IDENTIFY_REQ.reply_addr` 不再改 `master_cid`（只決定那一封 `0x100E` 的位址），且 `master_cid` 已落盤（`@node.master_cid`，P2）。本行保留為當時的歷史記錄。
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
| 0x100D | IDENTIFY_REQ | Master→Slave | `reply_addr(u16)` | 逐 address 素描；帶 reply_addr 告知 master_cid ⟶ **已被 §30 取代**（只點名，不改方向）|
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

---

## 29) `proto.py` 寫入路徑：成本模型修正 + 按大小選寫法（2026-09）

### 29.1 為什麼會去看這一行

真機量到 `StreamParser.feed()+pop_frame()` 每幀 ~870us（ESP32-S3 @160MHz），與預期不符。

### 29.2 根因：MicroPython 切片賦值的成本模型

**「寫入長度」不是成本，成本是「目標視圖的長度」。** 真機二維實測
（`test/protocol/bench_slice_assign.py`、`test_proto_writes.py` §7）：

| 目標 buffer | 寫 16B | 寫 113B | 寫 512B | 寫 2000B | 寫 4000B |
|---|---|---|---|---|---|
| `b[a:c] = x`（舊） | 34 | 34 | 34 | 34 | 34 | ← 256B buffer
| `b[a:c][:] = x`（新） | 21 | 28 | 58 | 170 | 320 |

- 舊寫法 `b[a:c] = x`：成本 = **0.076us × 緩衝區總長**（寫 1 byte 與寫 4000 bytes 一樣貴）
- 新寫法 `b[a:c][:] = x`：成本 = **0.076us × 寫入長度 + ~20us 固定**
- 兩者交叉點：`寫入長度 + 256 < 緩衝區長度`

**所以「寫入接近填滿緩衝區」時舊寫法反而較快（最多 ~6%）**，不能無腦全改。

### 29.3 為什麼當初那樣寫（查證結果）

`mp4_testkit/lib/proto.py` 的舊版是：

```python
_viper_append(self._buf, data, self._end, ln)      # @micropython.viper 逐 byte 迴圈
_viper_compact(self._buf, self._start, self._end, keep)
```

`doc/01_protocol/08_performance_benchmark.md:109` 記載的「**改 memoryview slice 賦值
（memmove，替代 viper 逐 byte）**」以及「快 ~17x」，是**相對 viper 逐 byte 迴圈**。
本次改動**沒有退回 byte 迴圈**，只是把「memmove」的成本假設修正——
原註解「memoryview slice 賦值 = C 層 memmove」只在緩衝區不大時成立。

### 29.4 改動（語意完全相同，只影響效能）

`lib/sys/proto.py` 三個寫入點，全部按 `寫入量 + 256 < 緩衝區` 選寫法：

| 位置 | 舊 | 新 |
|---|---|---|
| `StreamParser.feed()` append | `self._mv[e:e+ln] = data` | 小量→`self._mv[e:e+ln][:] = data`；接近填滿→原寫法 |
| `StreamParser.feed()` compact | `self._mv[:keep] = self._mv[s:e]` | 同上規則 |
| `_write_frame()` payload | `b[a:c] = payload` | 同上規則 |

⚠️ `bytearray[a:c]` 回傳的是**副本**（不是視圖），所以 `_write_frame` 要求 `b` 是 memoryview；
`Proto.pack_into()` 對非 memoryview 的輸入自動包一層。

### 29.5 實測

| | 舊 | 新 |
|---|---|---|
| `feed+pop`（113B 幀, max_len=8192） | 876 us/幀 | **196 us/幀（4.5×）** |
| `feed+pop`（max_len=64） | 78 | 70 |
| 大視圖寫 16B（8205B buffer） | 631 us | **24 us（26×）** |
| 寫 4000B（4115B buffer，接近填滿） | **324 us** | 327 us（分界選回舊寫法，不吃虧）|

### 29.6 驗證（核心零件，所以驗得比較兇）

- `test/protocol/test_proto_writes.py`（**PC + 真機都能跑，89 項**）
  - §1 與舊寫法**隨機對拍** 120 輪（隨機幀長/切包/4 種 max_len）逐位元組相同
  - §2 compact 邊界，含**來源與目的重疊**（唯一有語意風險的情況）
  - §3 輸入型別（bytes/bytearray/memoryview）與**餵自己緩衝區**的別名情況
  - §4 錯誤行為一致（長度不符的例外型別、唯讀來源、容量不足）
  - §5 `pack` / `pack_into` / `_build_frame` 對**獨立參考實作**（bytes 拼接）逐位元組相同
  - §6 500 輪 feed 記憶體不成長；建/丟 parser 第二輪不再成長
  - §7 成本曲面 + 「沒有任何 (buffer, 寫入量) 組合比舊寫法慢」
- `router_selftest.py` §14（新增）+ 全套 499 項；`router_board_test.py` 真機 114 項

### 29.7 已知取捨（誠實記錄）

`Proto.pack()` 為與 `pack_into()` 共用內核而呼叫 `_write_frame()`，**多一次 Python 函式呼叫**。
交錯量測（順序輪換，消除位置偏誤）顯示這塊固件上「一次呼叫」約 **+80us 固定成本**
（小封包比例難看，如 113B 1.4~2.0x；大封包 1.0x）。**寫入本身沒有退步**（§29.5 已證）。
- 這塊固件每個基本操作都比正常 MicroPython 慢 10~100 倍（連 `sys.modules` 都是空的），
  正常固件一次呼叫 ~2us。
- 若日後真的要壓這 80us：把 `_write_frame` 的內容在 `pack()` 就地展開即可
  （與 `pack_into()` 各一份，用 §5 的逐位元組測試鎖住兩份不分歧）。
  **目前選擇保留單一內核**，因為在核心檔案裡放兩份組幀邏輯的風險大於一次呼叫。

### 29.8 其他同類寫入點（**未動**，屬其他子系統）

`circuit_bus.py` `poll()` 的 `pv[:n] = raw_bytes`、`_commit()` 的 `cview[:take] = view[:take]`、
`net_bus.py` 同類路徑 —— 目標視圖都是 4115B 級，預期各 ~320us/次。要用同一套方法量過再改。

---

## 30) `0x100D` 只點名、不改方向：`master_cid` 收成單一寫入者（2026-10）

### 30.1 問題：一個狀態兩個寫入者

`bus.master_cid`（「我以後聽誰的」）原本有**兩個**指令層寫入者：

| 指令 | 職責 | 寫什麼 |
|---|---|---|
| `0x1016 SET_MASTER` | **設定方向** | `master_cid` + `role="slave"` + `save_node()`（落盤）|
| `0x100D IDENTIFY_REQ` | **點名** | `master_cid = reply_cid`（記憶體，不落盤）|

根因不是誰亂寫，是**`_reply()` 少了一個參數**：它只能從全域 `bus.master_cid`
拿目的位址，所以 `on_identify_req` 要回 `0x100E` 給掃描者，就只能先
**把 `reply_cid` 塞進 `master_cid`**。

副作用（實測）：

| # | 後果 |
|---|---|
| 1 | **掃描 = 無聲搶奪**：面板 B 按一下「掃描」→ 射程內**每台**節點都改認 B，面板 A 完全不知道 |
| 2 | **狀態自相矛盾**：掃描後 `master_cid=0x0001` 但 `role=None`，`node_state()["bound"]` 卻回 `True` |
| 3 | **查不到真值**：掃描不 `publish_node()` → `0x1101{keys:"node.master_cid"}` 讀到**過期**快照 |
| 4 | **重開機不一致**：掃描不落盤 → 重開機還原成 btree 的值，但這輪開機期間一直回給掃描者 |

### 30.2 修法：把位址還原成「這一封」的參數

```python
# net_actions.py —— _reply 補上目的位址參數
def _reply(ctx, rsp_cmd, fields, addr=None):
    ...
    ctx["send"](Proto.pack(rsp_cmd, payload,
                           addr=bus.master_cid if addr is None else addr))

# net_actions.py —— on_identify_req：刪掉那個寫入
-   if reply_cid != ADDR_BROADCAST:
-       bus.master_cid = reply_cid
-   _reply(ctx, CMD_IDENTIFY_RSP, {...})
+   _reply(ctx, CMD_IDENTIFY_RSP, {...}, addr=reply_cid)
```

**語意**：`reply_cid` = 回信地址（單次）；`master_cid` = 通訊地址（持續）。
`0x100D` 回答「你是誰」，`0x1016` 回答「我以後聽誰的」—— 前者是**問題**，後者是**狀態**。

改完後指令層只剩 **1 個** `master_cid` 寫入者（`on_set_master`）。

### 30.3 影響 / 驗證

- 功能**沒有少**：掃描照樣問到誰是誰，`0x100E` 的 addr 仍等於 `reply_cid`
  （`app.py` 的 ADDR 過濾靠它讓其他節點丟掉這封回信）。
- 掃描過的節點不再知道回給誰 → `master_cid` 維持 `0xFFFF`；綁定時 `_do_bind`
  本來就會送 `0x1016`，正常流程不受影響。
- 回歸測試：`test/protocol/test_master_writer.py`（19 項；把舊寫入加回去會 FAIL 7 項，
  含 AST 靜態檢查「只有一個寫入者」）。
- 文件同步：`01_nc4_protocol.md` §定址模型、`02_command_index.md`（0x100D/0x1016）、
  `17_esp_stream_controller.md` §2.3、`18_pixel_panel_control_path.md` §5.1、
  `19_remote_control_plan.md`（零件清單 / 誰會寫它）、`sys_bus.py` 註解、
  `ui/lvgl/page/remote.py` `_do_scan` docstring、Test_Peer `Core_Manager.py` 註解。


---

## 31) 節點發現與配對：抖動 ＋ 傳輸層無關的位址解析（2026-10）

> 完整設計與決策見 **`todo/05_node_pairing.md`**；本節只記「改了什麼、為什麼」。

### 31.1 `0x100D.timeout_ms` —— 抖動（避免同時回話撞車）

廣播點名會讓射程內**所有** Slave 在同一瞬間回話 → 在 ESP-NOW 空中／RS485 匯流排上是
**硬碰撞**，會整批掉。新增 `timeout_ms`：各台各自隨機延遲 `[0, timeout_ms]` 才回，
把「同時」攤成「序列」。`0`／缺席 = 不抖動（＝舊行為）→ **追加欄位，向後相容**
（`SchemaCodec.decode` 的 `pos >= plen → break` 讓缺席欄位「不存在於 dict」而非補 0）。

**只有 `0x100D` 抖動** —— 全專案「廣播出去、N 台回話」只有它（其餘廣播
`0x1401/0x1501/0x1502/0x3105/0x3106` 都單向）。範圍由「寫在 `on_identify_req` 裡」保證，
不需要執行期判斷。

### 31.2 ★ 延後發射掛在**解碼鏈**，不是任何一條 bus

```
tasks/bus_decode.py  _TxOut / _pending / _fire_due()
```

- `ctx["send"]` 就是 `_TxOut`（每條 bus 建一次，不是每幀）→ handler 只知道
  「延遲幾毫秒」，**不知道 MAC、也不知道哪條管子**
- **每條管子深度 1**（新掃描覆蓋舊的未發項）
- **延後發射必須在「收幀當下」把來源定下來**：到期時「剛剛講話的人」早就換了
  → 存進 pending 的是 `(fire_at, bus, frame, src)`，到期用 `bus.write_to(src, frame)`
- **為什麼不放各 bus**：抖動是**所有共享媒介**的需求（RS485 更需要），而三條 bus
  沒有共同基底類別；放解碼鏈＝三條都天生具備，未來加管子不用再改
- 測試用 AST 釘住：三條 bus 裡**不得**出現 `_pending` / `defer` / `fire_due`

### 31.3 ★ `NowBus.write()` 依幀頭 `addr` 解析目的地

`signal_router._forward()` 只會呼叫 `dst.write(frame)`，**沒有目的地位址參數**——
所以「UI → vBus → Router → 射頻管」要成立，管子必須自己解析：

| 幀頭 `addr` | 讀哪份既有記錄 |
|---|---|
| `0xFFFF` | 廣播 |
| `== bus.master_cid` | `bus.master_mac`（**Slave 端**）|
| 其他 | `PeerRegistry.by_cid()`（**Master 端**）|
| 查不到 | 「剛剛講話的人」（保留既有回覆語意）|

→ **`by_cid()` 從「零呼叫者的死碼」變成必要零件**（文件 `sys_bus.py:31` 早就這樣寫，
是程式碼漂走了）。`@node.targets` 也回到「**只存 cid**」—— MAC 由 peers 表查。

### 31.4 Slave 記 `master_mac`；`_claimed` 實現「重啟才能換 master」

- `@node.master_mac`（新增）：Master 的**射頻層**位址，取自 `0x1016` 收幀當下的來源
  —— 與 `0x100E` 帶回 cid 的方式完全對稱（**配對是雙邊對稱的記錄**）
- `bus.pair_claimed`（純記憶體 1 bit，開機歸零）：`master_cid` 是半永久的，
  光靠它會「永遠不能被換」；有了這 1 bit 就得到「**重啟一下，再讓新的 Master 執行**」
- **`0x1016{0xFFFF}` = 解除**（清 `master_cid`/`master_mac`/`role` ＋ `pair_claimed=False`
  重新開放認領）；`_do_unbind` 會**通知對方**（＝ `todo/04` Task 1）
- **值沒變不寫 flash**：`save_node()` 是 3 個 btree key + `flush()`，而 `kv_set`
  沒有變更偵測 → 沒這個守衛就是「掃描每輪每台一次抹寫」。用**前後快照比對**

### 31.4b ★ 「0x100D 不改方向」≠「回不去」（最容易誤解的一點）

| | 誰決定 | **掃描回覆**（0x100E）| **其他回覆**（0x1102/0x3102…）|
|---|---|---|---|
| **協議層 addr**（幀頭）| `_reply(addr=…)` | `reply_cid`（**payload** 帶來的）| `bus.master_cid`；**未設 = `0xFFFF`** |
| **射頻層去向**（MAC）| 管子自己 `resolve()` | **這一幀的來源** | `master_mac` → `by_cid()` → 剛剛講話的人 |

**Master 的 MAC 不是用 payload 傳的** —— ESP-NOW 每一幀到達時驅動層就附帶了來源
（`espnow.recv()` 的 `peer`）。所以 `0x1016` 一次帶來兩層：payload 的 `master_cid`
（協議）＋**收幀當下的來源 MAC**（射頻）→ 存成 `master_mac`。與 `0x100E` 完全對稱。

`0x100D` 的 `reply_cid` 只管**那一封**；`0x1016` 管的是**以後每一封的預設**。
→ **任何情況下都送得回去**（未配對時協定 addr 就是 `0xFFFF`）。
測試 `test/protocol/test_node_pairing.py` §15 把這個性質釘住。

### 31.5 UI 的分界線：**MAC 不得出現在協議層或 UI**

| 入口 | 語意 | 走 Router？ |
|---|---|---|
| `disp.exec_cmd(cmd, args, ctx)` | 「我呼叫一個函式」—— **絕對內部執行** | ❌ |
| `vbus.inject(frame)` | 「我發起一幀」—— 可能被轉發出去 | ✅ |

- `CircuitBus.inject()` 🆕（從 `ScheduleTask._inject` 搬出來 —— 通用能力不該是
  scheduler 的私產）；`ScheduleTask` 建 vBus 時**同時註冊具名服務** `"vbus"`，
  並提供 `sys_bus.get_vbus()` 讓所有呼叫端共用查找順序
- `remote.py`：`_tx()` 改走 vBus（**移除** `now.add_peer` / `write_to` / `broadcast`）
- 🆕 `pixel_controller.py`：舊版**直接抓 `now._esp`（私有屬性）自己 `send()`**，
  連 `connected`、統計、Router 全繞過 —— 比 `remote.py` 舊版更糟。已改走 vBus
- 測試用 AST 釘住：`slave/ui/` 底下**不得**出現 `NowBus` / `add_peer` / `write_to`
  / `_esp` / `broadcast` / `import espnow|network`

### 31.6 ★ `NowBus.init()` 在 AP-only 時**靜默失效**（真機實測）

實測（ESP32-S3）：`STA-only` → `send()` 正常；**`AP-only` → `send()` 回
`ESP_ERR_ESPNOW_IF (-12396)`**；兩者都開 → 正常。**ESP-NOW 綁的是 STA。**

而舊碼的條件是 `not sta.active() and not ap.active()` → **AP 開著就整段跳過**，
但 `ESPNow().active(True)` 與 `add_peer()` 都「成功」→ `connected = True`，
之後**每一次 send() 都失敗**，而 `send()` 的錯誤處理只有一個計數器
→ **UI 顯示 ON，卻什麼都送不出去**。已改成「不論 AP 狀態，STA 一定要開」。

### 31.7 真機驗證結果（`/dev/cu.usbmodem11401`，1 板）

| 項目 | 結果 |
|---|---|
| `machine.unique_id()` vs ESP-NOW STA MAC | **相同**（`24EC4A2CA430`）→ `slave_id` 可直接當射頻位址 |
| AP MAC | `24EC4A2CA431`（+1）—— **與 ESP-NOW 無關** |
| `urandom` / `random` | 兩者都有 `getrandbits` → 抖動用 `urandom` |

### 31.8 測試與文件

- `test/protocol/test_node_pairing.py` 🆕 **68/68 PASS**（階段 1~4 全涵蓋）
- `test/protocol/test_master_writer.py` 19/19（C3 回歸；AST 斷言改成
  「**只有 `on_set_master` 能寫 `master_cid`**」—— 解除分支本來就需要第二個賦值）
- 實作時測試抓到的 3 個真 bug（假隨機聚簇、`cid_conflict` 沒重算、解除沒通知對方）
  → 見 `todo/05` §11

---

## 32) 節點配對的**真機驗證**：三個真 bug ＋ 一個開發流程陷阱（2026-10）

`todo/05` §12 的真機驗證（兩塊板：面板 `/dev/cu.usbmodem11401` ＋ B 板
`/dev/cu.usbmodem101`）。腳本在 `temp/`：`board_util.py`、
`two_board_jitter_test.py`、`two_board_status_probe.py`、`probe_panel_reset.py`。

**§12 九項全數通過**；抖動、`reply_cid`、認主、閃寫保護、重開機換手都在真機上量到了
（數據見 `todo/05` §12.1~§12.3）。以下記「驗證過程抓到的東西」——
這一輪真正有價值的產出其實是這四件事。

### 32.1 ★ `NowBus.send()` 帶 hex 字串會 `ValueError`：**定向發射從來沒成功過**

```
espnow.send("ffffffffffff", b"x")      → ValueError: invalid buffer length
espnow.send(b"\xff\xff\xff\xff\xff\xff", b"x") → True
```

`mac_bytes()` 的註解早就寫了「espnow 只吃 6-byte bytes」，但**收斂點漏了**：
`resolve_addr()` 回傳的是 `PeerRegistry.mac` / `bus.master_mac`（**hex 字串**），
一路餵進 `send()` / `add_peer()`。

而 `add_peer()` 的例外被吞掉、**還回 `True`** —— 所以：
**UI 顯示綁定成功、peer 表看起來有東西，但每一次定向發射都在射頻層失敗。**
廣播走 `BCAST_MAC`（本來就是 bytes）所以沒事 → P5 的離線/單板冒煙測試看不出來。

修法：`mac_bytes()` 套在 `NowBus` **所有**入口（`add_peer`/`learn_peer`/`has_peer`/
`send`/`write_to`），認不出來的位址回 `False` 並印訊息，不再靜默說謊。

### 32.2 ★ 「已經在表裡」被當成失敗（`ESP_ERR_ESPNOW_EXIST`）

修完 32.1 之後真機立刻冒出第二個：`add_peer("ffffffffffff")` 回 `False`，
因為 `init()` 開機就把廣播位址加進**硬體表**，卻**沒同步記進 `self._peers`**
（它直接呼叫 `self._esp.add_peer`，繞過了簿記）→ 之後每一次「確保存在」都撞 `EXIST`。

為什麼這會傷到配對：`learn_peer()` 把 `add_peer()` 的結果往上傳，
於是對**已經認識的對象回 `False`** → 配對流程誤判成「學不到對方」，明明對方就在表裡。

修法兩處：`init()` 改走 `self.add_peer(BCAST_MAC)`；
`add_peer()` 用新的 `_is_exist_error()` 把 `EXIST(-12395)` 當**成功**（真機例外形狀是
`OSError(-12395, 'ESP_ERR_ESPNOW_EXIST')`，離線測試得自己造，所以錯誤碼與訊息字串都認）。

> 這個坑我在**自己的測試腳本上又踩了一次**（`two_board_status_probe.py` 第二輪
> 就死在 `add_peer`）—— 目前提是「把已存在當失敗」，代價立刻可見。

### 32.3 `0x1016` 是 fire-and-forget：「送了」不等於「到了」

實測 **送 5 次 `0x1016` 只到 4 次**（乾淨的兩板環境、ESP-NOW 廣播、無 ACK）。
一開始我把它誤判成「同一次開機裡第二個 `0x1016` 不生效」，
實際上是**掉幀** —— 加上「送 → 用 `0x1101` 查 → 沒到就重送」之後就全數穩定。

→ `0x1016` 沒有 ACK 是**設計如此**（`on_set_master` 的 docstring 已寫明：
被拒絕時回覆會跑到舊 master，所以拒絕只能靠 `0x1101` 查）。
本輪確立：**請求者的正確用法就是「送＋查＋重送」**，
而 `0x1101 STATUS_GET{keys:"node"}` 的空中查詢在真機上可用（20~30ms 往返）。
這條路同時就是 `todo/04` D1 / Task 2 指定的查法 —— 順便完成。

### 32.4 ⚠️ 開發流程陷阱：`Ctrl-D` 軟重開機會**耗盡內部 SRAM**

**症狀**：面板開機失敗，而**失敗點每次都不一樣** ——
`Core_Manager.py:10 MemoryError 512 bytes`、下一次 `driver/pixel_drv.py:13 MemoryError 1756 bytes`，
連四次都失敗。**失敗點會往前跑**就是洩漏的指紋（不是程式碼 bug）。

同一次開機前量 `esp32.idf_heap_info(esp32.HEAP_DATA)`：

```
total=236360  free=624    largest=200     ← 236 KB 的堆只剩 200 bytes 最大塊
total=22308   free=4      largest=0       ← 完全耗盡
```

**根因**：ESP32-S3 的 `MPY: soft reboot` **不會拆掉 WiFi / ESP-NOW 驅動**，
驅動佔的內部 SRAM 每次軟重開機都留著。而 `Ctrl-D` 與
**`mpremote` 的預設收尾都是軟重開機** → 開發時反覆上傳／進 REPL 就會累積到開不了機。
⚠️ `gc.mem_free()` 這時還報 **7 MB**（那是 PSRAM）→ **完全看不出問題**。

**解法**：`machine.reset()`（hard reset）。代價是 USB-CDC 重新列舉、
`/dev/cu.usbmodem*` 消失約 7 秒再出現 → **控制代碼必須重開**（`temp/board_util.py`）。

**併發教訓**：`Ctrl-B` 在 **friendly REPL 是無作用的**（只在 raw REPL 有意義），
所以「送 `\x02` 當重開機」是錯的 —— 這讓第一次除錯多繞一圈（面板日誌全空，
看起來像沒開機，其實是根本沒重開）。重開機只有 `Ctrl-D` 或 `machine.reset()`。

### 32.5 我自己的驗證方法也錯了一次（值得記）

第一版探針去讀面板的 **`config.json`** 的 `node` 節，量到四個「失敗」。
實際上節點狀態存在 **btree**（`ConfigManager.kv_set`），`config.json` 裡永遠沒有 `node`。
→ **驗證要走專案自己的機制**（`0x1101`），不要另外找一個檔案讀 ——
換一個檔案讀只會得到另一組假答案。

### 32.6 這一輪的程式碼改動

| 檔案 | 改動 |
|---|---|
| `slave/lib/sys/now_bus.py` | `mac_bytes` 收斂；`_is_exist_error()`＋`ESPNOW_ERR_EXIST`；`add_peer` 把 EXIST 當成功；`init()` 改走 `self.add_peer(BCAST_MAC)` |
| `slave/action/net_actions.py` | `on_set_master` 成功路徑新增 `認主` 日誌（＝ flash 寫入次數的可觀察面）|
| `test/protocol/test_node_pairing.py` | **107/107 PASS**（+12：§18 `_is_exist_error`/EXIST 語意/`init` 簿記、§19 認主日誌＝flash 次數）|
| `todo/05_node_pairing.md` | §12 改寫成驗證結果（§12.1~§12.5）|

> 前一輪（§31）的程式碼改動沒有動到；本輪只修「真機才會現形」的三個靜默失敗。

### 32.7 順手收掉：測試與裝置腳本的 import 路徑（同日）

`test/protocol/night_run/REPORT.md` 早就記了「**`lib/` 三級重構後 test/ 腳本未同步
import 路徑**」，這次一次清掉：

| 檔案 | 修正 |
|---|---|
| `test/protocol/router_selftest.py` | **75/75 PASS**（基線是崩潰）。三個問題：`build()` 沒註冊介面 → route 全進 `_pending`；`VBUS → "self"` 的期待過時；★ `Router._by_id` 用 `id()` 當索引，測試的臨時 `FakeBus` 被 GC 後位址重用 → 查到死人名字 |
| `test/protocol/test_proto_hotpath.py` | **9 PASS / 0 FAIL**。`lib.proto` → `lib.sys.proto` ＋ 補 `sys.path` bootstrap |
| `test/protocol/test_decode_perf.py` | 4 處模組路徑 |
| `test/protocol/test_proto_speed.py` | 模組路徑 ＋ 補 bootstrap（以前只能從專案根目錄跑）|
| 其餘 **15 個裝置腳本** | `lib.X` → `lib.sys.X` 共 24 處（tft/sd/thread/husb238/ui/bench_net）|

原則：**只改程式碼行**（行首是 `from`/`import` 的）——
說明文字與註解裡提到的舊路徑是**歷史敘述**，改了反而看不懂。

`test/protocol/`、`test/buffer/`、`test/thread/` 在 PC 上**全綠**。
`test/ui/ui_test_tool.py`（硬寫 `/ui/lvgl/src`）與 `espnow_*.py` / `rs485_probe.py` /
`wtt_rx_probe.py`（需要 `network`/`machine`）是**裝置專用**，在 PC 上失敗是正常的。

### 32.8 補驗：換 master **不必重啟**（先解除就好）

`todo/05` §12 上一輪只驗了「重啟後換手」，漏掉「**先解除、再換手、全程不重啟**」。
補測（`temp/probe_master_swap.py`，**整場只開機一次**）：

```
claim(0x0002)     → 2        認主 0x0002
claim(0x0003) ×3  → 2 不變   忽略 ×3
claim(0xFFFF)     → 65535    解除
claim(0x0003)     → 3  ★★    不重啟直接接手（第 1 次就成功）
claim(0x0007) ×3  → 3 不變   忽略 ×2
claim(0xFFFF)     → 65535    解除
```

→ 對照 `todo/05` **D8**，兩條路都成立且都保留：

| 路徑 | 行為 | 為什麼保留 |
|---|---|---|
| A **直接**換 B | 被 `pair_claimed` 擋掉 → 要重啟 | 防止「廣播掃描就把人家的 master 搶走」|
| A → **解除** → B | `pair_claimed = False` → **不必重啟** | 給「人明確要換手」一條不必動電源的路 |

⚠️ 教訓：**同一條流程的兩條分支，只驗一條不算驗完。**
「拒絕換手」很容易蓋住「解除後可換手」——因為兩者都表現成「狀態沒變」。

---

## 33) 通訊頻道：**執行期可換、不必重啟**（2026-10 真機兩板實測）

### 33.1 問題

「Master 發現 Slave 換了通訊頻道，需不需要重啟？」（使用者不希望 Master 重啟）

帳面上像是「要」：`NowBus.init(channel=N)` 只在 `sta.active()` 為 False 時才設頻道
（`now_bus.py` 的 `if not sta.active():`），而 `0x1301 NOW_INIT` 只有
`action` 0=查詢／1=開／2=關 —— **沒有換頻道**。STA 一開著，`init(channel=N)` 會被**靜默忽略**。

### 33.2 實測結論：不必重啟

`temp/probe_channel_switch.py`（兩板都不重啟，只有進 REPL 時 Ctrl-C）：

| 回合 | 送方 | 收方 | 結果 |
|---|---|---|---|
| B1 | ch6 | ch6 | **收 12/12 幀** ✓ 基線 |
| B2 | → ch11 | ch6（沒跟著換）| **收 0/12** ← 換頻道**真的生效**，不是沒作用 |
| B3 | ch11 | → ch11 | **收 12/12 幀** ★★ 兩端都執行期換頻道 → 通了 |

`sta.config(channel=N)` 在 STA 已 active 時**有效**（6 → 11 → 6 當場驗過）。
→ **缺口純粹在 `NowBus` 與缺指令，不是硬體限制。**

### 33.3 ★ 每個 peer 可以有自己的頻道（Master 跟隨 Slave 的關鍵）

真機量到的形狀：

```python
e.add_peer(mac, channel=11)          # ← 接受
e.get_peer(mac)
# (b'...mac...', b'\x00'*16, 11, 0, False)
#                            ↑ channel 是第 3 欄，0 = 用當前頻道
```

所以 Master **不必為了某台 Slave 而搬家**：登記時記住它的頻道，送的時候驅動自己切過去。

### 33.4 ⚠️ 但無線電只有一顆：**TX 可以多頻道，RX 只能待一個**

這是本題**真正的設計限制**，不是實作細節：

| | 能力 |
|---|---|
| **定向 TX** 給不同頻道的 peer | ✅ 用 per-peer channel，送完自動切回 |
| **廣播** `0x100D` | ❌ 只會發在**當前**頻道 → 只找得到同頻道的 Slave |
| **RX** | ❌ 停在某一頻道時，聽不到其他頻道的任何東西 |

→ 要跨頻道發現，只能**頻道跳躍掃描**（像 BLE）：逐頻道切過去、廣播、收一輪、再換。
**這件事不需要重啟**，但需要一個「掃描」任務。
→ 也因為 RX 的限制，Slave **無法主動告訴 Master「我換頻道了」**（Master 聽不到）
—— 只能由 Master 掃到、或由使用者指定。

### 33.5 順手抓到的真 bug：`espnow.recv()` 會回 `(mac, None)`

實測 3 秒的接收迴圈裡出現 **2871 次** `(mac, None)` —— 那是 peer event／控制訊框，
**不是資料幀**。

- `poll()` 沒事（用 `not msg` 擋，None 與 `b""` 都擋）
- **`recv()` / `recv_timeout()` 會炸**：只擋 `peer is None` → `bytes(None)` → `TypeError`，
  而且會被 `discover()` 的迴圈原樣拋出去
- `discover()` 目前**零呼叫者** → **潛在** bug（與 `by_cid()` 同性質：接上去才會咬人）

已修（兩個方法都擋 `msg is None`），並在 `test_node_pairing.py` §20 加回歸
（**112/112 PASS**）。

> ⚠️ 我自己的測試 harness 也踩了同一個坑：監聽腳本沒防 `(mac, None)` → 一收到就
> `TypeError` 爆掉 → **「什麼都沒收到」長得跟「頻道不對」一模一樣**，第一輪因此
> 得到兩個假結果。教訓：**驗證腳本一定要把 stderr 印出來**。

### 33.6 實作：`set_channel` / `del_peer` / `apply_peers` / `0x1301 action=3`

使用者定案的兩個語意：

- **頻道＝區分不同網路**，不是漫遊 → **不做跨頻道兼容、不需要掃描**。
  同頻道的 Master／Slave 才看得到彼此；換頻道就是「搬到另一個網路」。
- **peer 表上限 20**，UI 是 Windows 式的 **可選／已選兩張表**，
  滿了**不自動淘汰**，直接拒絕並提示「請先移除一些」。

| 新增 | 位置 | 做什麼 |
|---|---|---|
| `MAX_PEERS = 20` | `now_bus.py` | ESP-IDF 硬體上限；廣播固定佔 1 格 |
| `set_channel(n)` | `now_bus.py` | **執行期**換頻道；回報**實測值**而不是假設成功 |
| `del_peer(mac)` | `now_bus.py` | 接上 `espnow.del_peer`，硬體表終於**能減** |
| `apply_peers(keep)` | `now_bus.py` | 把表**重建**成「廣播 ＋ 已選清單」（兩張表的「套用」）|
| `action=3` ＋ `channel(u8)` | `0x1301`／`now.json` | 換頻道的指令入口 |

**修掉的靜默失敗**：`init(channel=N)` 在 STA 已開時**靜默忽略** channel
（呼叫端以為設了、其實沒有）→ 改走 `set_channel()` 並印出「要求 vs 實測」。

**向後相容**：`channel` 是追加的第二欄位。SchemaCodec 兩條解碼路徑都有
`if pos >= plen and tc != 5: break` → 舊客戶端只送 1 byte（`action`）時
`args` 裡根本沒有 `channel` → 擋掉並提示，**不會誤動作**。

#### 真機驗證（`temp/probe_channel_cmd.py`，**10/10 PASS**）

面板跑正式韌體、**只開機一次**，B 板用 ESP-NOW 送指令並用 `0x1101` 確認：

| | 動作 | 結果 |
|---|---|---|
| C1 | 雙方 ch6 | `0x1101` 查得到 ✓ 基線 |
| C2a | 送 `0x1301{3,11}`，B 留在 ch6 | **查不到** ← 換頻道真的生效 |
| C2b | B 也換 ch11 | **又查得到** ★★ 應用程式層執行期搬過去了 |
| C3 | 送 `0x1301{3,6}`，雙方回 ch6 | 查得到 ✓ |

```
📶 [NOW-Bus] 頻道 → 11（實測回報 11）
📶 [NOW-Bus] 頻道 → 6（實測回報 6）
開機 「Boot complete」×1
```

傳輸層（面板 REPL）：

```
DELPEER base=1 after_add=4 after_del=3            ← 格數真的下降
📋 peer 表套用：已選 1／新增 0／移除 1／失敗 0 → 現有 2
DELPEER after_apply=2 peers=['FFFFFFFFFFFF', '010203040506']
DELPEER 廣播還在嗎 = True
```

離線測試 `test_node_pairing.py` **132/132 PASS**（+20：§21 換頻道／del_peer／
apply_peers／上限，§22 `0x1301 action=3` 含舊客戶端不誤動作）。

順手：`now_actions.py` 的 `import ubinascii` 改成 try/except（與 `now_bus.py` 一致），
否則這個模組在 CPython 上根本 import 不了、無法離線測試。

#### ⚠️ 兩個 harness 教訓（都不是韌體問題）

1. **上傳要在板子「站穩」之後**：在 `machine.reset()` 剛觸發、USB-CDC 還在重新列舉時
   上傳會**全部失敗**，而我第一版把 stderr 丟掉 → 測到舊韌體還以為是功能壞了。
   → 上傳後**一定要在板上驗證新程式碼真的在**（`print('del_peer' in open(...).read())`）。
2. **同一塊板連續跑多輪要先把舊的 ESP-NOW 關掉**：舊 `ESPNow` 實例還 active 時
   直接再建一個，`recv()` 會拋 `ValueError: ESPNow.recv(): buffer error`
   → 例外處理不能只接 `OSError`。

#### 還沒做

**UI 的兩張表**（可選／已選、上限 20、滿了提示）還沒接。傳輸層與指令都已經就緒，
`apply_peers()` 就是「套用」那顆按鈕要呼叫的東西。

---

## 34) ESP-NOW 設定頁的資料層（2026-10；UI 尚未接）

使用者要兩頁：**ESP-NOW 設定**（傳輸層）與**遙控器設定**（應用層）。本節只做前者
的資料層 —— 三個定案：金鑰**一組共用**放 btree、「清除所有記錄」**只清 ESP-NOW**、
Slave 清單**用現有的 `PeerRegistry`**（它已經有 `via` / `ifaces` 標來源總線）。

### 34.1 新增

| 位置 | 東西 |
|---|---|
| `NowBus.set_encrypt(on, pmk, lmk)` | 開關加密；**沒有金鑰就不給開** |
| `NowBus.peer_cap()` | 一般 19（20 − 廣播）／**加密 6** |
| `NowBus.clear_peers()` | 清光射頻 peer，只留廣播 |
| `NowBus.add_peer(mac, encrypt=None)` | 加密路徑 |
| `ConfigManager.now_state/load_now/save_now/clear_now` | `@now.*`（btree＝secrets.db）|

金鑰**不進 config.json**：那個人可讀、會被 commit。放 `@now.pmk` / `@now.lmk`，
和 `@node.*` / `@peer.*` 同一個 secrets.db。

### 34.2 ★ 測試抓到的三個真 bug

1. **`bool(encrypt) and self.encrypt`** —— `encrypt=None`（＝沿用本管設定）會被
   `bool()` 壓成 `False` → **加密永遠不生效**。改成明確的三元式。
2. **★★ 廣播不能加密** —— ESP-NOW 的加密只支援**單點**。讓
   `add_peer(BCAST_MAC)` 帶 `encrypt=True` 會變成「加得進去、但廣播從此送不出去」
   —— 又是靜默失敗。現在廣播**一律不加密**（也因為如此，`peer_cap()` 加密時是 6：
   廣播那格不佔加密額度）。
3. **`apply_peers` 的錨點**（我的 patch 打錯括號）→ 整個 patch 沒寫入。
   教訓：**逐錨點回報命中次數**再套用，不要一個大 patch 失敗了才知道。

### 34.3 ★ 修掉一個 12% 失敗率的 flaky 斷言

`test_node_pairing.py` §4 原本要求「12 個抖動值**全部相異**」—— 那是**生日悖論**：
從 501 個值抽 12 個全不撞的機率只有 `exp(-12*11/(2*501)) ≈ 88%`
→ 大約**每 8 次跑就紅一次**（實測踩到）。

> 一個 12% 失敗率的斷言等於沒有斷言，還會訓練人忽略紅燈。

改成「相異值 >= 9」（低於 9 的機率 < 1e-4），並把生日悖論寫在旁邊，
免得下一個人又把它「收緊」。**連跑 8 次全過。**

### 34.4 狀態

- `test_node_pairing.py` **153 / 153 PASS**（+21：§23 加密／上限／清除／btree 持久化）
- ⚠️ **`_hw_add_peer` 的加密形式（位置參數 vs kwarg）還沒真機驗證** ——
  兩條路都寫了，但那塊板子當時不在 USB 上（見下）。

### 34.5 ⚠️ 板子從 USB 上消失了

做到一半時，`/dev/cu.usbmodem101` 與 `11401` **同時**消失，等 30 秒以上沒回來。
兩個同時不見比較像實體原因（拔線／hub 斷電），但也要記著另一種可能：
**我這輪反覆用 `mpremote exec`（預設結尾是軟重開機）＋ hard reset，
內部 SRAM 可能被 WiFi/ESP-NOW 驅動耗盡 → 開機 hard fault →
ESP32-S3 的 USB-CDC 會整個從匯流排消失**（§12.5 / §32.4 同一個機制）。

→ 這正好是「軟重開機不可靠」這件事在**開發流程**上咬人的第二次。
**斷電重上**就會回來；但根本解是別讓 `mpremote` 用軟重開機收尾
（`--no-soft-reset`），或至少在連續操作之間做 hard reset。

### 34.6 文件

新增 **`todo/07_now_setting_ui.md`** —— 「ESP-NOW 設定 ＋ 遙控器設定」兩頁的計劃書。

拆頁的理由（不只是排版）：**兩個清單的鍵與限制根本不同**

| | 鍵 | 來源 | 上限 |
|---|---|---|---|
| ESP-NOW 已選 | **MAC**（射頻層）| 掃到的 radio peer | **19**／加密 **6** |
| 遙控器已選 | **cid / slave_id**（協議層）| `PeerRegistry`（任何總線）| **無** |

混在一張表裡，「已選 3/19」會被讀成「我最多只能控 3 台」—— 而遙控器明明沒有這個限制。

計劃書裡同時記了**六個踩過的陷阱**（軟重開機洩漏、上傳時機、廣播不能加密、
`recv()` 的 `(mac, None)`、ESP-NOW 重複實例、`0x1016` 掉幀），
以及**板子回來後要先驗的五件事**（其中 `_hw_add_peer` 的加密形式尚未真機驗證）。

---

## 35) 兩頁 UI 落地 ＋ 模式表覆蓋驗證 ＋ 三個靜默失敗（2026-10）

§34 的資料層接上 UI，並且把使用者點名的四件事全部做完／認證完。
這一節記的是**驗證結果**與**驗證過程本身踩到的坑**——後者比前者值錢。

### 35.1 先說結論：使用者要的四件事

| # | 要求 | 狀態 |
|---|---|---|
| 1 | 有 pixel 的裝置要用 `/pixel/modes/*.json` **覆蓋** DB | ✅ 真機驗證（§35.3）|
| 2 | 修 `remote.py` 兩顆開關重疊、移除重複的 Wi-Fi 開關 | ✅ 已修（§35.4）|
| 3 | `c3` 那個裸 `12345ms` 要加標籤 | ✅ 已改（§35.4）|
| 4 | 依 `todo/07` 執行兩頁拆分 | ✅ `now_setting.py` 新頁上板（§35.5）|

### 35.2 ⚠️ 為什麼「pixel 沒覆蓋 DB」看起來像個 bug，其實不是

上一輪的結論是「改動 ① 邏輯對，但真機上驗不出來」。這一輪查到了原因：

```
11401 的 /Core_Manager.py:119
    tm.register_task("pixel", PixelTask, default_affinity=(1, 0), layer=-1)
                                                        ↑ repo 這一行是 layer=2
```

`layer == -1` 在 `task_manager._task_eligible_for_boot()` 裡是**無條件回 False**：

```python
def _task_eligible_for_boot(self, name):
    layer = self.layers.get(name, 0)
    if layer == -1:
        return False          # ← 「這層永遠不開機」
```

**這是 task_manager 內建的「關掉某個 task」開關，不是 bug。**
`layer=-1` 同時被排除在 `_max_layer` 的計算之外（`if layer > _max_layer and layer >= 0`），
所以 boot 推進層數時也不會被它卡住。

→ 使用者早就用這個開關把 11401 的 pixel 關掉了。PixelTask 從不啟動
→ `_init_modes()` 從不執行 → `set_local_modes()` 從不被呼叫
→ DB 裡的舊值當然活得好好的。**行為完全正確，只是我找錯了地方。**

（`Core_Manager.py` 裡「佈署時要拿掉某個功能，直接註解掉對應一行即可」那行註解
已經過時了 —— 現在有 `layer=-1` 這個不用改結構的作法。）

### 35.3 ① 的真機驗證（11401）

把板上的 `layer=-1` 暫時改成 `layer=2`，種 3 筆假資料，hard reset：

```
【測試前】@mode.source = remote
          ids  = ['0x101', '0x102', '0x200']
          names= ['FAKE_a', 'FAKE_b', 'FAKE_servo']

【開機 log】
          [Config] ✓ modes(local): 2 個        ← set_local_modes() 真的被呼叫
          [Pixel] modes: 2 個

【測試後】@mode.source = local
          ids  = ['0x1', '0x2']
          names= ['demo_eyes', 'motor_sine']   ← 假資料被真的模式檔覆蓋掉了
          @mode.list n = 2
```

**① 成立。** 驗完立刻把 `layer=-1` 還原（並驗證還原後的檔案與測試前備份一致）。

順帶量到 11401 真實的模式表是 `0x0001 demo_eyes` / `0x0002 motor_sine` ——
`0x0200`（`MOVABLE_ID`）**不是**任何模式檔，它是 `pixel_controller.py` 硬寫的
「可動（鐵打模式）」，只在臨時應急用。

### 35.4 排版 bug 與裸數字（`remote.py`）

```
mk_switch() 實寸 44×24（ui_common.py:251）
  _wifi_sw (10, 224)  x∈[10,54]   標籤 "Wi-Fi"    x=34  ← 壓在開關上
  _now_sw  (168,224)  x∈[168,212] 標籤 "ESP-NOW"  x=192 ← 壓在開關上
  y=224 + 24 = 248 > 240（螢幕高）→ 下緣被切掉 8px
```

兩顆開關都移除（Wi-Fi 那份與 `settings.py` 重複；ESP-NOW 搬到新頁），
空出來的 42px 還給內容：左欄清單 `h 168→180`、`c3` `h 42→58`、底列按鈕 `y 198→210`。
`ITEM_SWITCH` 從 import 拿掉（本頁已無開關）。

`c3` 的 `_lb["sel2"]` 原本是 `"{}ms".format(age_ms)` —— **沒有主詞也沒有方向**，
分不出是「多久沒聽到」還是「回得多快」。改成三行都有標籤：

```
sel  : 0x0002  test-peer
sel2 : 最後 1234ms 前  ●在線     ← age_ms 的定義就是「距離最後一次收到它」
sel3 : 總線 NOW-Bus
```

### 35.5 新頁 `now_setting.py`（傳輸層）

`@register(id="now_setting", title="ESP-NOW", icon="sensors", order=2, accent=0x7B1FA2)`

版面：三張卡（啟用／頻道／加密）＋ 兩張表（可選／已選）＋ 五顆按鈕
（掃描／加入／移除／套用／清除）。

實作時對 `todo/07` 做了**兩個判斷**：

1. **不另開 `remote_setting.py`**（§3.5）。`remote.py` 本身就是應用層那一頁，
   計劃書的 §3.1 與 §3.4 描述的是同一頁，再拆一次只會多一個空殼。
   「兩頁」的實際落點 = `now_setting.py`（傳輸層）＋ `remote.py`（應用層）。
2. **「可選」清單不放 FF**。FF 不是掃到的、也不能被「加入」—— 它是「已選」的預設值。
   放在可選裡只會讓人以為可以把它加到已選（它本來就在）。

### 35.6 真機驗證（11401，`build_all: 7 screen(s)`）

| 項目 | 結果 |
|---|---|
| 頁面註冊 ＋ `build()` 跑得起來 | ✅ `[app] build_all: 7 screen(s)`（原 6），無 `import skip` |
| `registry.ordered()` | ✅ `remote 0, control_panel 1, now_setting 2, ...` |
| 掃描（真廣播 `0x100D`） | ✅ 收到對板 `2C:65:B8 → 0x0002 [NOW-Bus]` |
| `apply_peers([mac])` | ✅ `(1,0,0)`，peer 1→2，**廣播格保留** |
| `apply_peers([])` | ✅ `(0,1,0)` → 只剩廣播（「已選＝只有 FF」的預設語意）|
| `clear_peers()` ＋ `clear_now()` | ✅ 移除 1 筆；`@now.selected`→`[]`、`@now.encrypt`→`0` |
| **清除不越界** | ✅ `@node.*` 與 `@peer.*` 都沒動（使用者定案的範圍）|

⚠️ **沒驗到的**：`_do_add` / `_do_remove` / `_do_encrypt` 這三支會經過
`lb.set_text()`，而 REPL 是**另一個執行緒** —— LVGL 不是 thread-safe，
跨執行緒改 widget 有 hard fault 的風險，所以沒有從 REPL 直接呼叫。
它們由「`build()` 沒報錯」＋程式碼審閱涵蓋；要真機確認請**用手轉編碼器**走一遍。

### 35.7 三個靜默失敗（這一節的重點）

三個都是「**看起來成功了，其實沒有**」，而且都不是靠讀程式碼看出來的。

#### (a) MicroPython 沒有 refcount → reset 前寫入消失

要在板上改一行設定，寫了：

```python
open("/Core_Manager.py", "w").write(src.replace("layer=-1", "layer=2"))
machine.reset()
```

印出了「✓ 已改成 layer=2」，但開機後 PixelTask **還是沒啟動**。
原因：MicroPython 沒有引用計數，`open().write()` 之後檔案物件**還開著**，
要等 GC 才 flush；`machine.reset()` 搶在它前面 → **寫入整批消失**。

修法：明確 `f = open(p,"w"); f.write(s); f.close()`，
而且**寫完先讀回來印出那一行**再重置。
（這個坑第一次踩的時候，我把它誤判成「layer=-1 不是開關」，
差點去改 task_manager。）

#### (b) 板上的檔案比 repo 舊，而 `try/except` 把它變成靜默的錯值

新頁的 `_cap()` 長這樣：

```python
def _cap():
    now = _now()
    try:
        return int(now.peer_cap()) if now is not None else 19
    except Exception:
        return 19          # ← 取不到服務時的保守值
```

板上的 `NowBus` **沒有 `peer_cap`**（`now_bus.py` 是舊版，20739 B vs repo 33822 B）
→ `AttributeError` → 落到 `except` → **回 19**。
於是「加密時上限應該是 6」在這一頁上會**安靜地顯示 19**，完全不會報錯。

→ 教訓：**動到既有 API 之後，上板前先問板子有沒有那個方法**。
```python
for n in ("peer_cap", "set_encrypt", "clear_peers"):
    print(n, hasattr(bus.get_service("NowBus"), n))
```
補上 `now_bus.py` 之後，`_cap()` 才真的回 19／6。

#### (c) `peers` 回的是 `bytes`，不是 hex 字串

`now.peers` 是 `@property`（不是方法，加 `()` 會 `TypeError`），
而且內容是 `[b'\xff\xff\xff\xff\xff\xff', b'$\xecJ,e\xb8']` —— **bytes**。
所以 `"FFFFFFFFFFFF" in now.peers` 恆為 False（還會噴
`Warning: Comparison between bytes and str`），會讓人誤判「廣播不見了」。
要比對請走 `mac_bytes()`。

### 35.8 文件與註解

`ConfigManager` 裡有三處**已經與程式碼相反**的說明，一併修正：

- `set_local_modes()` 的 docstring 原本寫「★ 不把清單寫進 btree」「會刪掉舊的
  `@mode.list`」—— 但 §35.3 的改動已經讓它**寫進去且覆蓋**。
  留著會讓下一個人以為 local 不落地。
- 模式表那段區塊註解（`# ★ 只有 remote 需要持久化…`）同理。
- `load_modes()` 的「只有 remote 的清單在 btree」。

新的說法把**理由**寫清楚：原本「local 不落地」的前提是
「PixelTask 一定會跑」，而它**是可以被關掉的**（`layer=-1`）。
關掉之後 DB 就是唯一來源，「不寫」等於讓錯的舊值永遠沒人管。

`todo/07` 補上：§2.5／§3.4 完成清單、§3.5 不另開 `remote_setting.py` 的決定、
§5.1 這一輪的驗證表、以及 §6 新增三個陷阱（refcount、舊檔案、bytes vs str）。

### 35.9 測試

```
test_node_pairing.py    153 / 153 PASS
test_master_writer.py    19 /  19 PASS
router_selftest.py       75 /  75 PASS
test_proto_hotpath.py     9 PASS
```

### 35.10 排版的算術（以及一件我**沒能**驗到的事）

`remote.py` 的 bug 修完之後，回頭把新頁的每個座標也算過一遍。
字型是 `ui/lvgl/src/zh_hant_16.bin` → **中文字 16px 寬、數字約 8px**。
算完抓到一個還沒發生的溢出：`啟用` 卡只有 96px 寬，開關（44px）擺在 x=6
佔到 50，右邊只剩 46px —— 而「未授權」是 **3 個中文字 = 48px**，會超出 2px。

→ 三張卡改成**照最長的字串**訂寬，而不是平均分：

```
啟用卡 112 (4..116)    52..112 放得下「未授權」
頻道卡 104 (120..224)  26+6 左鈕 / 標籤 36 / 右鈕 72..98
加密卡  88 (228..316)  52..88 放「19格」
合計 112+4+104+4+88 = 312，左右各留 4
```

垂直方向：卡片標題 y4..20、元件 y20..44（高 46）；清單標題 y72..88、
清單 y88..202；底列按鈕 y208..230。**全部 ≤ 240，且相鄰只是相接、不重疊。**

#### ⚠️ 沒驗到的：螢幕上的實際幾何

本來想量真機的 widget 座標來當證據，失敗了，記下來免得下次重蹈：

1. **直接量會全部得到 0×0** —— LVGL 的幾何在 screen **被 load** 之後才算；
   `app.build_all()` 只預建、不顯示（當下顯示的是 launcher）。
2. **不能從 REPL 呼叫 `lv.screen_load()`** —— LVGL 不是 thread-safe，
   而 REPL 與 LvglTask 是不同執行緒，跨執行緒改 LVGL 狀態可能 hard fault。
3. **`lv.async_call()` 這條正路在這塊固件上沒反應** ——
   `lvgl_init.tick()` 確實有呼叫 `lv.task_handler()`（＝timer handler，
   理論上會處理 async 佇列），也試過把回呼存進模組屬性防 GC，
   排進去的回呼就是沒被執行。**原因未查明。**

所以 §35.4 與這一節的結論是**算出來的**（座標全是明確的 `set_pos` 數值），
不是量出來的。要在真機上確認，請**用手轉編碼器走一遍那兩頁**。

---

## 36) 修好文字殘缺：字型子集少了 203 個字（2026-10）

使用者回報「有部份文字是沒有對應表的」。查下去發現不是「部份」——是**三成**。

### 36.1 症狀與數字

`slave/ui/lvgl/src/zh_hant_16.bin` 是 `lv_font_conv` 產生的**子集**字型。
這顆的 `fallback = None`（實測），所以不在子集裡的字**不會退到別的字型**，
就是畫不出來。

```
UI 用到的非 ASCII 字        640 個
其中字型裡沒有 glyph        203 個   ← 32%
```

連「**遙**」都沒有。也就是說 `remote.py` 的標題「遙控器」一直是「⬜控器」。
其他像 了 人 我 你 但 很 想 真 多 心 短 私 種 端 筆 開 關… 也都不在。

### 36.2 ★ 最值錢的部分：我第一次的稽核給了**完全相反**的答案

先寫了 `font_audit.py`：掃 UI 原始碼的字元 → 上板問每個字有沒有 glyph。
第一次跑出來：

```
FONT struct lv_font_t
CHECKED 640
MISSING 0
### ✅ 沒有缺字
```

看起來是好消息。**實際上 203 個字一個都沒量到。** 兩個錯誤疊在一起：

1. **oracle 壞了**：`font.get_glyph_width(ch)` 在這塊固件上對**每一個**字元
   都丟 `TypeError`（它不是 `f.get_glyph_width(ch)` 這種綁定形式）。
   正確形式是 **unbound 風格、要自己把 font 傳進去**：
   ```python
   d = lv.font_glyph_dsc_t()
   ok = f.get_glyph_dsc(f, d, codepoint, 0)    # -> True/False
   ```
2. **結果解析器把證據吃掉了**：查詢失敗的字元被丟進 `bad` 清單，
   程式也印了 `ERRC`，但我的**解析器只認 `MISSING`，沒印 `ERRC`**
   → 畫面上只剩「MISSING 0」。

→ **教訓：新增一個判斷依據時，先拿已知的陽性與陰性對照組驗它。**
修好之後的對照組（`temp/font_oracle_check.py`）：

```
陽性  A / 1 / 空白 / 之        -> True
陰性  U+E000 / U+10FFFD / emoji -> False     ← 陰性若回 True，oracle 就不能用
```

### 36.3 修法

字型要覆蓋的是「UI 真的會畫出來的字」，不是「原始碼裡所有的中文字」。
`temp/gen_font.py`：

1. AST 掃 `slave/**/*.py` 的**字串常量**，排除 docstring 與控制字元、
   排除私用區（那是 `icons_16.bin` 的地盤）。
2. **先讀來源 TTF 的 cmap**（環境沒有 fontTools，自己解格式 4/12），
   只把 TTF 真的有的碼點餵給 `lv_font_conv`
   —— 它只要遇到一個沒有的碼點就**整個中止**，不會跳過。
3. 產生，並跟舊檔比對 `glyf` 前 512 B。

```
舊檔   73716 B
新檔  136616 B  (+62900, +85%)
glyf 前 512B 相同: True  → 來源 TTF 一致，字形不會變
```

★ **`glyf` 前段相同這件事很重要**：它證明來源就是同一支 Arial Unicode，
所以這次只增加覆蓋率、**整個 UI 的字形外觀不變**。重生成字型最怕的就是
「補了字但全部的字都換了長相」，這個檢查把那個風險變成可驗證的。

### 36.4 驗證

```
【部署前】CHECKED 640  MISSING 203  ERRORS 0
【部署後】CHECKED 174  MISSING   0  ERRORS 0     ← 174 = 排除 docstring/PUA 後真正的顯示字集
開機     [font] read 136616 bytes / loaded from buffer OK
         [app] build_all: 7 screen(s) pre-built
```

剩下的 32 個「缺字」全是**假陽性**，已在稽核工具裡過濾掉：
`U+000A`（換行不是 glyph）、`U+26A0 ⚠`（只出現在 docstring/註解）、
以及 36 個 `0xE000–0xF8FF` 私用區碼點（那是 `icons_16.bin` 的 icon 字型，
`mk_icon()` 會明確 `set_style_text_font(icon_font)`）。

### 36.5 沒解决的：動態文字

`@mode.*` 的模式名稱、節點名稱是**執行期才從 JSON 進來**的，靜態掃描看不到。
要支援得收一整段常用字（Big5 一級字 5401 字 ≈ 690 KB）、或規定模式名稱用 ASCII、
或換一顆帶 `fallback` 的字型（`lv_font_conv --lv-fallback`）。
**目前沒做**，所以模式名稱請用英文/數字。

### 36.6 其他副本

`find . -name zh_hant_16.bin` 另外找到三份，**都是未追蹤的本地副本**（不在版控內），
仍是舊的 73716 B：`temp/rc/`、`ports/P4/ESP32-P4-ETH/temp/`、`ports/P4/ESP32-P4-ETH_mp3/ui/`。
最後那個是 P4 port 真正在用的 UI，要一起換的話把新檔複製過去即可
（新檔是舊檔的**超集**，P4 的頁面是 S3 的子集，格式相同）。

### 36.7 文件

`doc/02_guides/06_lvgl_ui.md` §6 整節改寫。舊版有三個問題：
`-o ui/lvgl/src/...` **路徑漏了 `slave/`**、「多收無害（註釋字也收）」**會多胖 85%**、
而且完全沒有「怎麼驗」。新版補上 §6.3 的四個坑與 §6.5 的動態文字限制。

---

## 37) LVGL 軟重開機後重新初始化：元兇是 `deinit()`（2026-10）

### 37.1 症狀

```
[ERROR] ❌ [Core 0] Failed to start lvgl:
        memory allocation failed, allocating 3254793227 bytes
```

數字每次都不一樣（實測 3254793227 / 1009759640 / 1634034300）——
**那個數字不是尺寸，是指標**。只有**軟重開機**會觸發，hard reset 完全正常。

### 37.2 根因（逐步量出來的）

軟重開機後打斷進 REPL，把 `LvglDisp.__init__` 的每一道手續分開跑：

```
lv.is_initialized()          -> True
lv.display_get_default()     -> 有物件
  └ resolution               -> 320 x 240    ← ★ 好的！display 還能用
lv.deinit()                  -> 回 OK，但 is_initialized() 仍 True
  └ resolution 變成 (1009033516, 8029)       ← ★ 壞了（0x3C238B2C 是 PSRAM 指標）
lv.display_create(320,240)   -> MemoryError（垃圾尺寸）
```

舊版 `lvgl_init.py` 寫的是「有殘留就 `deinit()`，然後 `lv.init()` +
`display_create()`」。**三步都錯**：

1. `deinit()` 沒把狀態清乾淨，反而把一個**還堪用**的 display 弄成半死。
2. 就算 `deinit()` 有效，`display_create()` 也會在舊的還在時再建一個。
3. 不能改成「先 `delete()` 舊的再建」—— 軟重開機把 MicroPython 的 heap 整個重來，
   LVGL 記的指標已經不屬於它了。實測 `disp.delete()` → **直接 hard fault**（USB 消失）。

### 37.3 修法：沿用同一個 display

C 層還在就把 display 拿回來用，只重裝 buffers / flush_cb：

```
resolution = 320 x 240
OK set_color_format / set_buffers / set_flush_cb
OK lv.obj() / label / screen_load / task_handler x20
flush_cb 被呼叫次數 = 6        ← 真的畫出來了
```

`_drop_stale()` 只做 `lv.screen_load(lv.obj())` 一件事，**刻意不做**：

- `lv.anim_delete()` —— 會走訪殘留的動畫鏈，那些節點同樣是死指標。
  沒有真機證據就不放（放過一次，疑似造成 USB 掉線，已移除）。
- 任何 free/delete（見 37.2.3）。

### 37.4 已驗證 / 未驗證

| | |
|---|---|
| ✅ hard reset 走全新 init | `[lvgl_init] ... (全新 init)`、`build_all: 7 screen(s)`、`_ui_active=True`、`app.cur='launcher'`、active screen 14 個 widget、`mem_free=7.24MB` |
| ✅ 沿用路徑本身 | 手動實測全通（37.3），flush_cb 真的被呼叫 |
| ❌ 軟重開機的端到端 | **沒驗成** —— 見 37.5 |

### 37.5 ⚠️ 一個**沒有排除**的相關性

**軟重開機會讓 USB-CDC 重新列舉**，序列埠控制代碼中途失效 → log 只抓到一半
（停在 `Task Runner Started`）、重開後也查不到狀態（`Device not configured`）。

要留意的是：**改動前**的軟重開機能抓完整 log（一路到 `schedule`），
**改動後**兩次都在中途斷線。這條相關性還沒排除 ——
可能是當時版本裡的 `lv.anim_delete()`（已移除），也可能只是時序競爭。

→ 所以**這個修法目前只能說是「根因正確、方向正確、尚未端到端驗證」**，
詳細的下一步與備案寫在 `todo/08_lvgl_reinit.md`。

### 37.6 備案

若「沿用」仍有不安全的情況，有一條保證可靠的路：**偵測到剛軟重開機就
`machine.reset()` 自我修復**（hard reset 會把 C 層一起清掉；而且不會無窮迴圈，
因為 hard reset 後 `is_initialized()` 是 False）。代價是約 7 秒 + USB 重新列舉。

### 37.7 工具坑（這一輪花最多時間的地方）

1. **`\x04` 的意義看你在哪個 REPL**：friendly = 軟重開機；raw = 「執行我送的程式」。
2. **`machine.soft_reset()` 從 raw REPL 呼叫，回來還是 raw REPL** → `main.py`
   根本不跑，會讓人以為「軟重開機沒事」。要 Ctrl-C → `\x02` →**等 `>>>`**
   →才送 `\x04`。
3. **`sys.stdout.flush()` 在 MicroPython 不存在**。
4. 軟重開機後必須 `reopen` 序列埠，但 reopen 成功不等於連線穩定。

### 37.8 文件

新增 **`todo/08_lvgl_reinit.md`**（症狀、根因、修法、驗證狀態、下一步、備案、
七個工具坑、五支診斷工具）。`doc/02_guides/06_lvgl_ui.md` §8 的
「LVGL 只能初始化一次」那條也一併更新 —— 它原本寫的解法
（`get_platform()` 一次初始化 + bus reuse）**在軟重開機後是無效的**，
因為 `bus` 本身就是被重開機清掉的 Python 物件。

---

## 38) §37 結案：真根因是 C 層 root pointer 跨 soft reboot 存活（2026-10）

> §37 的方向（元兇是 `deinit()`、修法是「沿用同一個 display」）**已被證偽**。
> 這一節是查到底之後的結論，並且**已修好、已真機驗收**。

### 38.1 真根因：三行程式碼

| 位置 | 內容 | 軟重開機後 |
|---|---|---|
| `ext_mod/lvgl/mem_core.c` | `lv_malloc_core()` → `gc_alloc()` | **GC heap 被 `mp_init()` 清空** |
| `gen/lvgl_api_gen_mpy.py` | `void *mp_lv_roots;`（普通 C 全域） | **指到的 `lv_global_t` 在舊 heap → 死指標** |
| 同上 | `static bool mp_lv_roots_initialized = false;`（function-local static） | **仍是 `true` → 永遠不會重建 `lv_global`** |

ELF 符號（`build-ESP32_GENERIC_S3-SPIRAM_OCT/micropython.elf`）證實兩者都在 `.bss`：

```
3fcaceec B mp_lv_roots
3fcacef4 b mp_lv_roots_initialized$7
```

⇒ **LVGL 的每一筆配置（display / screen / widget / timer / anim / style）
都在 MicroPython 的 GC heap 上；軟重開機把 heap 整個重來，但 C 層的 root
pointer 與「已初始化」旗標都活著 → LVGL 整棵樹變成死指標。**

### 38.2 為什麼「沿用同一個 Object」不可行（§37.3 已證偽）

軟重開機後攔在 REPL（`main.py` 未執行）實測：

| 動作 | 結果 |
|---|---|
| `display_get_default()` | 有物件（解析度還讀得到 320x240）← 看起來「堪用」 |
| `screen_active().get_child_count()` | **14** ← 上一個 session 的 UI 樹還在 |
| `lv.display_create(320,240)` | ✅ OK |
| **`lv.obj()` / `lv.label()` / `lv.screen_load()` / `lv.deinit()`** | ❌ **直接打死板子**（USB-CDC 消失） |

能沿用的不是「一個堪用的 display」，是**一整棵死指標樹**。
另外試過 4 種純 Python 復原配方（含 `lv.mp_lv_deinit_gc()` +
`lv.mp_lv_init_gc()`，兩者確實存在且能清掉 Python 側狀態）——全部失敗，
因為這個 binding **沒有把真正的 `lv_init()` 匯出到 Python**
（`lv.init` 是 `lv_anim_del_all` 的別名，no-op），沒有辦法治好 C 層。

### 38.3 一個重要的判準陷阱（踩過）

**`lv.is_initialized()` 不能當「有沒有殘留」的判準** ——
這個 binding 在 **import `lvgl` 的當下就會做掉 C 層初始化**，
所以乾淨開機時它也可能是 `True`。第一版守門拿它判斷，
結果把正常開機誤判成殘留、**自己的守門把 UI 擋掉**。

要看的是 **`lv.display_get_default() is not None`**。

而且殘留狀態的組合**不穩定**（實測看過 `True+非None`、`False+非None`、
甚至 `True+None`），所以不要用旗標推論 —— 要嘛探測、要嘛用外部狀態。

### 38.4 修法：LVGL 自己的 soft-reboot 守門（已驗收）

新增 **`slave/ui/lvgl/soft_reboot_guard.py`**，掛在
`lvgl_init.get_platform()`（LVGL 唯一初始化入口）。

```
recover()   ← get_platform() 進來先呼叫
  1) 讀 /lvgl_state：reset=1 → 清標記、回 True（防無窮重置）
     ★ 這步必須在寫標記「之前」，否則會蓋掉自己的訊號（踩過）
  2) 探測 lvgl.display_get_default()
       非 None → 殘留 → 標記改 reset=1 → machine.reset()（不會回來）
       None    → 乾淨 → 回 True
note_owned()    LvglDisp 建起來後寫標記
mark_ready()    board._setup() 跑完 → 清標記
```

**為什麼不放在 `boot.py`**：`boot.py` 是硬體初始化，不該為了 LVGL 弄髒
（第一版就是塞在那裡，已整段移出 —— 現在 `boot.py` 跟原本只差一行註解）；
而且探測要在 LVGL 真正要被建起來的那一刻做，heap 越乾淨越安全。
這也對齊 `mp_lcd_bus/PLAN_lcd_bus_hardening_and_selfowned.md` 的 M2：
「`make_new` 偵測殘留 → `esp_restart()` 保險」——檢查放在物件自己的入口。

**能不能推廣到 i80/rgb/dsi**：可以，模組介面就是照那個形狀留的，但先不做 ——
i80 的殘留是「可 cleanup」的（deinit 外設就好），LVGL 不是（配置在死掉的
heap 上）；而且目前只有一個呼叫者，等第二個出現再抽
`lib/sys/soft_reboot.py`。

改動檔案：`slave/ui/lvgl/soft_reboot_guard.py`（新增）、
`slave/ui/lvgl/lvgl_init.py`、`slave/ui/lvgl/board.py`；
`slave/tasks/lvgl_task.py` 不再嘗試清標記（時機不可控）。

### 38.5 驗收（真機 11401，重構後重跑，全部通過）

| 情境 | 結果 |
|---|---|
| 軟重開機（有殘留） | ✅ `[lvgl_guard] 偵測到 soft-reboot 殘留 → hard reset` → USB 斷線 → `已是重置後的重入，清標記繼續` → `(全新 init)` → `_setup done` → `Boot complete` |
| 軟重開機（乾淨） | ✅ 不重置，直接開機成功 |
| 強制 `reset=1` | ✅ `已是重置後的重入 → 清標記繼續`，不再重置 |
| 開機後畫面 | ✅ 320x240、`app.cur='launcher'`、active screen 14 個 widget、`_ui_active=True` |

> ℹ️ 守門那句 `print` 有時會被 USB-CDC 斷線吞掉（`machine.reset()` 太靠近）；
> 驗收腳本因此把「有 SERIAL BREAK」也算成守門有動作的證據。

### 38.6 根治（待做，需重編韌體）

把 `mp_lv_roots_initialized` 從 function-local static 換成 `MP_STATE_VM`
（soft reset 會清），讓 `mp_lv_init_gc()` 在新 heap 上重建 `lv_global`。
改法與注意事項寫在 `todo/08_lvgl_reinit.md` §4。做完
`soft_reboot_guard.py` 就可以整包刪掉。
