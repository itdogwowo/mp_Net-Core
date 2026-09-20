# 訊號 Router（Router 任務）

> **用途**：讓 ESP-NOW / 網路 / 實體線之間可以**互相轉送訊號**，並決定每一條線收進來的幀
> 要「本地執行」還是「轉送出去」還是「兩者都做」。路由由 `config.json` 宣告，不寫死。
> **位置**：`slave/lib/sys/signal_router.py`（核心）＋ `slave/tasks/bus_decode.py`（掛鉤）
> ＋ `slave/action/router_actions.py` + `slave/schema/router.json`（0x16xx 執行期指令）
> **狀態**：P1–P5 ＋ 自動註冊（P7）已落地
>           - 離線自測 440 項全過：`python3 -B test/protocol/router_selftest.py`（PC，不需硬體）
>           - **真機 114 項全過**：`test/protocol/router_board_test.py`（ESP32-S3 @160MHz）
>           - **待做**：完整固件的整機回歸、真通道 0x16xx、P6 端到端（清單見 `todo/03_signal_router.md`）
> **最後更新**：2026-09（P2–P5 + P7；真機驗證完成）

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

#### ⚠️ 掛鉤位置：在 **ADDR 過濾之前**（P3 實作決策）

`handle_stream` 既有的 ADDR 過濾（只收廣播或本機 `cID`）排在 gate **後面**：

```python
_ver, addr, cmd, payload = r
if router is not None:
    verdict = router.gate(src_bus, addr, cmd, payload)   # ★ 先判定（含轉送）
    if verdict != V_OK and not (verdict & V_EXECUTE):
        continue                                          # 只轉發／沒配對 → 不執行
if addr != ADDR_BROADCAST and addr != my_cid:
    continue                                              # 既有行為，一行未改
disp.dispatch(cmd, payload, ctx)
```

| | 效果 |
|---|---|
| `enable=0` | `gate()` 立刻回 `V_OK` → **與未導入 Router 前逐行相同**（selftest §7 逐項比對）|
| `enable=1` + 命中 route | **過路幀**（位址不是本機的幀）也會被轉送 → 支援「Remote 的指令轉給下層節點」|
| `enable=1` + 沒配對 | 不執行、不轉發（§5）|

> 放在過濾**之後**的話，只有「給本機」的幀能被轉送，對「閘道器／轉運」的用途來說等於少一半功能，
> 而且需要在 gate 內再判一次位址。放前面則**多支援過路轉運，且 disable 時零差異**。
> 這一條只影響 `enable=1` 的部署，已由 selftest §7.10 固定行為。

### 2.3 檔案與責任

| 檔案 | 責任 |
|---|---|
| `lib/sys/signal_router.py` | 路由表 + 匹配 + 轉送 + 統計（純邏輯，**CPython 可離線測試**）|
| `lib/sys/proto.py` | `Proto.pack_into()` — 轉送組幀用的共用內核（與 `pack()` 位元組相同）|
| `tasks/bus_decode.py` | 建立 router、註冊服務 `signal_router`、週期 `sync_ifaces()`、每幀呼叫 `gate`、尾端 `housekeep()` |
| `app.py::handle_stream` | 依 verdict 決定要不要 `disp.dispatch()`（收 `router` / `src_bus` 兩個選填參數）|
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

**註冊時機（P3）**：`BusDecodeTask` 每 100ms 呼叫一次 `router.sync_ifaces(bus)`，把當下已上線的通道
補進 Router（重複呼叫是 no-op）。來源三處，依權威性排序：

| 順序 | 來源 | 涵蓋 |
|---|---|---|
| 1 | 具名服務 `NowBus` / `net_bus_ctrl` / `net_bus_discovery` | `now` / `net` / `udp` |
| 2 | `circuit_bus_all_list` | `uartN` —— **含沒進 `CircuitDecode` 的線**（它們仍可當出口，§3.2）|
| 3 | `bus_sources.list()` | `vbus` 等沒有具名服務的線 |

