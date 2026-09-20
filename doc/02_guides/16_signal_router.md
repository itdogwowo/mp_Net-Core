# 訊號 Router（Router 任務）

> **用途**：讓 ESP-NOW / 網路 / 實體線之間可以**互相轉送訊號**，並決定每一條線收進來的幀
> 要「本地執行」還是「轉送出去」還是「兩者都做」。路由由 `config.json` 宣告，不寫死。
> **位置**：`slave/lib/sys/signal_router.py`（核心）＋ `slave/tasks/bus_decode.py`（掛鉤）
> **狀態**：設計定案，P1 初版已落地（見 §11 分期）
> **最後更新**：2026-09（初版）

---

## 1. 為什麼要做這個

`todo/README.md` 的待跟進清單原本就記著：

> **MCU ↔ MCU 對等傳輸**（需先補「來源位址 + 回給來源」的定址機制）

現在的架構裡，**每個通道只能「自己收、自己回」**：

```
Remote ──ESP-NOW──> [NowBus] ──> 解碼器 ──> 執行 ──> 回 Remote
下層節點 ──UART───> [CircuitBus] ──> 解碼器 ──> 執行 ──> 回節點
                     ↑ 兩條線互不相通
```

要做「Remote 的指令轉給下層節點」或「PC 透過 LAN 控制 ESP-NOW 節點」，就只能改程式。

**Router 就是把這件事變成設定。**

---

## 2. 架構：Router 是解碼鏈上的一道關卡，不是另一個消費者

### 2.1 為什麼不能「另開一個任務去讀 rx_hub」

這是最容易走錯的一步。`AtomicStreamHub` 是 **SPSC（單一生產者／單一消費者）**：

| 問題 | 位置 | 後果 |
|---|---|---|
| **搶幀** | `hub.get_read_view()` | Router 讀走 = 解碼器永遠看不到，反之亦然 |
| **配額飢餓** | `bus_decode.py` 的 `decode_budget_slots`（預設 32） | `used >= max_slots` 會 **return**，兩個消費者會互相餓死 |
| **自噬競態** | 讀走後回注 = 自己再讀到自己 | 需要額外去重，還是有 race |

**結論：解碼鏈只有一條，Router 站在鏈上，不另開出口。**

### 2.2 實際形態

```
BusDecodeTask.loop()                    ← 唯一的讀取者
  ├─ poll 各 bus → rx_hub → StreamParser 解幀
  ├─ ★ router.gate(bus, addr, cmd)      ← 掛鉤（app.py::handle_stream 內）
  │     ├─ 查路由表 → 轉送（write 到目的通道）
  │     └─ 回傳 verdict
  └─ Dispatcher.dispatch()              ← 只有 verdict 允許時才執行
```

`Router.enable = 0` 時 `gate()` 直接回 `OK`，**解碼路徑一行都不進** —— 這是「升級不破壞」的保證。

### 2.3 檔案與責任

| 檔案 | 責任 |
|---|---|
| `lib/sys/signal_router.py` | 路由表 + 匹配 + 轉送 + 統計（純邏輯，**CPython 可離線測試**）|
| `tasks/bus_decode.py` | 建立 router、每幀呼叫 `gate`、`housekeep()` |
| `app.py::handle_stream` | 依 verdict 決定要不要 `disp.dispatch()` |
| `action/router_actions.py` + `schema/router.json` | 0x16xx 執行期指令（P5）|

**沒有新的 Task 類別**（`Core_Manager.py` / `Core0.py` 不需修改）—— 見 §8。

---

## 3. 通道模型

### 3.1 註冊者

| 介面名 | 實際 bus 物件 | 誰註冊 |
|---|---|---|
| `uart1` / `uart2` … | `circuit_bus_uartN` | `CircuitTask` |
| `now` | `NowBus` | `NetworkTask` / `NowTask` |
| `net` | `net_bus_ctrl`（WS）| `NetworkTask` |
| `udp` | `net_bus_discovery` | `NetworkTask` |
| `vbus` | `CircuitBus(io=None)` | `ScheduleTask`（惰性）|

`bus_sources.add()` 本身以 `id()` 去重，**多個任務重複註冊同一條線是安全的**。

### 3.2 `CircuitDecode` 保留 —— 它是「介面 up/down」，不是路由

