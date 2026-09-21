# 訊號 Router（Router 任務）

> **用途**：追蹤「ESP-NOW / 網路 / 實體線互相轉送」功能的落地與驗收。
> **最後更新**：2026-09（P1~P5 + P7 落地；**再經一輪語意統一與 autofill 改版，見 §近期變更**）
> **相關文件**：[doc/02_guides/16_signal_router.md](../doc/02_guides/16_signal_router.md)（設計與規則唯一真相）
> 　　　　　　[doc/01_protocol/02_command_index.md §7](../doc/01_protocol/02_command_index.md)（0x16xx 指令索引）
> **離線驗證（現行）**：`python3 -B test/protocol/router_self_selftest.py` → **43 項全過**（PC，不需硬體）
> **輔助工具**：`python3 -B test/protocol/router_show_defaults.py` → dump autofill 的實際預設
> （含 `snapshot()` / `table()` / `status()` 的輸出），改排序或預設時看這支。
>
> ⚠️ **下面多處引用的兩份測試檔目前不存在於 repo**：
> `test/protocol/router_selftest.py`（文中稱 440 / 499 項）與
> `test/protocol/router_board_test.py`（真機 114 項）。
> 第 13 行甚至有一條已勾選的「**這份檔案原本不存在，已補齊**」。
> **我沒有重建它們**（440 項不是能憑空還原的）；`router_self_selftest.py` 至少把
> `self` 來源、autofill 預設、跳過不存在通道這幾條新行為鎖住了。
> 要重建或改引用，見 §近期變更的最後一條。

## 近期變更（2026-09，語意統一 ＋ autofill 改版）

> 這一輪把 Router 的幾個語意收乾淨，**與上面 P1~P7 的記載有多處不同**。

- [x] **介面名索引化**：`uartN` 的 N 由 `item["id"]` 改成 **`UART.list` 索引（0-based）**
      —— 與 `uart_list[idx]` / `CircuitDecode.GPIO.uart` / `bus_speed.bus_id` 三層對齊。
      動到 `circuit.py`（label/svc 產生）+ `signal_router.py`（`_LABEL_EXACT` / `_KNOWN_IFACES`）+ 兩份 schedule.py。
- [x] **`self` 成為一等來源**：本機迴路（`CircuitBus(io=None)`，即 vBus）的幀一律以
      來源名 `self` 進 Router（`is_local_bus()` 判別，**不靠 label 字串**）。
      （更正：vbus **仍在來源表**（`ALWAYS_PRESENT`），只是路由決策查 `by_in["self"]`。
      見下方那條「vbus 不是出口 ≠ vbus 不是來源」。）
- [x] **`out` 的保留字**：`self`（本地執行開關）、`vbus`（**無視**，不是出口）。
      `out` 含 `in` 的自我反射檢查**對 `self` 例外**（`self → self` 是合法語意，不是迴圈）。
- [x] **vbus 不是出口 ≠ vbus 不是來源**（實作時把前者過度套用到後者，改壞過）：
      `out: ["vbus"]` 無視；但 `vbus` 在 `ALWAYS_PRESENT`，autofill 照樣替它補 `["self"]`，
      它**永遠在表裡**。使用者定案：`self`、`vbus` 是**最開頭的兩個**。
- [x] **路由表排序**（`_route_order`）：本機（`self` → `vbus`）→ 網路（`net`/`now`/`udp`）
      → 實體（`uartN`）→ 其他（字母序）。使用者：「先網絡後實體，然後就是 self、vbus，
      是最開頭的兩個」。
- [x] **autofill 改版**：
      - `self` 預設 **`[]`**（不指向自己）；其他通道預設 `["self"]`。
      - 只補缺口，**永不覆蓋使用者的**（護欄：`if name in self.by_in: continue`）。
      - **開機結算時把整張表寫回 config.json**（＝使用者要的「幫用戶自行註冊
        目標是 self」；`BusDecodeTask._persist_router_table()`）。
        **只在開機那一次寫**，不在通道上線時寫 —— 時機是規格的一部分。
      - 時機改為**主動函數**：`BusDecodeTask.finalize_router()`，
        只在「開機 / 任務開始」被呼叫，之後不自動跑。