> 為什麼要週期補註冊而不是在 `on_start` 註冊一次：ESP-NOW 可能在 WiFi 就緒後才 `init` 成功、
> `vBus` 是 `ScheduleTask` 惰性建立 —— 開機當下的介面集合不等於五分鐘後的介面集合。

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

### 4.4 自動註冊 —— **每次啟動都跑**，不是開關

Router 啟動時（以及之後每有新線路上線時）會**檢視自己現在真的有哪些線路**：

```
實際存在的線路          config.routes                  結果
────────────────       ────────────────────────       ─────────────────────────────
now, uart1, uart2  ＋  []                         →   自動補 3 條 {in: X, out: ["self"]}
now, uart1, uart2  ＋  [{in: now, out:[uart1]}]   →   now 用你的；uart1/uart2 自動補
now, uart1         ＋  [{in: uart2, out:[self]}]  →   uart2 不存在 → 無視它、跳過建立
```

| 規則 | 行為 |
|---|---|
| 線路存在、`routes` 裡沒有 | **自動補** `{ "in": 線路, "out": ["self"] }`，並**寫回 `config.json`** |
| 線路存在、`routes` 裡有了 | **尊重使用者的**，一個字都不動 |
| `routes` 寫了、**實體不存在** | **無視它、跳過它的建立** —— 不生效、不出現在 `ROUTER_TABLE_GET`；route 留在 config 裡，**線路上線就自動生效**（不必重啟）|
| 寫回 config 的時機 | 只在真的補了新線路時寫**一次** → 開機後不會反覆寫檔 |
| `enable: 0` 時 | **照樣自動註冊**（方便你先把 config 看清楚再決定要不要開）|

**為什麼補的是 `self`**：`out: ["self"]` ＝ 這條線自己收、自己執行 ——
正是**未導入 Router 前的行為**。所以自動註冊之後，**打開 `enable:1` 不會讓任何一條線失效**；
你只需要改「想轉送」的那幾條，不必先把每條線都寫一遍。

> **要讓一條線「不執行」**：明確寫 `{ "in": "uart1", "out": [] }`（＝`V_DROP`，見 §5）。
> 直接把那條從 `routes` 刪掉沒用 —— 下次啟動會被自動補回來。

> ⚠️ 自動註冊只由 `sync_ifaces()` 驅動（`BusDecodeTask` 每 100ms 呼叫）。
> 直接呼叫 `register_iface()` 的低階路徑不會觸發它。

---

## 5. 行為規則（全部）

| 情況 | 行為 |
|---|---|
| `enable: 0` | 全部不作用（與未導入 Router 前 100% 相同）|
| `in` 沒對應 route，**但線路存在** | 自動註冊會補上 `out: ["self"]`（§4.4）→ 本地執行 |
| `in` 沒對應 route，且自動註冊已被 `ROUTE_DEL` 跳過 | **不執行、不轉發** |
| `out = ["uart1"]` | 只轉發，**本地不執行** |
| `out = ["self"]` | 只本地執行，不轉發 |
| `out = ["self", "net"]` | **本地執行 ＋ 同時轉發** |
| `out = []` | **明確不執行也不轉發**（＝把一條線關掉的正確寫法）|
| `out` 含 `in` | **開機拒絕該 route**（自我反射，永遠是錯的）|
| `out` 含 `"udp"` | ⚠️ **允許但發出警告**（見 §7.2）|
| `out` 寫成字串 | 接受，但警告（建議一律寫列表）|
| 介面名解析不到 | 警告 + 丟棄該幀 + 計數 |
| 幀位址**不是本機**且命中 route | **仍然轉送**，但永不本地執行（過路轉運，§2.2）|
| 幀位址不是本機且 `enable: 0` | 與舊版相同：直接跳過（ADDR 過濾）|

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