**結論：`CircuitDecode` 不移除。** 它跟 Router 不是重疊，是**兩層**：

| 層 | 誰管 | 機制 | 對應 IP router |
|---|---|---|---|
| **進不進解碼鏈** | `CircuitDecode` | `bus_sources.add()` | **介面 up/down**（`ip link`）|
| **進來了做什麼** | `Router.routes` | `gate()` | **routing table**（`ip route`）|

它同時定義了「**這個裝置要聽哪幾條線**」—— 這在有 2 條 UART、卻只用 1 條的板子上是有意義的
（省掉另一條的輪詢與解析），而且它**本來就是為了相容不同設備才加的**。

#### 為什麼不用移除

**移除它沒有任何好處，因為出口這一側本來就已經全開**：

```python
# circuit.py:70-82 —— 現況（不用改）
bus.register_service("circuit_bus_uart{}".format(uid), cb)   # 單條：全部
bus.register_service("circuit_bus_all_list", all_buses)      # 全部
bus.register_service("circuit_bus_all_by_id", all_by_id)     # 全部
bus.register_service("circuit_bus_list", buses)              # 只有選中的
for cb in buses:
    sources.add(cb)                                          # 只有選中的進解碼
```

所以 **Router 要寫一條「沒進解碼」的 UART，現在就做得到** —— 直接取
`circuit_bus_uartN` 服務即可：

```python
# Router 的出介面解析（不碰 CircuitTask 任何一行）
def _wire(self, name):
    if name.startswith("uart"):
        return bus.get_service("circuit_bus_uart" + name[4:])
```

| | 保留 `CircuitDecode` | 移除（原本的提案）|
|---|---|---|
| 沒被選中的 UART 能不能當出口 | ✅ 可以（`circuit_bus_uartN`）| ✅ 可以 |
| 沒被選中的 UART 會不會被輪詢/解析 | ❌ 不會（省成本）| ⚠️ 會（白做工）|
| 行為變更 | **無** | **有**（需上板驗證）|
| 設定重複 | 無（各管一層）| 無 |

**結論：保留。** 它沒有壞處，移除卻要背一次回歸風險。

#### ⚠️ 語意要寫明：`uart` 的值是**索引**（0-based）

```json
"CircuitDecode": { "enable": 1, "list": [ {"GPIO": {"uart": 0}} ] },
"UART":          { "list": [ {"id": 1, ...}, {"id": 2, ...} ] }
                         ↑ 索引 0             ↑ id=1  →  uart:0 選中這條
```

`uart` 的值是 **`UART.list` 的索引**，**不是** `id`。
（現在 `circuit.py` 的實作本來就是索引 —— 但沒有任何地方寫明，所以看起來像 bug。）

| 寫法 | 選到 |
|---|---|
| `"uart": 0` | `UART.list[0]`（`id=1` 那條）|
| `"uart": 1` | `UART.list[1]`（`id=2` 那條）|
| `"uart": 5`（超出範圍）| **什麼都選不到** → 必須警告（見 §11.1）|

#### 其他 key 的現況（已知缺口，本次不處理）

`_get_selected_sources()` 也接受 `spi` / `i2c` / `i2c_target` / `can` / `service`，
但 `CircuitTask` 只建 UART bus —— **那些 key 永遠對不到任何東西**。列為已知缺口，不在本次範圍。

### 3.3 邏輯名 ↔ bus label 對應（唯一真相）

路由表用的是**邏輯名**，但 `BusDecodeTask` 傳進來的 transport label 是各 bus 自帶的**實體名**。
兩者本來就不同調，所以集中在 `signal_router.py` 的 `iface_name_from_label()` 翻譯一次 ——
**各 Task 不需要重複註冊別名**。

| 邏輯名 | bus 物件 | bus label（實體名）|
|---|---|---|
| `now` | `NowBus` | `NOW-Bus` |
| `net` | `net_bus_ctrl`（WS 控制通道）| `CTRL-WS` |
| `udp` | `net_bus_discovery` | `UDP-DISCV` |
| `uartN` | `circuit_bus_uartN` | `CIRCUIT-UARTN` |
| `vbus` | `CircuitBus(io=None)` | `VBUS` |
| `self` | —（保留字，非實體）| — |