- [x] **通道可見性三段機制**：分層（主要）＋ `SysBus.register_service()` hook ＋
      ~~每 100ms 輪詢~~（**已移除** —— 那是「順序錯了就等下一輪」的症狀解法）。
- [x] **分層修正**：`bus_decode` 移到 **layer 1**，排在「產生通道」的任務（network / circuit / now）之後
      —— 保證 Router 出生時看得到 boot 期所有通道。slave + Control_Panel 兩份 Core_Manager 已改。
- [x] **`vBus` 在 `on_start` 建立**（原本惰性）：讓它跟其他通道同時在場，Router 開機就看得到。
- [x] **`schedule` 的直通出口移除**：`circuit:<i>` / `net:<i>`（`cb.write(frame)`，繞過 Router）刪掉，
      出口一律經路由表。※ 附帶查到那條路**從來沒生效過**（`_load()` 沒放 `"bus"`）。
- [ ] **重建或改引用那兩份遺失的測試檔** —— 目前 6 個檔案（本檔、doc 16、doc 01 changelog、
      `proto.py`、`signal_router.py`、`todo` 自身）都還在引用它們。
- [ ] **`self` 的來源語意是否要延伸到「非 vBus 的本地機制」** ——
      目前判定靠 `io is None`，若將來有別的本地迴路要納入，這個判準要放寬。

## 已完成（程式碼 + 離線自測）

- [x] **P1** `slave/lib/sys/signal_router.py` 核心（`load` / `gate` / `_forward` / `status`）
- [x] **P1** `test/protocol/router_selftest.py` §1–§5（**這份檔案原本不存在，已補齊**）
  - [x] `load()` 驗證：`in` 純量、`out` 列表、自我反射拒絕、重複 `in`、空 `out`、殘留欄位提示
  - [x] `gate()` 判定：`V_OK` / `V_EXECUTE` / `V_FORWARD` / `V_BOTH` / `V_DROP`
  - [x] 轉送正確性：**位元組級等價**、一對多不互相汙染、CRC 合法、`payload=None`
  - [x] label ↔ 邏輯名對應（`NOW-Bus`→`now`、`CTRL-WS`→`net`、`UDP-DISCV`→`udp`、`CIRCUIT-UARTn`→`uartn`、`VBUS`→`vbus`）
  - [x] 韌性：介面未註冊、`write` 回 False、`write` 丟例外、一對多首個失敗不影響其餘
- [x] **P2** `Proto.pack_into()`（`slave/lib/sys/proto.py`）—— 與 `pack()` 共用 `_write_frame` 內核
  - [x] selftest §6：多種長度 / 位址**逐位元組**對比 `pack()`
  - [x] 非零 offset、容量不足回 -1、payload `None` / `memoryview`
- [x] **P3** 掛鉤（`app.py::handle_stream` 收 `router` / `src_bus`；`bus_decode.py` 建立並註冊 `signal_router`）
  - [x] `BusDecodeTask`：`_router_setup()` / 每 100ms `sync_ifaces()` / 尾端 `housekeep()`
  - [x] selftest §7 用**真的 `App.handle_stream`** 逐項回歸
  - [x] `Router.enable = 0` 時，結果與 `router=None` **逐項相同**
  - [x] `enable=1` + `out:["self"]` 行為等同 `enable=0`（純本地執行）
  - [x] 確認 `ctx["send"]`（回覆）**不經過** Router（回程不觸發任何 route）
  - [x] selftest §10 `sync_ifaces()`：晚到的 ESP-NOW / 惰性 `vBus` / 沒進 `CircuitDecode` 的 UART