> `spec is None` 在**生產環境幾乎不會發生** —— 線路存在就一定被自動註冊補上（§4.4）。
> 會落到 `DROP` 的只有兩種：使用者明確寫了 `out: []`，或該線路被 `ROUTE_DEL` 跳過。

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
| 6 | `CircuitDecode` 的 `uart` 值是**索引**且未寫明 | 寫 `uart: 2` 想選 `id=2` 卻選到第 3 條（或什麼都沒選到）| ✅ §3.2 已寫明語意；P4 已補「選不到就警告」（`circuit.py::_warn_unmatched`）|
| 7 | 無速率限制 | 環發生時會吃滿 CPU | 看得見（雜訊），不會靜默 |
| 8 | `CircuitDecode` 的 `spi` / `i2c` / `can` key 永遠對不到 | 寫了沒有作用 | 已知缺口，不修（`CircuitTask` 只建 UART bus）；P4 已讓它**出聲** |
| 9 | 出廠 config 的 `Router.enable` 都是 `0` | 不先開就什麼都不作用（＝安全的預設）| 用 `0x1605 ROUTER_SAVE` 帶 `enable=1` 開啟（§12.2 / §13.1）|
| 10 | 過路幀（位址非本機）在 `enable=1` 時也會被轉送 | 未指派 `cID` 時廣播幀會在多站重複搬運 | 這是「閘道器」要的行為（§2.2）；**多跳前先指派 `System.cID`**（同第 5 點）|
| 11 | **`ports/P4/ESP32-P4-ETH_mp3/` 是 fork，沒有這份程式碼** | 該 port 的 `config.json` 雖有 `Router` 區塊（`enable: 0`），改它**不會有任何作用也不會出聲** | 同步該 port 之前**不要開**（詳見 `todo/03_signal_router.md`）|

---

## 11. 分期施工

| 階段 | 內容 | 風險 | 狀態 |
|---|---|---|---|
| **P1** | `signal_router.py` 核心：`load()` + route 正規化 + 配對 + `out` 展開 + 開機驗證。純函式庫 | **零**（沒有東西呼叫它）| ✅ `test/protocol/router_selftest.py` §1–§5 |
| **P2** | `Proto.pack_into()`：與 `pack()` 共用 `_write_frame` 內核 | 低（加法）| ✅ selftest §6（含逐位元組對比 `pack()`）|
| **P3** | `app.py` hook + `bus_decode` 掛鉤 + `sync_ifaces()`。`enable:0` 時一行都不進 | 中 | ✅ 實作完成；selftest §7 逐項回歸；**板上實測待做** |
| **P4** | `CircuitDecode` **不動**（§3.2）；只補「選不到線就警告」+ 文件寫明 `uart` 是索引 | **低**（只有警告，無行為變更）| ✅ 實作完成 + selftest §8；板上待驗 |
| **P5** | 0x16xx 指令（`ROUTE_ADD` / `ROUTE_DEL` / `TABLE_GET` / `STATUS` / `SAVE` + `ACK`）| 低 | ✅ 實作完成 + selftest §9–§11；**板上往返待做** |
| **P6** | ESP-NOW ↔ UART 實測 + 文件 | — | ⏳ 待做（需兩板）|
| **P7** | **自動註冊**（§4.4）：每次啟動檢視實際線路，沒 route 就補 `out: ["self"]` 並寫回 config | 低（只新增 route，不動既有）| ✅ 實作完成 + selftest §12；**板上寫檔待驗** |

### 11.1 什麼被修、什麼沒被修

**修（P4，只有加法）：**

| 項目 | 位置 | 內容 |
|---|---|---|
| **靜默失敗** | `circuit.py` `_warn_unmatched()` | `CircuitDecode` 宣告的索引對不到任何 UART 時**沒有任何訊息** → 現在明確指出「宣告 uart:N 但沒有對應的線（uart 是索引，0-based）」；`spi` / `i2c` / `can` 這些永遠對不到的 key 也一併出聲 |

**不修（`uart` 是索引，現行程式碼本來就是對的）：**

`selected.add(("uart", int(gpio["uart"])))` 比對 `("uart", idx)` —— **語意一致，是索引對索引**，
只是**沒有任何地方寫明**，所以看起來像 bug。處置是**把語意寫進文件**（§3.2），不是改程式碼。