> **為什麼叫 `net` 而不是 `lan`**：這條線是 WS 控制通道，**它可能跑在 LAN 也可能跑在 WiFi 上**。
> 叫 `lan` 會在 WiFi 部署時說謊。`net` 描述的是「網路控制通道」這個角色，與底層媒體無關。
> 同理 `udp` 描述的是發現通道的傳輸方式。

認不出來的 label 會**原樣回傳**（例如 `WHATEVER` → `WHATEVER`），
所以自訂 label 的第三方 bus 也能直接用它的 label 當 `in` 的值。

---

## 4. 路由表（唯一政策）

```json
"Router": {
  "enable": 0,
  "routes": [
    { "in": "now",   "out": ["uart1"] },
    { "in": "uart1", "out": ["self"] },
    { "in": "net",   "out": ["uart2"] }
  ]
}
```

### 4.1 欄位

| 欄位 | 型別 | 說明 |
|---|---|---|
| `in` | **字串（純量）** | 這一條 route 的觸發來源。**一個來源 = 一條 route**，結構上不可能衝突 |
| `out` | **列表** | 目的地佇列，依序送出。**一個也要寫列表** |
| `out` 內的 `"self"` | 保留字 | 進本地解碼鏈執行 |

### 4.2 `in` 為什麼是純量而不是列表

| | `in` 純量（採用）| `in` 列表（已否決）|
|---|---|---|
| 一個來源有幾條 route | **恰好 1 條 = 可判定** | 可能重疊多條，要定優先序 |
| 改一個來源的行為 | 找到那一條就好 | 要掃全表 |
| 衝突 | 結構上不可能 | 必須定義「先匹配誰」|

需要「多個來源」時**寫兩條**，比列表更好讀：

```json
{ "in": "uart1", "out": ["self"] },
{ "in": "uart2", "out": ["self"] }
```

> 這一版**不支援** `"in": ["now","net"]`。寫了會**明確報錯並跳過該條**（不猜你的意思），
> 因為猜錯比報錯更難排查。

### 4.3 支援的拓撲

| 拓撲 | 寫法 |
|---|---|
| 點對點 | `{ "in": "now",   "out": ["uart1"] }` |
| 一對多 | `{ "in": "net",   "out": ["uart2","udp"] }` |
| 多對一 | `{ "in": "uart1", "out": ["self"] }` ＋ `{ "in": "uart2", "out": ["self"] }` |
| 本地執行 | `{ "in": "uart1", "out": ["self"] }` |
| 執行＋轉發 | `{ "in": "now",   "out": ["self","net"] }` |

---

## 5. 行為規則（全部）

| 情況 | 行為 |
|---|---|
| `enable: 0` | 全部不作用（與未導入 Router 前 100% 相同）|
| `in` 沒有對應 route | **不執行、不轉發**（沒配對＝沒路走）|
| `out = ["uart1"]` | 只轉發，**本地不執行** |
| `out = ["self"]` | 只本地執行，不轉發 |
| `out = ["self", "net"]` | **本地執行 ＋ 同時轉發** |
| `out` 含 `in` | **開機拒絕該 route**（自我反射，永遠是錯的）|
| `out` 含 `"udp"` | ⚠️ **允許但發出警告**（見 §7.2）|
| `out` 為空 | 警告，該 route 無作用 |
| `out` 寫成字串 | 接受，但警告（建議一律寫列表）|
| 介面名解析不到 | 警告 + 丟棄該幀 + 計數 |

### 5.1 「送出去就不執行」與 `self` 的關係

使用者的原始規則：**「發射了出去的東西，就不會進行本地解碼了」**。
`self` 保留字讓這條規則可以精確表達：

- `out` **不含** `self` → 純轉發，本地不執行
- `out` **含** `self` → 本地執行（要不要順便轉發，看列表裡還有沒有別的）

**用一個列表同時表達「執行」與「去哪」，不需要額外旗標。**

### 5.2 判定順序

```python
def gate(self, bus_obj, addr, cmd):
    if not self.enable:      return OK      # 未啟用，完全不作用
    if bus_obj is None:      return OK      # 呼叫端沒給來源，不介入
    spec = self.by_in.get(name_of(bus_obj))
    if spec is None:         return DROP    # 沒配對 = 沒路走
    return spec.verdict                     # EXECUTE / FORWARD / BOTH
```