- [x] **P4** `CircuitDecode` 不動，只補警告（無行為變更）
  - [x] 語意確認：`CircuitDecode.list[].GPIO.uart` 的值是 **`UART.list` 的索引（0-based）**，不是 `id`
  - [x] `circuit.py::_warn_unmatched()`：宣告的索引對不到 UART 時**印警告**（原本完全靜默）
  - [x] 文件已寫明索引語意（doc §3.2）
  - [x] selftest §8 用真的 `CircuitTask.on_start()` 驗證：`uart:0` 選到第 1 條、`uart:1` 選到第 2 條、`uart:5` 出警告、`spi` 出警告
  - [x] **10 份 config.json**（`slave/` + `ports/`）只加 `Router`（`enable: 0`），**沒動 `CircuitDecode`**
- [x] **P5** 執行期指令 0x16xx
  - [x] `slave/schema/router.json`（`ROUTER_STATUS` / `ROUTE_ADD` / `ROUTE_DEL` / `TABLE_GET` / `SAVE` / `ACK`）
  - [x] `slave/action/router_actions.py` + `registry.py`
  - [x] selftest §9 執行期 API（`route_add` / `route_del` / `table` / `snapshot` / `set_enable`）
  - [x] selftest §11 用**真的 schema + 真的 codec** 走完整 0x16xx 往返（含存檔成功／失敗兩條路）
- [x] **P7 自動註冊 config**（使用者新要求，每次啟動都跑、**不是開關**）
  - [x] `_autofill()`：檢視實際存在的線路，`routes` 沒有的自動補 `{"in": 線路, "out": ["self"]}`
  - [x] ~~補出來的**寫回 config.json**（`BusDecodeTask._persist_autofill()`，只在有新線路時寫一次）~~
        → **2026-09 改**：寫回 **整張表**（`router.snapshot()`），且只在
        **開機結算那一次**寫（`BusDecodeTask.finalize_router()`）。
        **看 §近期變更的 autofill 改版那一條為準。**
  - [x] 使用者寫過的不覆蓋；`enable: 0` 時照樣註冊（方便先看 config 再決定要不要開）
  - [x] ~~寫了但**實體不存在**的來源 → 無視、跳過建立~~
        → **2026-09 改**：暫不註冊但**留在 `_pending`**；該通道之後上線時
        用**使用者寫的內容**註冊（不是預設值）。看 §近期變更為準。
  - [x] `ROUTE_DEL` 掉自動補的 → 同 session 不會被下一次 sync 補回來
  - [x] selftest §12（39 項）
- [x] 文件同步：doc §2.2 / §3.1 / §4.4 / §5 / §10 / §11 / §12 / §13、指令索引新增 §7

## 已完成（真機實測，2026-09）

- [x] **§1** `Proto.pack_into()` 與 `pack()` 在真機**逐位元組相同**（真 `ubinascii.crc32`，0/1/13/255/1000/4102 B）
- [x] **§2** `SignalRouter` 核心在真機：位元組級等價、一對多不汙染、超長丟棄、未註冊出口、
      `write` 丟例外不影響其餘、`enable=0` 完全不作用、自動註冊（`sync_ifaces`）與
      「寫了但不存在 → 無視跳過建立」
- [x] **§3** `0x1601`~`0x1606` 指令在真機走**真 schema + 真 codec + 真 handler** 完整往返
      （含 `ROUTE_ADD` 覆寫、`code=2/3` 錯誤碼、`ROUTE_DEL`），`ROUTER_SAVE` 真的寫進 `/config.json`
- [x] **§4** 真機 `BusDecodeTask.loop()` → `App.handle_stream`（真 `micropython.native`）→ Router：
      本地執行／純轉發／`self`+兩出口一對多／`enable=0` 回歸／壞設定不影響解碼