> 原本的判斷（「id/idx 混用 → `enable:1` 卻永遠選不到線」）是**錯的**：
> `P4-ETH_mp3` 的 `CircuitDecode.enable = 1` + `uart: 0` **確實選中了第 1 條線**，
> 因為雙方都是索引。真正的缺口只有「對不到時不出聲」。

---

## 12. 執行期指令（P5，已落地）

指令域 **`0x16xx`**（原本完全未被佔用）。schema 唯一真相：`slave/schema/router.json`；
handler：`slave/action/router_actions.py`。

| CMD | 名稱 | 方向 | Payload | 說明 |
|---|---|---|---|---|
| `0x1601` | `ROUTER_STATUS` | Master→Slave | (空) | 回 `0x1606`：介面清單 / 每條 route 的 hit·fwd·drop / load 期間的錯誤 |
| `0x1602` | `ROUTER_ROUTE_ADD` | Master→Slave | `route_json(str)` | 新增或覆寫一條 route（單行 JSON，如 `{"in":"now","out":["uart1"]}`）|
| `0x1603` | `ROUTER_ROUTE_DEL` | Master→Slave | `in_name(str)` | 依 `in` 刪除一條 route |
| `0x1604` | `ROUTER_TABLE_GET` | Master→Slave | `page(u8)` | 回 `0x1606`，`data_json` = `{page,pages,page_size,total,routes}` |
| `0x1605` | `ROUTER_SAVE` | Master→Slave | `enable(u8)` | 存回 `config.json`（`cfg_manager.save_from_bus(update_key="Router")`）|
| `0x1606` | `ROUTER_ACK` | Slave→Master | `ok(u8)` `code(u16)` `message(str)` `data_json(str)` | 唯一回覆 |

### 12.1 `ROUTER_ACK.code`

| code | 意義 |
|---|---|
| `0` | OK |
| `1` | router 尚未啟動（`BusDecodeTask` 還沒建立 `signal_router`）|
| `2` | `route_json` 不是合法 JSON |
| `3` | route 本身驗證不通過（`in`/`out` 欄位問題；訊息與開機 `load()` 同一份，直接透傳）|
| `4` | 寫 `config.json` 失敗（**此時設定未變更**，見下）|

### 12.2 `ROUTER_SAVE` 的 `enable` 位元組（沿用本專案 0xFF 慣例）

| 值 | 行為 |
|---|---|
| `0xFF` | 只存檔，**不改開關**（＝「我就只是要存檔」要送的值）|
| `0` | 存檔 + 關閉 Router |
| `1` | 存檔 + 開啟 Router |
| 其他 | 不改開關（不猜）|

> ⚠️ **送 `ROUTER_SAVE` 一定要帶這 1 byte**。沒帶 = payload 長度 0 → 解碼出 `0` → 等於「關閉」。
> 這是本專案既有的「空 payload 欄位補 0」語意（`SchemaCodec`），不是 Router 特有的陷阱。

**原子性**：`enable` 的切換**只在存檔成功後**才生效；存檔失敗會把 `bus.shared["Router"]`
一併回復原狀並回 `code=4`。理由：「說存檔失敗但開關已經翻了」是最難排查的半套狀態。

### 12.3 執行期 vs 開機

- `ROUTE_ADD` / `ROUTE_DEL` **立即生效**（改的是 `BusDecodeTask` 建立的那個實例），不需要重啟。
- `ROUTER_SAVE` 只負責持久化；沒存就重開機，改動會消失。
- `enable` 在**所有出廠 config.json 都是 `0`**：要先用一次 `ROUTER_SAVE` 帶 `enable=1`
  （或直接改 config.json）開啟，之後就能全遠端管理路由。
- **自動註冊（§4.4）在 `enable: 0` 時也會跑並寫回 config.json** —— 所以第一次開機後，
  config 裡就會自動長出「這台真實有哪些線路」的清單，你直接在上面改就好。