---

## 6. 回程：不需要任何設定

**「怎樣來，就怎樣走」** 是既有的實作方式，Router 完全不需要介入：

| 通道 | 收包時自動記錄 | 位置 |
|---|---|---|
| ESP-NOW | `_decode_ctx["_peer_mac"] = peer` | `now_bus.py:179` |
| UDP | `self.target_addr = addr` | `net_bus.py:358` |

而 action 回覆走的是 **`ctx["send"]`＝來源通道的 `write`**（`app.py:54`），
**tx 方向完全不經過 Router**：

```python
# app.py:51-54
ctx = { "app": self, "transport": transport_name, "send": send_func }
```

> **這代表：回覆不會觸發任何 route，因此不可能自己成環。**
> 成環只可能來自人為寫錯路由（見 §7）。

---

## 7. 循環（loop）問題

### 7.1 目前設計裡不會成環，因為

1. **回覆不經過 Router**（§6）—— 回覆不觸發 route
2. **`in` 沒配對就不走** —— 沒有反向規則就沒有迴路
3. **`out` 含 `in` 開機拒絕** —— 自我反射在設定階段就消滅

### 7.2 唯一的人為成環途徑（**刻意不防**）

```
        ┌──── net (WS) ────┐
PC ─────┤                  ├──── Router
        └──── udp (發現) ──┘
```

`net` 和 `udp` 是**同一台 PC 的兩張臉**。若同時寫了：

```json
{ "in": "net", "out": ["udp"] },
{ "in": "udp", "out": ["net"] }
```

就會成環（PC 的 discovery 服務會自動回話，`on_connect_request` 就是這個機制）。

**決策：不防。** 理由（使用者定調）：

> 「這是一個顯然的人為錯誤，並且他成功執行了他應該執行的行為。
>   回覆也不會觸發行為，所以我們也不用太擔心，最多只是當雜訊丟棄。」

**原本的系統也沒有任何防禦**（實測 grep：`dedup` / `duplicate` / `seen` / `storm` / `loop_detect` 全部零筆），
所以升級**不應該偷偷加上去**。

**排查方式**：`Router` 在 `load()` 時對 `out` 含 `udp` 的 route 印**一次**警告；
執行期靠 `ROUTER_STATUS`（P5）的計數器看誰在狂送。

### 7.3 已否決的防環機制（連同理由，勿重提）

| 機制 | 內容 | 否決理由 |
|---|---|---|
| `links` 同對端歸群 | 宣告 net/udp 同一台，禁止互送 | 人為錯誤不是設計缺陷；且多一個設定點＝同一個問題兩種設法 |
| `protect` 禁當出口 | 明確禁止某些介面當出口 | 同上；「要禁就在 `routes` 裡不寫它」，一眼看見 |
| `dedup_ms` 內容去重 | CRC32 相同則丟 | 原本沒有防禦，不該偷加；且會 **誤殺合法重送** |
| `max_hops` 跳數 | 每跳 +1，超過丟棄 | **NC4 header 9 byte 已滿，沒地方放**；且對「回覆型環」無效 |
| `max_fwd_per_sec` 速率限制 | 每條 route 每秒上限 | 使用者要求先不加功能；環的後果是「雜訊變多」，看得見 |
| Router 信封 | 幀外再包一層放跳數 | 每幀 +13 byte，ESP-NOW 250 上限吃緊 |

---

## 8. 為什麼沒有獨立的 RouterTask

`Task` 類別（TaskManager 調度）與「資料路徑任務」是兩件事：

| 名詞 | 要不要 |
|---|---|
| 一個去 poll / 讀 `rx_hub` 的任務 | ❌ **絕對不要**（§2.1 會搶幀）|
| `Task` 類別（`on_start` / `loop` / `on_stop`）| ❌ 不需要，家事掛在 `bus_decode` 尾端即可 |

`RouterTask` 原本只負責：session 逾期清理、統計窗口、設定熱重載 —— 全部是微秒級運算，
放在 `BusDecodeTask.loop()` 尾端一行 `housekeep()` 就夠。

**代價**：`SYS_TASK_QUERY` 看不到 router。**補救**：`ROUTER_STATUS`（P5）自己回報。