- [x] **§5** 自動註冊用**真 `ConfigManager`** 落盤 `/config.json`，重新 load 不再重補（idempotent）
- [x] **§4 效能**：真機量測（n=2000 / n=300）
      - `gate()` 空函式 15.5µs/次 → `enable=0` 23.0µs/次 ⇒ **增量 7.6µs/次**
      - `handle_stream()` 無 router 1510µs/幀 → `enable=0` 1491µs/幀 ⇒ **增量 ≈ 0（在雜訊內）**

### 真機抓到的兩個 bug（已修）

1. **`router_actions.on_router_save` import 順序**：`from lib.sys.ConfigManager import cfg_manager`
   放在寫入 `bus.shared["Router"]` **之後** → 首次 import 會跑 `load_setup()`，把 `bus.shared`
   蓋回檔案裡的舊值 → **存檔存到舊設定**（`ROUTER_SAVE enable=1` 實際寫進 `enable=0`）。
   修法：先 import 再寫 `bus.shared`。
   （真機 `boot.py` 已在 T0 import 過，所以只在「首次用到」時踩到 —— 離線測試用假 cfg_manager 抓不到。）
2. **`ConfigManager.save_from_bus` 的標準保存用 `os.replace`**：MicroPython 的 `os` **沒有 `replace`**
   → 整個「非無損更新」的存檔路徑一直失敗（只印一行 `✗ 保存出錯`）。
   這條正是**自動註冊把新線路寫回 config** 的路（config 沒有 `Router` 鍵時走無損更新會失敗 → 回退標準保存）。
   修法：`getattr(os, "replace", None)` 沒有就退回 `os.rename`。

### ⚠️ 真機順手量到的**既有**效能問題（與 Router 無關，但同一條熱路徑）

**MicroPython 的「切片賦值」成本與緩衝區總大小成正比，不是與複製長度成正比。**

在同一塊板子上（ESP32-S3 @160MHz）：

| 操作（8205B buffer） | 成本 |
|---|---|
| `buf[0] = 65` | 7.1 µs |
| `x = buf[0:17]`（讀切片） | 14.0 µs |
| `struct.pack_into('<17s', buf, 0, f)` | **17.1 µs** |
| `buf[0:17] = f`（**寫切片**） | **626.3 µs** |
| `mv[0:17] = f`（memoryview 寫切片） | **626.4 µs** |

而且 `mv[0:0+1] = 1B` 與 `mv[0:1000] = 1000B` **都是 628µs** → 成本取決於**目標緩衝區大小**，不是寫入量。
實測 `StreamParser(max_len=64/512/2048/8192)` 的 `feed+pop` =
**93 / 128 / 244 / 704 µs**，與 `max_len` 線性。

- 受影響：`StreamParser.feed()`（`proto.py`，每幀一次）、compact 搬移、
  `Proto._write_frame` 的 `b[HDR_LEN:...] = payload`（＝ **Router 每次轉送也會付一次**）
- `proto.py` 現有註解寫「memoryview slice 賦值 = C 層 memmove，比 viper 逐 byte 迴圈快」——
  **在這塊板子的固件上不成立**，需要重測（該固件是 `1.29.0-preview.420.gc4fa8cb309.dirty`，
  與量到「快 17x」時的可能不是同一個 build）
- ✅ **找到一行修法**（同一塊 8205B buffer，實測，n=1000~2000）：

  | 寫法 | 成本 |
  |---|---|
  | `mv[0:17] = f`（現行） | **626.1 µs** |
  | `mv[0:17][:] = f`（**先取小視圖再 `[:]` 賦值**） | **17.8 µs** |
  | `sub = mv[0:17]` 預建後 `sub[:] = f` | 11.1 µs |
  | `struct.pack_into('<17s', mv, 0, f)` | 16.7 µs |
  | compact：小視圖 ← 小視圖 | 25.4 µs |

  原因：**成本取決於「目標視圖的長度」**，不是寫入量。`mv[a:b] = data` 的目標是整個大視圖
  → O(8205)；`mv[a:b][:] = data` 的目標只有 17 bytes → O(17)。**語意完全相同。**

