# 訊號 Router（Router 任務）

> **用途**：追蹤「ESP-NOW / 網路 / 實體線互相轉送」功能的落地與驗收。
> **最後更新**：2026-09（P1 落地）
> **相關文件**：[doc/02_guides/16_signal_router.md](../doc/02_guides/16_signal_router.md)（設計與規則唯一真相）

## 已完成（loopback／單元自測，非實測）

- [x] **P1** `slave/lib/sys/signal_router.py` 核心（`load` / `gate` / `_forward` / `status`）
- [x] **P1** `test/protocol/router_selftest.py` — 74 項離線檢查全過
  - [x] `load()` 驗證：`in` 純量、`out` 列表、自我反射拒絕、重複 `in`、空 `out`、殘留欄位提示
  - [x] `gate()` 判定：`V_OK` / `V_EXECUTE` / `V_FORWARD` / `V_BOTH` / `V_DROP`
  - [x] 轉送正確性：**位元組級等價**、一對多不互相汙染、CRC 合法、`payload=None`
  - [x] label ↔ 邏輯名對應（`NOW-Bus`→`now`、`CTRL-WS`→`net`、`UDP-DISCV`→`udp`、`CIRCUIT-UARTn`→`uartn`、`VBUS`→`vbus`）
  - [x] 韌性：介面未註冊、`write` 回 False、`write` 丟例外、一對多首個失敗不影響其餘

## 待跟進（實測）

### P2 — `Proto.pack_into()`
- [ ] 把 `signal_router._build_frame()` 的組幀邏輯搬進 `proto.py`，與 `pack()` 共用內核
- [ ] selftest 增加：`pack_into()` 輸出必須與 `pack()` **位元組完全相同**
- [ ] 上板：轉送鏈路實際吞吐（一對多時是否吃滿 UART）

### P3 — 掛鉤（唯一的「回歸風險」階段）
- [ ] `app.py::handle_stream` 收 `router` / `bus`，依 verdict 決定是否 `disp.dispatch`
- [ ] `bus_decode.py` 建立 router、傳掛鉤、尾端 `housekeep()`
- [ ] **回歸（最重要）**：`Router.enable = 0` 時，現有全部功能行為 **100% 不變**
- [ ] `enable=1` + `out:["self"]` 時，行為等同 `enable=0`（純本地執行）
- [ ] 確認 `ctx["send"]`（回覆）**不經過** Router（tx 方向不被攔）

### P4 — `CircuitDecode` 不動，只補警告（無行為變更）
- [x] 語意確認：`CircuitDecode.list[].GPIO.uart` 的值是 **`UART.list` 的索引（0-based）**，不是 `id`
- [ ] `circuit.py`：宣告的索引對不到任何 UART 時**印警告**（原本完全靜默，看不出設定沒生效）
- [ ] 文件已寫明索引語意（doc §3.2）
- [ ] 11 份 config.json：只加 `Router`（`enable: 0`），**不動 `CircuitDecode`**
- [ ] 上板：`uart:0` 選到第 1 條；`uart:5` 要出現警告

### P5 — 執行期指令 0x16xx
- [ ] `slave/schema/router.json`（`ROUTER_STATUS` / `ROUTE_ADD` / `ROUTE_DEL` / `TABLE_GET` / `SAVE` / `ACK`）
- [ ] `slave/action/router_actions.py` + `registry.py`
- [ ] 板上：指令往返、`ROUTER_SAVE` 後重開機設定仍在

### P6 — 端到端實測
- [ ] ESP-NOW → UART 單向（`test/protocol/espnow_send.py` / `espnow_mon.py` 現成工具）
- [ ] UART → ESP-NOW 反向
- [ ] 一對多（三個出口同時）
- [ ] 本地執行 ＋ 轉發（`out: ["self", ...]`）
- [ ] 回程（節點回覆 → 原路回到 Remote）

## 已知問題／待決

- [ ] **跨裝置多跳環無防護**：NC4 header 9 byte 已滿，沒有 TTL 位置（三種放法已評估，見 doc §7.3）
- [ ] **無遠端逃生門**：路由設錯只能實體重刷（使用者已知悉並接受）
- [ ] `out` 含 `udp` 可成環（不防，靠自律；`load()` 會警告）
- [x] ~~`CircuitDecode` 移除~~ → **決定保留**（它是「介面 up/down」，Router 是「routing table」，兩層不重疊）
- [x] ~~`CircuitDecode` id/idx 混用 bug~~ → **不是 bug**（雙方都是索引，語意一致），真正缺口只是「對不到時不出聲」
- [ ] `CircuitDecode` 的 `spi` / `i2c` / `can` key 永遠對不到（`CircuitTask` 只建 UART bus）— 已知缺口
- [ ] **`cID` 未指派時 `bus.cid = 0xFFFF`**：廣播幀會被每一站重複執行 → 多跳轉運前必須先指派 `System.cID`
- [ ] `ctx["send"]`（回覆）不經過 Router：目前視為正確行為，但代表無法對回覆做路由政策
- [ ] P4 port（`ports/P4/ESP32-P4-ETH_mp3/`）是否同步 —— **尚未決定**

## 筆記

**定案的設計原則（討論中被否決的方案一律記在 doc §7.3 / §9.1，勿重提）：**

1. Router 是**解碼鏈上的關卡**，不是另一個讀 `rx_hub` 的消費者（SPSC 會搶幀）。
2. **沒有獨立的 RouterTask** —— 家事掛在 `BusDecodeTask.loop()` 尾端。
   同樣地，**`CircuitDecode` 也不動** —— 它管「哪些線進解碼鏈」（介面 up/down），Router 管「進來了做什麼」（routing table），兩層不重疊。
3. 設定**只有兩個欄位**：`enable` + `routes`。
4. 一個來源 = 一條 route（`in` 純量）；`out` 一律列表；`self` 是保留字。
5. **沒配對 = 沒路走 = 不執行。**
6. 原本沒有的防禦（dedup / TTL / 速率限制）**一律不加** —— 升級不偷偷改變行為。
7. 不確定就出聲，不猜（`in` 寫成列表 → 明確報錯並跳過，不自動解讀）。