**什麼時候才需要獨立 Task**：要做「非同步重送 / 佇列排程」時（目標通道忙線排隊、逾時重送、
優先級排序）。**第一版不做** —— 先量到實際丟包率再決定。

---

## 9. 參數：只用兩個，其餘全部砍掉

```json
"Router": {
  "enable": 0,
  "routes": [ ... ]
}
```

### 9.1 被砍掉的參數與理由（完整清單）

| 曾提議 | 原本用途 | 砍掉的理由 |
|---|---|---|
| `peers` | MAC ↔ cid 對照表 | **回覆地址是協議的事**：`IDENTIFY_REQ.reply_addr`（`sys.json:36`）與 `SET_MASTER.master_cid`（`sys.json:66`）已經由發起方帶上來。回程 MAC 是**學來的**（`_peer_mac`），廣播更不需要。**MAC 是學來的，不是填來的** |
| `self` | 本機 cid 覆寫 | 已有唯一真相 `System.cID` → `bus.cid`（`ConfigManager.py:330`），Router 不重複定義 |
| `ifaces` | 介面實例清單 | 介面存在性屬驅動層（`UART.list` / `Network.*`），Router 只引用不重複宣告 |
| `links` / `protect` | 防環 | §7.3 |
| `dedup_ms` / `max_hops` / `max_fwd_per_sec` | 防環 | §7.3 |
| `bypass_cmds` | 逃生門（0x16xx 等強制本地執行）| **可用 route 表達**（`{ "in": "net", "out": ["self"] }`），同一個問題一種設法。⚠️ 代價見 §10 |
| `stat_window_ms` | 統計窗口 | 統計改成 `ROUTER_STATUS` 查詢時即時計算 |
| `max_frame` | 轉送 buffer 上限 | 沿用 `proto.RX_BUF_SIZE`（4115 = 剛好一幀）|
| `send_retry` | 送出重試 | 沿用 `Buffer.send_retry`（`net_bus.py:48` / `circuit_bus.py:31`）|

### 9.2 沿用的既有參數（不重複定義）

| 需求 | 沿用的既有參數 |
|---|---|
| ESP-NOW 頻道 | `Network.ESP_now.channel` |
| 哪些 UART 存在 | `UART.list[].id` / `baudrate` / `GPIO` |
| 訊框最大長度 | `proto.RX_BUF_SIZE` |
| 協議負載上限 | `proto.MAX_PAYLOAD`（8192）|
| 幀頭／CRC 長度 | `proto.HDR_LEN` / `CRC_LEN` |
| 廣播位址 | `proto.ADDR_BROADCAST` |
| 送出重試 | `Buffer.send_retry` |
| hub 滿了丟不丟 | `Buffer.drop_on_full` |
| 除錯輸出 | `System.debug_level` + `dprint()` |

---

## 10. 已知限制與風險（誠實清單）

| # | 限制 | 影響 | 處置 |
|---|---|---|---|
| 1 | **沒有遠端逃生門** | 路由設錯時，管理指令可能被路由走 → 只能實體重刷 | 使用者已知悉並接受（§9.1）。**改路由前先確認管理通道有 `out: ["self"]`** |
| 2 | `out` 含 `udp` 可成環 | 雜訊放大（§7.2）| 不防；靠 `load()` 警告 + 自律 |
| 3 | 沒有跳數 | 跨多台裝置互指可成環 | §7.3；需要時再設計（三種放法已評估）|
| 4 | 回覆不經過 Router | 無法對「回覆」做路由政策 | 目前視為正確行為（§6）|
| 5 | `cID` 未指派時 `bus.cid = 0xFFFF` | 廣播幀會被**每一站**重複執行 | ⚠️ **要做多跳轉運，請先指派 `System.cID`** |
| 6 | `CircuitDecode` 的 `uart` 值是**索引**且未寫明 | 寫 `uart: 2` 想選 `id=2` 卻選到第 3 條（或什麼都沒選到）| §3.2 已寫明語意；P4 補「選不到就警告」 |
| 7 | 無速率限制 | 環發生時會吃滿 CPU | 看得見（雜訊），不會靜默 |
| 8 | `CircuitDecode` 的 `spi` / `i2c` / `can` key 永遠對不到 | 寫了沒有作用 | 已知缺口，不在本次範圍（`CircuitTask` 只建 UART bus）|