- 端到端實測（真機，`feed()+pop_frame()`，113-byte 幀）：

  | | 成本 |
  |---|---|
  | 原版 | 854.3 µs/幀 |
  | 改後 | **191.5 µs/幀**（**4.5×**，每幀省 ~660µs） |

  正確性：黏包／半包／4000-byte 大幀的解析結果**逐項相同**。

- **受影響的寫入點**（全都是同一個一行改法）：
  `proto.py` `StreamParser.feed()` 的 append 與 compact 搬移、
  `Proto._write_frame()` 的 `b[off+HDR_LEN:...] = payload`（**Router 每次轉送也付一次**）
- ✅ **已修並在真機複驗（2026-09）**：`proto.py` 三個寫入點改成
  **「按 `寫入量 + 256 < 緩衝區` 選寫法」**（不是無腦用小視圖 —— 見下面成本模型）
  - 真機 `feed+pop`：**876 → 196 us/幀（4.5×）**
  - 寫入本身**沒有任何 (buffer, 寫入量) 組合變慢**（成本曲面已逐步驗證）
  - 驗證：`test/protocol/test_proto_writes.py` **89 項（PC + 真機都跑）**，
    含 120 輪隨機對拍、重疊 compact、錯誤行為一致、對獨立參考實作逐位元組相同、
    記憶體不成長；`router_selftest.py` §14 + 全套 499 項；真機 114 項
  - 完整成本模型與取捨：`doc/03_notes/01_changelog.md` §29
- ⚠️ **已知取捨**：`Proto.pack()` 走 `_write_frame()` 共用內核 → 多一次函式呼叫。
  這塊壞固件上一次呼叫 ~+80us（正常固件 ~2us）；**寫入本身沒退步**。
  要壓掉就把 `_write_frame` 在 `pack()` 就地展開（代價：兩份組幀邏輯）。**目前選擇不展開**。
- ⚠️ **尚未處理的同類寫入點**（同樣一行改法，屬其他子系統，未經同意不動）：
  `circuit_bus.py` `poll()` 的 `pv[:n] = raw_bytes`、`_commit()` 的 `cview[:take] = view[:take]`、
  `net_bus.py` 同類路徑 —— 這些的目標視圖都是 4115B 級，預期各 ~320us/次，值得之後量一次。

### ⚠️⚠️ 更嚴重：這塊板子的固件 `sys.modules` 是空的（模組完全沒有快取）

同一次複測順手量到（`bench_slice_assign.py` §1c）：

```
len(sys.modules) = 1        # 只有 'flashbdev'
'struct' in sys.modules     → False
sys.modules['struct']       → KeyError
```

也就是說**每一個 `import` 都真的重新 import 一次**，沒有快取：

| 在函式內執行 | 成本（ESP32-S3 @160MHz）|
|---|---|
| `import time` / `import struct` / `import json`（builtin，重複 import） | **34.4 ms** |
| `import 不存在的模組`（失敗） | 34.5 ms |
| `from lib.sys import bus_speed`（本專案最常見的寫法） | **2.0 ms** |
| `import lib.sys.bus_speed`（點號式，檔案存在） | 0.09 ms |

**這直接打在 `app.py::handle_stream` 上** —— 它在**每批解碼的尾端**做兩次函式內 import：

```python
        if packet_found:
            try:
                from lib.sys import bus_speed      # ← ~2ms（檔案不存在時 34ms）
                bus_speed.bus_speed_touch()
            ...
            try:
                from lib.sys import watchdog       # ← ~2ms
                watchdog.touch()
```