---

## 13. 快速參照

```json
// config.json —— 出廠（空的就好，第一次開機會自動長出真實線路，見 §4.4）
"Router": { "enable": 0, "routes": [] }

// 開機自動註冊之後（例：這台有 ESP-NOW + 2 條 UART + WS 控制通道）
"Router": {
  "enable": 0,
  "routes": [
    { "in": "net",   "out": ["self"] },      // ← 自動補的：維持原行為
    { "in": "now",   "out": ["self"] },      // ← 自動補的
    { "in": "uart1", "out": ["self"] },      // ← 自動補的
    { "in": "uart2", "out": ["self"] }       // ← 自動補的
  ]
}

// 你只要改想轉送的那幾條
"Router": {
  "enable": 1,
  "routes": [
    { "in": "now",   "out": ["uart1"] },     // ESP-NOW 進來 → 轉給 UART1（本地不執行）
    { "in": "uart1", "out": ["self"] },      // UART1 進來 → 本地執行
    { "in": "uart2", "out": [] },            // UART2 完全不要（明確關掉）
    { "in": "net",   "out": ["self"] }
  ]
}
```

**心智模型三句話：**

1. 每條線自動註冊，**沒配對就沒路走**。
2. 一條 route = 一個來源 ＋ 一個出口清單。
3. `self` 是出口清單裡的保留字，代表「進本地解碼」。

### 13.1 遠端開啟 Router（出廠 enable=0 → 全遠端管理）

```
1) 0x1602 ROUTER_ROUTE_ADD  {"in":"now","out":["uart1"]}
2) 0x1602 ROUTER_ROUTE_ADD  {"in":"uart1","out":["self"]}
3) 0x1604 ROUTER_TABLE_GET  確認表對了
4) 0x1605 ROUTER_SAVE       enable=1     ← 存檔成功的同時就生效
5) 0x1601 ROUTER_STATUS     看 hit/fwd/drop 確認流量真的在走
```

> 第 4 步之前 Router 是關的（`enable=0`），所以第 1~3 步的指令本身不受路由影響 ——
> **先確認表對了再開**，是唯一「不會把自己鎖在門外」的順序（§10 限制 1）。
>
> 而且第 1~2 步通常**不用自己寫**：自動註冊（§4.4）已經把每條真實線路補成 `out: ["self"]`，
> 你只要把要轉送的那幾條 `ROUTE_ADD` 覆寫掉就好。

### 13.2 驗證

```bash
# 離線（PC，不需硬體）
python3 -B test/protocol/router_selftest.py     # 440 項

# 真機（MicroPython；需先把 lib/sys/*、schema/*.json、action/router_actions.py 部署上去）
mpremote connect <PORT> fs cp test/protocol/router_board_test.py :router_board_test.py
mpremote connect <PORT> exec "exec(open('/router_board_test.py').read())"   # 114 項
```

真機實測數字（ESP32-S3 @160MHz）:

| 項目 | 成本 |
|---|---|
| `gate()`（`enable=0`，含 Python 呼叫開銷） | 23.1 µs/次（空函式基準 15.5 µs ⇒ **增量 7.6 µs**）|
| `handle_stream()` 整幀（無 router vs `enable=0`） | 1120 µs vs 1036 µs ⇒ 差在量測雜訊內 |

> 上面 `handle_stream` 的絕對值比初版量到的小了一半以上 —— 因為 `proto.py` 的寫入路徑
> 已改成「先取小視圖再 `[:]` 賦值」（真機 `feed+pop` 872 → 215 µs/幀）。
> 詳見 `doc/03_notes/01_changelog.md` §29 與 `test/protocol/bench_slice_assign.py`。

**相關文件**：`doc/02_guides/15_schedule.md`（同類的任務驅動模式）、
`doc/03_notes/02_buffer_architecture.md`（rx_hub 為什麼是 SPSC）、
`doc/01_protocol/02_command_index.md` §7（0x16xx 指令索引）、
`todo/03_signal_router.md`（驗收清單）。