---

## 11. 分期施工

| 階段 | 內容 | 風險 | 驗證 |
|---|---|---|---|
| **P1** | `signal_router.py` 核心：`load()` + route 正規化 + 配對 + `out` 展開 + 開機驗證。純函式庫 | **零**（沒有東西呼叫它）| ✅ `test/protocol/router_selftest.py`（74 項離線全過）|
| **P2** | `Proto.pack_into()`：把 P1 內部私有的組幀邏輯搬進 `proto.py`，與 `pack()` 共用內核 | 低（加法）| 擴充 selftest：對比 `pack()` 與 `pack_into()` 輸出 |
| **P3** | `app.py` hook + `bus_decode` 掛鉤。`enable:0` 時一行都不進 | 中 | 板上回歸：`enable:0` 行為 100% 不變 |
| **P4** | `CircuitDecode` **不動**（§3.2）；只補「選不到線就警告」+ 文件寫明 `uart` 是索引 | **低**（只有警告，無行為變更）| 板上：`uart:0` 選到第 1 條；`uart:5` 出現警告 |
| **P5** | 0x16xx 指令（`ROUTE_ADD` / `ROUTE_DEL` / `TABLE_GET` / `STATUS`）| 低 | 板上往返 |
| **P6** | ESP-NOW ↔ UART 實測 + 文件 | — | 兩板 |

### 11.1 什麼被修、什麼沒被修

**修（P4，只有加法）：**

| 項目 | 位置 | 內容 |
|---|---|---|
| **靜默失敗** | `circuit.py:87-124` | `CircuitDecode` 宣告的索引對不到任何 UART 時**沒有任何訊息** → 補一行警告，明確指出「宣告了 uart:N 但沒有對應的線」 |

**不修（`uart` 是索引，現行程式碼本來就是對的）：**

`selected.add(("uart", int(gpio["uart"])))` 比對 `("uart", idx)` —— **語意一致，是索引對索引**，
只是**沒有任何地方寫明**，所以看起來像 bug。處置是**把語意寫進文件**（§3.2），不是改程式碼。

> 原本的判斷（「id/idx 混用 → `enable:1` 卻永遠選不到線」）是**錯的**：
> `P4-ETH_mp3` 的 `CircuitDecode.enable = 1` + `uart: 0` **確實選中了第 1 條線**，
> 因為雙方都是索引。真正的缺口只有「對不到時不出聲」。

---

## 12. 執行期指令（P5，規劃）

指令域 **`0x16xx`**（目前完全未被佔用）。

| CMD | 名稱 | 功能 |
|---|---|---|
| `0x1601` | `ROUTER_STATUS` | 介面狀態 / 每條 route 的命中與丟棄計數 |
| `0x1602` | `ROUTER_ROUTE_ADD` | 新增或覆寫一條 route（JSON）|
| `0x1603` | `ROUTER_ROUTE_DEL` | 刪除一條 route（依 `in`）|
| `0x1604` | `ROUTER_TABLE_GET` | 讀回目前路由表（分頁，payload 上限 8192）|
| `0x1605` | `ROUTER_SAVE` | 存回 `config.json`（`cfg_manager.save_from_bus`，無損更新）|
| `0x1606` | `ROUTER_ACK` | 執行結果回覆 |

---

## 13. 快速參照

```json
// config.json —— 最簡可用
"Router": {
  "enable": 1,
  "routes": [
    { "in": "now",   "out": ["uart1"] },     // ESP-NOW 進來 → 轉給 UART1
    { "in": "uart1", "out": ["self"] },      // UART1 進來 → 本地執行
    { "in": "now",   "out": ["self"] }       // ESP-NOW 進來 → 本地執行
  ]
}
```

**心智模型三句話：**

1. 每條線自動註冊，**沒配對就沒路走**。
2. 一條 route = 一個來源 ＋ 一個出口清單。
3. `self` 是出口清單裡的保留字，代表「進本地解碼」。

**相關文件**：`doc/02_guides/15_schedule.md`（同類的任務驅動模式）、
`doc/03_notes/02_buffer_architecture.md`（rx_hub 為什麼是 SPSC）、
`todo/03_signal_router.md`（驗收清單）。