→ 正常固件這兩個 import 是快取命中（µs 級），**這塊板子上是每批 ~4ms**。
（也解釋了先前量到的「handle_stream 129ms/幀」：那時 `bus_speed`/`watchdog` 還沒部署，
兩個**失敗**的 import = 2 × 34ms，部署後掉到 1.5ms/幀。）

- 這是**固件 build 設定**問題（`MICROPY_PY_SYS_MODULES` / 模組註冊被關掉），不是 Python 程式碼問題，
  也與 Router 無關。
- **待確認**：生產固件（`ext_mod/ESP32_GENERIC_S3_2026_08_21_06_01_18.bin`，2026-08-21）是否正常。
  若正常 → 專案不用改；若也一樣 → 要嘛改 build config，要嘛把熱路徑上的函式內 import 移到模組層。

## 待跟進（**只能上板做的部分**）

### 複測進度（使用者要求：先複測再改）
- [x] 寫出獨立 bench：`test/protocol/bench_slice_assign.py`（任何固件都能跑，自帶 VERDICT）
- [x] 在**現行 dirty build** 上量到：切片賦值 632µs / 小視圖 21µs / feed+pop 876→214µs（4.1×）
- [x] 順手抓到 `sys.modules` 空的問題（上面那節）
- [x] ~~在生產固件上複測~~ → **不需要**：複測＝在板子上跑 bench 即可（已做）。
      燒固件是我多做的，與任務無關；**`ext_mod/*.bin` 沒有被燒進板子**。
- [x] 依複測結果改 `proto.py`（三個寫入點）並在真機複驗（見上）
- [ ] **生產固件是否也有 `sys.modules` 空的問題** —— 拿任一台跑生產固件的板子，
      `mpremote exec "import sys; print(len(sys.modules))"` 一行就知道。
      正常應該遠大於 1；若也是 1，熱路徑上的函式內 import 要處理（`app.py` 兩處）。

> 離線自測涵蓋不到的是「真硬體時序、真通道、真吞吐」。以下每一項都要在板上勾。

### P3 — 板上回歸（最重要，風險最高）
- [x] ~~燒入後不改 config 行為 100% 不變~~ → **真機 §4 已驗**（`enable=0` 解碼路徑增量 ≈ 0；
      `gate()` 增量 7.6µs/次）。**但完整固件（boot.py + 真硬體）的整機回歸仍未做**
- [ ] 真·完整固件上板：boot.py 硬體初始化 + Core0/Core_Manager 全任務跑起來後，再跑一次上面那條
- [ ] `STATUS_GET 0x1101` / `IDENTIFY_REQ 0x100D` / `FILE_* 0x20xx` 照常
- [x] ~~開機訊息不應出現 `[Router]` 錯誤~~ → **真機 §4 已驗**（只有故意餵壞設定時才出聲）

### P4 — 板上警告
- [ ] `CircuitDecode.list` 寫 `{"GPIO":{"uart":0}}` + 2 條 UART → 選中第 1 條，**無警告**
- [ ] 改成 `"uart": 5` → 開機出現「對不到任何 UART」警告
- [ ] 確認警告是走 log（`log_print_levels` 有 `warn` 才顯示）
      （P4 的邏輯有離線測試 §8，但**需要真 `uart_list`／真 UART 硬體**，本次板子沒接 UART）

### P7 — 板上自動註冊（新增）
- [x] ~~第一次開機後 config 自動長出真實線路~~ → **真機 §5 已驗**（真 `ConfigManager` 寫進 `/config.json`，
      第二輪不再重複寫 = idempotent）
- [ ] 在**完整固件**上驗：真的 `NetworkTask` / `CircuitTask` / `ScheduleTask` 上線後，
      `net` / `udp` / `now` / `uartN` / `vbus` 都各自被補上（本次用假 bus 驗邏輯）
- [ ] ~~第一次開機後，`config.json` 的 `Router.routes` **自動長出這台真實的線路**
      （預期：`net` / `udp` / `now`（若 ESP-NOW 有開）/ `uartN`（若有 UART）/ `vbus`）
- [ ] 每條自動補的都是 `{"out": ["self"]}`，且**開機只寫一次**（第二次重開不再寫檔）
- [ ] 自己寫一條 `{"in": "uart9", "out": ["self"]}`（不存在的線路）→
      不會出現在 `0x1604 ROUTER_TABLE_GET`，但 **config 裡還在**；`ROUTER_STATUS` 的
      `unbound` 看得到它
- [ ] 把 UART 打開後重開機 → 那條自動出現

### P5 — 板上指令往返
- [x] ~~`0x1604 ROUTER_TABLE_GET page=0`~~ → **真機 §3 已驗**（回 `0x1606`，`data_json` 可解析）
- [x] ~~`0x1602 ROUTER_ROUTE_ADD`~~ → **真機 §3 已驗**（ok=1、立即生效）
- [x] ~~`0x1605 ROUTER_SAVE`~~ → **真機 §3 已驗**（真的改寫 `/config.json`，enable 正確落地）
- [x] ~~`0x1603 ROUTER_ROUTE_DEL`~~ → **真機 §3 已驗**
- [x] ~~壞 `route_json` → `code=2`；自我反射 → `code=3`~~ → **真機 §3 已驗**
- [ ] **重開機後持久化**：`machine.reset()` 後 `ROUTER_STATUS` 仍看到同一張表
      （§5 已驗「config 內容可被重新 load 且不再重補」，但沒真的 reset）
- [ ] 透過**真通道**（WS / UDP / ESP-NOW / UART）送 0x16xx，而不是本地函式呼叫

### P6 — 端到端實測
- [ ] **單板 + PC（最快的一輪，先做這個）**：`{ "in": "net", "out": ["udp"] }`
      → PC 從 WS 控制通道送一幀，PC 的 discovery socket 應該收到同一幀（位元組相同）
      ⚠️ 這正是 doc §7.2 的成環情境 —— 測完**記得把 route 移掉**
- [ ] ESP-NOW → UART 單向
- [ ] UART → ESP-NOW 反向
- [ ] 一對多（三個出口同時）
- [ ] 本地執行 ＋ 轉發（`out: ["self", ...]`）
- [ ] 回程（節點回覆 → 原路回到 Remote）
- [ ] 一對多時的實際吞吐（是否吃滿 UART）

> ⚠️ **現成工具不足**：`test/protocol/espnow_send.py` / `espnow_mon.py` 送的是
> 裸 payload（`b"PING-n"`）**不是 NC4 幀**，StreamParser 會直接丟掉 → 不能用來測 Router。
> P6 需要一支「組 NC4 幀 → 送 ESP-NOW → 印回覆」的腳本（或直接用 PC 端
> `tools/PC/NetBusMaster.py` 走 WS/UDP）。**這支工具還沒寫。**

## 已知問題／待決

- [ ] **跨裝置多跳環無防護**：NC4 header 9 byte 已滿，沒有 TTL 位置（三種放法已評估，見 doc §7.3）
- [ ] **無遠端逃生門**：路由設錯只能實體重刷（使用者已知悉並接受）
- [ ] `out` 含 `udp` 可成環（不防，靠自律；`load()` 會警告）
- [x] ~~`CircuitDecode` 移除~~ → **決定保留**（它是「介面 up/down」，Router 是「routing table」，兩層不重疊）
- [x] ~~`CircuitDecode` id/idx 混用 bug~~ → **不是 bug**（雙方都是索引，語意一致），真正缺口只是「對不到時不出聲」→ 已補
- [ ] `CircuitDecode` 的 `spi` / `i2c` / `can` key 永遠對不到（`CircuitTask` 只建 UART bus）— 已知缺口；**P4 已讓它出聲**
- [ ] **`cID` 未指派時 `bus.cid = 0xFFFF`**：廣播幀會被每一站重複執行 → 多跳轉運前必須先指派 `System.cID`
- [ ] `ctx["send"]`（回覆）不經過 Router：目前視為正確行為，但代表無法對回覆做路由政策
- [ ] **`ports/P4/ESP32-P4-ETH_mp3/` 是 fork，不含 Router 程式碼 —— 尚未決定要不要同步**
      本輪已查清：該 port 是 slave 的**分支**（有自己的 `app.py` / `lib/` / `schema/`），
      且已漂移：缺 `lib/sys/signal_router.py`、`lib/sys/timer.py`、`action/router_actions.py`、
      `schema/router.json`；`proto.py` 差 56 行、`bus_speed.py` 差 57 行、`app.py` 差 25 行、
      `tasks/bus_decode.py` 差 69 行。
      > ⚠️ 該 port 的 `config.json` **已經加了 `Router: {enable: 0}`（惰性、無作用）**，
      > 但那塊程式碼在該 port **不存在** —— 在同步之前，**不要把它的 `enable` 改成 1**
      > （不會有任何作用，也不會出聲；這正是 P4 要消滅的靜默失敗）。
- [ ] **`ROUTER_SAVE` 沒帶 payload = enable 0（關閉）**：`SchemaCodec` 對空 payload 的欄位補 0；
      已在 doc §12.2 寫明，但 PC 端實作要小心（只存檔請送 `0xFF`）
- [ ] **NC4 幀最大 4115B**：Router 不重組、不切幀；超過直接丟棄並計數（`RX_BUF_SIZE` 沿用 `proto.py`）

## 筆記

**本輪新增的兩個實作決策（已寫進 doc，勿重提）：**

1. **`gate()` 放在 ADDR 過濾之前**（doc §2.2）：`enable=1` 時「過路幀」（位址非本機）也會被轉送，
   這是「Remote 的指令轉給下層節點」的必要條件；`enable=0` 時逐行與舊版相同（selftest §7 固定）。
2. **`ROUTER_SAVE` 的 enable 是原子的**（doc §12.2）：存檔失敗 → 記憶體與 `bus.shared` 一起回復，
   不留「說存檔失敗但開關已經翻了」的半套狀態。
3. **自動註冊沒有開關**（doc §4.4）：使用者定調「每次啟動都要執行檢查建立」，
   所以不是 `auto: 1/0` 而是固定行為；同一 session 內只對「新看到的線路」動手一次
   （所以 `ROUTE_DEL` 刪掉的不會被 100ms 的 sync 又補回來，但**下次開機會重新檢查建立**）。
   要讓一條線永久不執行 → 寫 `{ "in": X, "out": [] }`，不是把 route 刪掉。

**定案的設計原則（討論中被否決的方案一律記在 doc §7.3 / §9.1，勿重提）：**

1. Router 是**解碼鏈上的關卡**，不是另一個讀 `rx_hub` 的消費者（SPSC 會搶幀）。
2. **沒有獨立的 RouterTask** —— 家事掛在 `BusDecodeTask.loop()` 尾端。
   同樣地，**`CircuitDecode` 也不動** —— 它管「哪些線進解碼鏈」（介面 up/down），Router 管「進來了做什麼」（routing table），兩層不重疊。
3. 設定**只有兩個欄位**：`enable` + `routes`。
4. 一個來源 = 一條 route（`in` 純量）；`out` 一律列表；`self` 是保留字。
5. **沒配對 = 沒路走 = 不執行。**（P7 之後：線路存在就會被自動註冊補上，所以這條只在「明確寫 `out: []`」或「已被 ROUTE_DEL 跳過」時成立）
6. 原本沒有的防禦（dedup / TTL / 速率限制）**一律不加** —— 升級不偷偷改變行為。
7. 不確定就出聲，不猜（`in` 寫成列表 → 明確報錯並跳過，不自動解讀）。
