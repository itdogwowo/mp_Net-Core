# 遙控器強化計劃 — 配對 / 綁定 / 晶片開關

> **用途**：把「強化遙控器」這件事的**共識、現況盤點、分階段做法、踩過的地雷**集中一處。
> **本檔自足**：不依賴任何對話上下文，單獨看就能接手。
> **狀態**：PLANNED（**P0 ~ P6 已完成**；P7 發送任務待做）
> **最後更新**：2026-09-26
> **相關文件**：
> - `doc/03_notes/18_pixel_panel_control_path.md` — 面板 pixel 控制路徑（定向查詢的來源）
> - `doc/03_notes/17_tx_render_center_plan.md` — 統一發射中心（本計劃的上游）
> - `doc/02_guides/15_schedule.md` — schedule（vBus 注入路徑）
> - `doc/02_guides/16_signal_router.md` — Router（路由政策）
> - `ports/S3/ESP32-S3-Control_Panel_V2/README.md` — 面板角色與設定備註
> - `ports/S3/ESP32-S3-Test_Peer/README.md` — 零硬體測試對端（§12）
> - `todo/03_signal_router.md` — Router 的測試追蹤

---

## 0. 一句話

面板是**遙控器**：只發指令、不自己執行。
本計劃讓它做三件事：**看見節點 → 建立通道（配對）→ 確認方向（Master）**，
並把晶片開關（Wi-Fi / ESP-NOW / ESP-NOW 配對）搬上 UI。

---

## 1. 範圍

| | 內容 |
|---|---|
| **做** | 遙控器 UI 頁（掃描 / 節點清單 / 綁定方向 / 晶片開關）；節點與配對關係的半永久儲存；身份 / 角色 / 目標的持久化 |
| **不做（本階段）** | 不改 LVGL 版面以外的 UI 框架；不動 Router 的路由政策；**不處理既有技術債**（例如「同一個亮度有三個寫入者」—— 已知，先擱置） |

---

## 2. 架構共識（**已定案，請勿重新發明**）

### 2.1 指令是唯一的行為介面

所有行為（UI 操作、排程、遠端指令、狀態變化）都收斂成指令。
**硬體只在指令 handler 裡被碰**，其他任何地方都只「產生指令」。

### 2.2 狀態模型

| 角色 | 職責 |
|---|---|
| **指令 handler** | 狀態的**唯一生產者**（例：`on_mode_set` → `gmode.set_mode()` → `bus.shared["mode_id"]`） |
| **UI** | **只觀察狀態（顯示）＋ 發送指令** —— 不寫狀態、不管理狀態 |
| **外部指令** | 走同一個 handler → 改同一個狀態 → UI 自然看到 |

→ **所以「純轉發型」的中間任務可以逐步移除**（例如 `ControlPanelTask._forward_display_cmd()`
—— 它只是把 `bus.shared["_display_cmd"]` 翻成廣播，UI 直接發射就取代了）。

⚠️ **成立的前提：每個狀態只能有一個寫入者。** 現在樹上違反此點的地方（已知技術債，先擱置）：
同一個「亮度」有三個寫入者 —— `ui/lvgl/page/control_panel.py:265`（`_local_bright`）、
`ui/lvgl/page/pixel_controller.py:269`（slider 值）、`tasks/action_task_1.py:758`（`_temp_brightness`）。

### 2.3 配對 vs 方向（★ 這是本計劃的核心區分）

| | 定義 | 層次 | 對稱性 | 對應零件 |
|---|---|---|---|---|
| **配對** | **建立一條獨立的通道** | 射頻層（實體） | ✅ **對稱** —— 兩端互相認識，沒有 Master/Slave | `espnow.add_peer(mac)`（**兩端各自做**） |
| **方向** | **由指令確認 Master 方向** | 協議層（邏輯） | ❌ 有方向 | `0x1016 SET_MASTER`（告訴對方「你的 master 是我」）→ 對方記 `master_cid` |

> **配對是「一條線的兩端」；方向是「協議上的方向」。兩者概念不同，不要混。**

### 2.4 管子抽象

**所有通訊線都只是「一條一條的管子」** —— ESP-NOW / WS / UDP / UART 一視同仁。
- UI **不需要知道**訊號走哪條管子
- 「走哪條」由 **Router** 決定
- **解碼器做什麼與本層無關**

### 2.5 執行 vs 發射 = **意圖**（Router 管不了意圖）

★ 這是最容易搞混的一點：

| 意圖 | 怎麼做 | 經過 Router 嗎 |
|---|---|---|
| **本地執行** | `app.disp.exec_cmd(cmd, args, ctx)` | ❌ 直接派發 |
| **發射出去** | `app.disp.make_cmd(cmd, args)` → 交給管子 | ✅ 由 Router 選管子 |

**為什麼 Router 決定不了**：`Router.by_in` 是 **dict → 一個來源恰好一條 route**
（`signal_router.py:604`，重複定義會 warn 並以後者為準）。
所以「同一來源、不同指令走不同路」表達不出來 —— 那要靠 `bypass_cmds`，而**那個設計已被否決**
（`signal_router.py:173-177` 的 `_is_bad_key`）。

→ **「這條指令是執行還是發射」必須由產生指令的人決定。**
→ 但 `out` **可以是多條路徑**（`["self","now","net"]`），所以「一個意圖 → 多條管子」是可以的。

### 2.6 廣播 vs 定向 = **發送任務的判斷**（不是設定）

使用者的定義（兩層，都是「不知道目標」的表現）：
1. **ESP-NOW 射頻層廣播** —— 目標 MAC = `FF:FF:FF:FF:FF:FF`
2. **協議層廣播** —— NC4 `addr = 0xFFFF`（不知道 Master/Slave 是誰）

**當目標已知時，就應該自動朝正確的目標送** —— 所以：
- ❌ 不需要「廣播/單播設定開關」（原設計方向錯誤）
- ✅ 需要的是**發送任務的邏輯**：知道目標 → 定向；不知道 → 廣播

**零件全都已經有了**（不需要新 primitive）：

```python
NowBus.broadcast(data)                     # 射頻層廣播
NowBus.send(mac, data)                     # 射頻層定向
NowBus.write_to(mac, data)                 # mac=None 時自動退回 write()
NowBus.send_proto(mac, cmd, payload, addr) # 已有 helper
NowBus.broadcast_proto(cmd, payload, addr) # 已有 helper
Proto.pack(cmd, payload, addr=...)         # NC4 層 addr（0xFFFF 或目標 cid）
```

### 2.7 發送任務的定位（收斂後的架構）

```
        外部指令 ──┐
        UI 頁面 ───┤  ① 只「發送指令」+「觀察狀態」
        schedule ──┤
        實體輸入 ──┘
                   ▼
          ╔════════════════════════╗
          ║ 指令層 exec_cmd / send ║  ② 決定意圖：執行 or 發射
          ╚════════════════════════╝
                   │
        ┌──────────┴──────────┐
   exec_cmd                 send
        │                     │
        ▼                     ▼
  [handler]            ╔══════════════════════╗
  ③ 狀態的唯一生產者    ║ 發送任務              ║  ④ 決定：定向 or 廣播
  ④ 直接操作硬體        ║ - target 已知→定向    ║     管配對表與方向
        │              ║ - 未知→廣播           ║
        ▼              ╚══════════════════════╝
  bus.shared[...]                 │
        │                         ▼
        └──► UI 觀察顯示      [Router] ⑤ 只選管子
                                  │
                              [管子] now/net/udp/uartN  ⑥ 怎麼送
```

★ **「發送任務」目前不存在** —— 它就是把 §2.6 的判斷集中到一處的地方。
本計劃的 UI 階段會先繞過它（見 P5 的取捨），之後再抽。

---

## 3. 指令的三個出入口（**P0 已完成**）

`slave/lib/sys/dispatch.py` 的 `Dispatcher`：

| 方法 | 方向 | 用途 |
|---|---|---|
| `dispatch(cmd, payload_bytes, ctx)` | 收 **bytes** → 解碼 → 派發 | **線上路徑**（行為與導入前逐字相同） |
| `exec_cmd(cmd, args, ctx)` | 收 **args(dict)** → 直接派發 | **內部執行**（按鈕／排程／頁面／task） |
| `make_cmd(cmd, args, addr)` | args → 產生 **NC4 幀** | **產生／發射** |

**檢查／`🔹 [transport] NAME` log／`try` 保護／執行時間** 全部集中在 `exec_cmd`，兩條路共用 → 觀測性一致。

### 用法

```python
# 執行（內部）
app.disp.exec_cmd(0x3106, {"action": 1}, {"app": app, "transport": "ui"})

# 產生（要發射）
frame = app.disp.make_cmd(0x3105, {"mode_type":0,"mode_id":3,
                                   "start_delay_ms":0,"brightness":255})
now_bus.broadcast(frame)          # ★ 立即消費
# 要收集多筆時：frames.append(bytes(frame))   ← ★ 必須複製（共享 buffer）

# 線上（不變）
disp.dispatch(cmd, payload, ctx)
```

### ⚠️ 使用須知

| 事項 | 說明 |
|---|---|
| **缺欄位語意** | `args` 的鍵 = **呼叫端真的給了什麼（不補 0）**。這與 `dispatch` 同一個規則：`decode` 對「payload 裡沒有的欄位」也不會放進 args。對 handler 的意義：**缺欄位 = 用它自己的預設值**（例：`waiting_to_trash.on_ctl` 的 `brightness` 預設 `0xFF`＝不改）。<br>★ 所以內部路徑**省略欄位 = 不改**，而線上路徑要表達「不改」必須**明確送 0xFF**。 |
| **`_name` / `_cmd`** | `decode` 會在 args 裡多放這兩個資訊鍵，`exec_cmd` 不會。已確認**全樹沒有 handler 讀它們**（只用在 debug 顯示）。 |
| **型別準確性** | args 的型別與範圍由**呼叫端負責給準**。編碼器的超界保護是最後一道網，不是常態依賴。 |
| **`web_ui` 是例外** | `tasks/web_ui.py` **刻意保留** `encode→dispatch` 的來回，**不要**改成 `exec_cmd` —— 它是唯一不可信輸入路徑，那一趟 encode 順帶做型別正規化；而且它是人操作的低頻路徑，「省一趟」的理由不成立（已有註解說明）。 |

---

## 4. 現況盤點：**零件清單**（大部分已經有了）

| 層 | 機制 | 位置 | 狀態 |
|---|---|---|---|
| **節點表** | `PeerRegistry` — **雙層定址**（`cid` 邏輯層 + `mac` 射頻層 + `ip`），持久化 | `lib/sys/peer_registry.py` | ✅ 完整（`snapshot` / `with_mac` / `by_cid` / `age_ms` / `forget` / `clear` 都有） |
| **自動學習** | 被動：任何帶 MAC 的幀 → `learn_from_frame()` | `tasks/bus_decode.py:229-234` | ✅ 已接 |
| **自動學習** | 主動：`0x100E IDENTIFY_RSP` → `learn_from_identify_rsp()` | `action/net_actions.py:70-77` | ✅ 已接 |
| **落盤** | `housekeep()` → `save()`（節流 2 秒 + tmp→rename 原子替換） | `tasks/bus_decode.py:182-186` | ✅ 已接 |
| **掃描** | `0x100D IDENTIFY_REQ {reply_cid}` → `0x100E {cid, slave_id, ip}` | `action/net_actions.py` `on_identify_req` | ✅ **接收端有**；❌ **發起端只有 PC 測試腳本**（`test/protocol/night_run/scan.py`）。★ 只**點名**，`reply_cid` 不改方向（2026-10 修正）|
| **方向** | `0x1016 SET_MASTER {master_cid}` → `bus.master_cid` | `action/net_actions.py` `on_set_master` | ✅ 有；**唯一的方向寫入者**，落盤 `@node.master_cid`（原本只存記憶體，P2 已修）|
| **自己的位址** | `bus.cid`（來自 config `System.cID`） | `lib/sys/ConfigManager.py` `ensure_cID()` | ✅ 有 |
| **晶片開關** | `0x1008 WIFI_CTRL` / `0x1012 NET_START`（lan/wifi/ap/espnow）/ `0x1014 GET_IP` | `action/net_actions.py` | ✅ 有 |
| **ESP-NOW** | `0x1301 NOW_INIT` / `0x1302 NOW_SEND_HB` / `0x1303 NOW_STATS` | `action/now_actions.py` | ✅ 有 |
| **ESP-NOW 射頻** | `NowBus.add_peer / has_peer / peers / send / write_to / discover` | `lib/sys/now_bus.py` | ✅ 有；❌ **`discover()` 零呼叫者** → 射頻 peer 表**永遠只有廣播位址** |
| **路由** | `SignalRouter.gate()` → `V_OK/V_EXECUTE/V_FORWARD/V_BOTH/V_DROP` | `lib/sys/signal_router.py` | ✅ 完整 |
| **持久化** | btree 迷你 DB（`secrets.db`）+ **`ConfigManager` KV 門面** | `lib/sys/ConfigManager.py` | ✅ **P1 剛完成**（見 §6） |

### ★★ 最關鍵的發現：`PeerRegistry` 是一本**只記不翻的帳簿**

它的查詢 API **全部零呼叫者**（grep 實證）：

| API | 設計用途 | 呼叫者 |
|---|---|---|
| `snapshot()` | 「給 STATUS/UI 用」 | **0** |
| `with_mac()` | 「可定向送出的 peer」 | **0** |
| `by_cid()` | 用 cid 反查 | **0** |
| `age_ms()` | 新鮮度 | **0** |
| `forget()` / `clear()` | 刪除 | **0** |
| `knows()` | 熱路徑去重 | ✅ 1（`bus_decode` 自己） |

**資料是對的、機制是完整的，就是沒有消費者** —— 跟 `NowBus.discover()` 一樣。
**遙控器頁將是它的第一個消費者。**

---

## 5. 缺的零件（本計劃要補的）

| # | 缺什麼 | 影響 | 解法 |
|---|---|---|---|
| **1** | **`disp` 沒註冊成 bus 服務** | UI 拿不到指令接口 | `app.py` 的 `App.__init__` 加 `bus.register_service("disp", self.disp)`（**1 行**） |
| **2** | **`master_cid` / 角色不持久化** | 每次開機要重綁 | 寫進 btree（`@node.*`）（§6） |
| **3** | **掃描沒有發起端** | 裝置自己不能掃 | UI 按鈕 → `make_cmd(0x100D, {"reply_cid": bus.cid})` → 發射 |
| **4** | **射頻配對從未建立** | unicast 送不出去（`ESP_ERR_NOT_FOUND(-12393)`） | ✅ **P6 已完成**：`NowBus.poll()` 收幀即 `learn_peer(peer)`（**對稱**：兩端各自學），另加硬體 peer 表上限（§9.7） |
| **5** | **沒有 `ESP-NOW 關` 指令** | 只能開不能關（UI 開關是單向的） | ✅ **P6 已完成**：`0x1301 NOW_INIT` 加 `action`（0=查詢／1=開／2=關；**不給參數 = 0 = 查詢**），成為 ESP-NOW 生命週期的唯一入口。**順帶修掉一個舊洞**：`0x1301` 對「服務在、但已斷線」只印 `already initialized` 就返回 → 關掉之後再也開不回來（§9.8）。<br>⚠️ **2026-09 整併**：原 `0x1304 NOW_CTRL` 已廢除（只活了一晚，無真機驗證、零外部呼叫者），能力併入 `0x1301`；`0x1012 NET_START{iface_type:3}` 也改走同一個 `_now_on()`——原本三套啟動邏輯、其中兩套是壞的 |
| **6** | **沒有「發送任務」** | 廣播/定向的判斷散在呼叫端 | 抽一個統一發射出口（§2.6/§2.7）—— **可延後** |
| **7** | ★★ **`0x1002 SLAVE_ANNOUNCE` 沒有發送端、也沒有接收端** | **ESP-NOW 上「被發現」的唯一途徑是空的** → 節點清單永遠空的 | ✅ **P6 已完成**：接收端 `net_actions.on_slave_announce`（+ `PeerRegistry.learn_from_announce`）；發送端在測試對端 `ports/S3/ESP32-S3-Test_Peer` 的 `AnnounceTask`（§12） |

### ★★★ 第 7 項是整個計劃真正的破口（2026-09 補記）

前面第 3、4 項看起來像「掃描沒人發起」「配對沒建立」，但修好它們**仍然發現不了任何東西**，
原因是 ESP-NOW 的一個結構性事實：

> **ESP-NOW 的位址是 MAC（6 bytes），而 MAC 不是一個可枚舉的數值空間。**

`0x100D IDENTIFY_REQ` 的設計是「**逐 address 掃描**」（`test/protocol/night_run/scan.py`
就是掃 cid `0x0000`–`0xFFFE`）—— 那在 **UART / TCP 上是可行的**（位址是連續整數）。
在 ESP-NOW 上**無從發起**：你不知道要掃誰，而且沒有任何「列舉鄰居」的 API。

所以被動發現（`PeerRegistry.learn_from_frame`）是**唯一**的路，而它需要一個前提：
**對端得先開口講話**。原本的鏈條是：

```
對端開機 → ✗ 沒有任何任務會主動送東西 → 面板永遠學不到它的 MAC
        → 面板送不出 unicast → 不可能定向查詢  ← 雞生蛋
```

`send_heartbeat`（0x1302）存在但**零呼叫者**；`0x1002 SLAVE_ANNOUNCE` schema 有、handler 沒有。
→ 這才是「還欠缺什麼」的答案。修法就是第 7 項：**兩端各補一半**。

**廣播 vs 定向的判準（本計劃定案）**
| 用途 | 位址 | 理由 |
|---|---|---|
| **公告**（`0x1002`，單向、不回覆） | **廣播** | 我還不認識任何人，只能對所有人講；而且**不回覆** → 不會有 N 個對端同時回話的風暴 |
| **發現探測**（`0x100D`，要回覆） | **廣播** | 同上「不認識任何人」；回覆走 `NowBus.write()`（＝射頻層回給**剛剛講話的那個人**），所以是**單播**回覆，不是廣播回覆 |
| **其餘一切**（`0x3101/0x3107/0x1016`…） | **定向** | 已經認識對方了；廣播會讓每個對端都執行一次（例如全部一起改模式） |

⚠️ 「廣播不回」是對的，但**不要**推論成「廣播一定沒回覆」——`0x100D` 是廣播出去、
**單播**回來。回覆位址不由 NC4 header 的 `addr` 決定（ESP-NOW 上 `ctx["send"]` = `NowBus.write`
= `_last_src_mac`），這件事很容易誤判，見 §9.3。

---

## 6. 持久化：btree / `ConfigManager` KV 門面（**P1 已完成**）

### 為什麼用 btree

`btree`（MicroPython 內建）已經在專案裡（`ConfigManager` 用它開 `secrets.db`，目前只存 Wi-Fi 憑證）。
它的價值正是本計劃要的：**逐 key 更新**（改一個節點不必重寫整個檔）＋ 寫壞有 rollback 保護。

### 門面（P1 已完成，`lib/sys/ConfigManager.py`）

```python
cfg_manager.kv_set(key, obj)        # 寫（自動 JSON 編碼）
cfg_manager.kv_get(key, default)    # 讀（自動解碼；不存在/壞掉回 default，不 raise）
cfg_manager.kv_del(key)             # 刪（回 True/False）
cfg_manager.kv_keys(prefix)         # 列 key（btree 有序，前綴掃描；回傳不含 '@'）
cfg_manager.kv_flush()              # 落盤（★ 寫入後不會自動落盤）
```

### 節點狀態的 API（**P2 已完成**，`lib/sys/ConfigManager.py`）

```python
cfg_manager.node_state()    # 目前狀態快照 → dict（給 UI / 持久化寫）
cfg_manager.publish_node()  # 推上 bus.shared["node"]
cfg_manager.load_node()     # btree → bus（開機一次，load_setup 尾端呼叫）
cfg_manager.save_node()     # bus → btree + flush（★ 只在明確動作時呼叫）
cfg_manager.clear_node()    # 解除綁定：回預設 + 刪 key
```

`bus.shared["node"]` 的形狀：

```python
{"cid": 1, "mac": "A0B1C2D3E4F5", "hostname": "s3-cpanel",
 "role": "master", "master_cid": 2, "bound": True,
 "targets": [{"cid": 2, "name": "執行A"}, ...]}
```

**執行期真相**在 `bus` 上（`sys_bus.py` 新增）：`bus.role` / `bus.master_cid`（原有）/ `bus.targets`
**持久化**：`@node.role` / `@node.master_cid` / `@node.targets`（逐 key）

> ✅ **2026-10 實作完成**：`@node.master_mac`（新增）、`pair_claimed`（記憶體 1 bit）、
> `0x1016{0xFFFF}` = 解除、值沒變不寫 flash、`NowBus.write()` 依 `addr` 解析、
> `CircuitBus.inject()`、UI 改走 vBus、cid 撞號偵測、`NowBus.init()` AP 修正。
> 完整內容 → **`todo/05_node_pairing.md`**；變更摘要 → `doc/03_notes/01_changelog.md` §31。

**誰會寫它**：
- `on_set_master`（`0x1016`，**方向確認**）→ 設定 + `save_node()` —— **唯一會改方向的地方**，也是唯一為它寫 flash 的地方
- `on_identify_req`（`0x100D`，**點名**）→ **不寫**（2026-10 修正）。以前它會把 `reply_cid` 寫進 `master_cid`（因為 `_reply()` 沒有目的位址參數），造成「掃描＝無聲搶奪」；現在 `reply_cid` 只作用在那一封 `0x100E`
- 開機 `load_node()` 由 btree 還原 / 解除 `clear_node()` 清空
- UI 綁定/解除 → `exec_cmd(0x1016, ...)` / `cfg_manager.clear_node()`

⚠️ **目標的 MAC 不存** —— 由 peers 表 `by_cid(master_cid)` 查（避免兩份真相）。

### 模式表的 API（**已完成**，`lib/sys/ConfigManager.py`）

**儲存模式（source）回答一個問題：這份模式表是誰給的？**

| 值 | 意思 | 明細存哪 |
|---|---|---|
| `"local"` | 本機 `PixelTask` 載入 `/pixel/modes/*.json` 後覆蓋（**本板是執行端**） | **不存 btree** —— 事實來源是那些 json 檔，每次開機必重載 |
| `"remote"` | 從對方查詢取得（`0x3102` 清單 + `0x3108` 逐一細節）（**本板是控制端**） | ✅ 存 btree（沒有別的事實來源） |
| `None` | 還沒有任何來源 | — |

```python
cfg_manager.mode_source()                 # → "local" / "remote" / None
cfg_manager.mode_ids()                    # → [id, ...]
cfg_manager.mode_detail(mid)              # → {"name": ...}
cfg_manager.mode_table()                  # → {"source":…, "count":N, "entries":[{id,hex,name},…]}
cfg_manager.publish_modes()               # 推上 bus.shared["mode_table"]
cfg_manager.load_modes()                  # 開機（load_setup 尾端呼叫）
cfg_manager.set_local_modes(modes)        # 來源①：PixelTask 載入後 → source=local（並清掉 remote 資料）
cfg_manager.set_remote_list(ids)          # 來源②：收到 0x3102 → source=remote + 寫 @mode.list
cfg_manager.set_remote_detail(mid, name)  # 來源②：收到 0x3108 → **只寫那一筆**
cfg_manager.clear_modes()
```

**流程（定案）**：

```
① 開機：PixelTask 有跑 → 覆蓋一次（source=local）
        沒跑        → 沒人覆蓋（source 保持 remote / None）
② 指令重新取得：0x3101 → 0x3102（清單）→ set_remote_list()
③ 逐一取得細節：0x3107 → 0x3108（name）→ set_remote_detail()  ← 逐 key 寫入
```

**接線（已完成）**：
- `tasks/pixel_task.py::_init_modes()` 尾端 → `cfg_manager.set_local_modes(modes)`
- `action/pixel_actions.py` 新增接收端 handler：`0x3102`（清單）、`0x3108`（細節）
  ⚠️ 兩者都有 **`_is_local_provider()` 防護**：本板若已有 `pixel_maps`（＝執行端），
  **忽略遠端清單** —— 否則執行端的 UI 會被遠端寫入蓋成錯的清單。

**尚未做的（屬於 P5）**：**發起端** —— 誰去送 `0x3101` / `0x3107`。
需要先定「發射走哪條路」（廣播 or 定向、哪條管子），見 §2.6 / §11 未決 #2。

### 命名空間：`@` = 系統保留

```
系統保留：  @node.role  @node.master_cid  @node.targets  @peer.<slave_id>
既有使用者：Network.wifi.ssid_pw   （config 路徑樣式，大寫開頭）
           wifi_credentials       （network_manager）
```
→ 三者**不會相撞**（已實測）。

### 要搬進去的東西

| Key | 內容 | 現況 |
|---|---|---|
| `@node.role` | `master` / `slave` / `peer` | ✅ **P2 完成** |
| `@node.master_cid` | 目前的目標 | ✅ **P2 完成**（原本只在記憶體） |
| `@node.targets` | **目標清單**（多目標切換用） | ✅ **P2 完成**（結構待 P5 定案） |
| `@peer.<slave_id>` | 一節點一筆（含配對欄位 `paired` / `role` / `paired_at`） | ✅ **P4 完成**（一節點一 key，逐筆寫入） |
| `System.cID` | 自己的 NC4 位址 | ✅ 已在 config |
| `Network.wifi.*` | 憑證 | ✅ 已在 secrets.db |

### `peers.json` 搬家：**已完成（P4）**

佈局採 **一節點一 key**：`@peer.<slave_id>` → 一筆（`kv_keys("peer.")` 直接列出清單）。

| 動作 | 行為 |
|---|---|
| `load()` | 讀 btree `@peer.*`；若一筆都沒有而舊 `/peers.json` 還在 → **一次性遷移**（搬完舊檔改名 `.old`，可回復） |
| `save(force)` | **只寫有變更的那幾筆**（`_dirty_sids`）＋ 刪除被移除的（`_removed_sids`）→ `kv_flush()` |
| `forget(sid)` | 記憶體移除 + 標記待刪 → 下次 save 刪掉 btree 那一筆 |
| `clear()` | 全部標記待刪 |
| 取不到 KV 門面時 | **退化為「不持久化」**，學習與查詢照常（`_kv()` 回 None） |

**為什麼延遲 import**：`peer_registry` 宣告「CPython 可離線測」，而 `ConfigManager`
在 module 層就會開 btree 檔（import 有副作用）→ 所以 `_kv()` 才做 lazy import。

**舊寫法（tmp + `os.rename` 原子替換）已移除** —— btree 本身就有 rollback 保護。

> ⚠️ **不要移除 `PeerRegistry`** —— 它是「定向」的資料層（`cid` 給協議層、`mac` 給射頻層）。
> 沒有它，定向查詢／定向發射都做不到。它現在沒人讀，是因為**定向那一半還沒建**（P5）。

---

## 7. 遙控器頁（P5，**已完成**：`ui/lvgl/page/remote.py`）

```
┌───────────────────────────────────────────────────┐
│ 遙控器                                            │
├──────────────────────┬────────────────────────────┤
│ 節點 (2)             │ 身份                        │
│ ┌──────────────────┐ │ cID 0x0001  ch6             │
│ │  ----- 445566    │ │ MAC A0B1C2D3E4F5            │
│ │* 0x0002 D3E4F5   │ │ 目標 0x0002 (2/2)           │
│ └──────────────────┘ ├────────────────────────────┤
│  (lv.list 可捲動)     │ 模式 remote (3)             │
│                      │  0x0002 跑馬燈              │
│                      │  0x0003 呼吸燈              │
├──────────────────────┴────────────────────────────┤
│ [掃描][綁定][解除][取清單][取細節]                  │
│ Wi-Fi [ ]   ESP-NOW [ ]                            │
└───────────────────────────────────────────────────┘
  `*` = 最近見過（age<10s）；`-----` = 還沒有 cid（等 identify_rsp）
```

**「選中 = 操作對象」** —— 綁定／解除／查詢都作用在清單上選中的那個節點。

| 動作 | 實作 |
|---|---|
| 掃描 | `make_cmd(0x100D, {"reply_cid": 我的cid})` → **廣播**（回覆自動被 PeerRegistry 登記） |
| **綁定** | ① `add_peer(mac)`（配對＝建立通道）② **發射** `SET_MASTER(我的cid)` ③ 本地 `@node.targets` 加入 + active |
| 解除 | 從 `targets` 移除選中的；選中的不是目標 → 退路解除 active；清空後 `role=None` |
| 取清單 | `make_cmd(0x3101, {"mode_type":0})` → **定向**（unicast 給選中節點） |
| 取細節 | 對模式表每個 id 送 `0x3107` → 定向 |
| Wi-Fi / ESP-NOW | `exec_cmd(0x1008)` / `exec_cmd(0x1301)`；**開關狀態由 config／服務單向同步**（UI 只是顯示器） |

### ★★ 實作時發現的語意修正：`SET_MASTER` 的方向

`0x1016 SET_MASTER` 有**兩種用法**，方向相反：

| 誰做 | payload | 效果 |
|---|---|---|
| **我發** | **我的 cid** | 告訴對方「你的 master 是我」→ 對方 `master_cid = 我` |
| **我收** | 對方 cid | 我把對方當上級 → 我 `master_cid = 對方`、`role = "slave"` |

**面板是遙控器 → 綁定時走「我發」的那一邊。**（原本誤用「我收」的語意，被離線冒煙測試抓出。）

### ★ `master_cid` 與 `targets` 是兩件不同的事

| 變數 | 語意 | 面板的情況 |
|---|---|---|
| `bus.master_cid` | **我的上級**（回覆定址） | 通常 `0xFFFF`（沒有上級） |
| `bus.targets` | **我控制誰**（清單，恰一個 `active`） | 綁定動作寫這裡 |

→ 綁定目標**不會**動 `master_cid`（已實測確認）。

### 已驗證（離線冒煙測試，LVGL 自動 stub）

| 測項 | 結果 |
|---|---|
| `build()` 建立整個畫面 | ✅ nav 註冊 8 項 |
| `update()`／`on_enc`／`on_confirm`／`on_exit` | ✅ 無例外 |
| 掃描 | ✅ 廣播、`addr=0xFFFF` |
| 綁定（完整鏈 UI→exec_cmd→handler→btree） | ✅ `master_cid`／`role`／`targets` 都對、**斷電重開仍在** |
| 多目標 + active 切換 | ✅ 綁兩個、active 指最新；解除後自動轉移 |
| 定向查詢 | ✅ `0x3101` `addr=0x0002` 走 unicast |
| 晶片開關 | ✅ Wi-Fi 走 `0x1008`；ESP-NOW 只能開（關待 P6） |
| 服務缺席（無 disp／NowBus） | ✅ 不炸，只印訊息 |
| 開關狀態同步 | ✅（原本漏了，冒煙測試抓出後已修） |

---

## 8. 分階段計劃

| 階段 | 內容 | 驗收 | 狀態 |
|---|---|---|---|
| **P0** | `Dispatcher` 三出入口（`exec_cmd` / `make_cmd`）＋ `SchemaCodec.encode` u16/u32 超界保護 | 離線三關：`dispatch` 逐字不變、`exec_cmd` 與之觀測一致、`make_cmd`→解析→dispatch 端到端 | ✅ **完成** |
| **P1** | `ConfigManager` KV 門面（btree） | 六項實測：set/get、命名空間、`kv_keys` 前綴、`kv_del`、flush 後重開、壞 JSON 容錯 | ✅ **完成** |
| **P2** | 身份／角色／目標 → btree（`@node.*`）＋ `bus.shared["node"]` 執行期鏡像 | 寫入後斷電重開仍讀得到；UI 能讀到 `node` | ✅ **完成** |
| **P3** | `bus.register_service("disp", app.disp)` | 頁面能 `bus.get_service("disp").exec_cmd(...)` | ✅ **完成** |
| **P4** | `PeerRegistry` 搬家 → `@peer.<sid>`（只改 `load` / `save`） | 學習 → 落盤 → 重開 → 清單一致；`/peers.json` 不再產生 | ✅ **完成** |
| **P5** | **遙控器頁**（`ui/lvgl/page/remote.py`） | 掃描 / 節點清單 / 多目標綁定 / 模式查詢 / 晶片開關 | ✅ **完成**（離線冒煙測試） |
| **P6** | ESP-NOW **關閉**指令（`add_peer` 配對已在 P5 隨綁定做掉） | 能關掉 ESP-NOW | ⏳ 只剩關閉指令 |
| **P7** | 抽「發送任務」（廣播/定向的判斷集中化） | 呼叫端不再自己選廣播/定向 | ⏳ |

### P5 遙控器頁（草案）

版面（320×240，新增 `ui/lvgl/page/remote.py`）：

```
┌───────────────────────────────────────────────────┐
│ 遙控器                        [掃描]              │
├──────────────────────┬────────────────────────────┤
│ 節點 (3)             │ 我的身份                    │
│ ┌──────────────────┐ │  cID   0001                 │
│ │● 0001 s3-cpanel  │ │  MAC   A0:B1:C2:D3:E4:F5    │
│ │  0x0002          │ │  IP    192.168.1.5          │
│ │  0x0003          │ │  通道  ESP-NOW ch6          │
│ └──────────────────┘ │                             │
│  (lv.list 可捲動)     │ 目標 (master)               │
│                      │  0x0002  192.168.1.9        │
│                      │  [綁定] [解除] [忘記]        │
└──────────────────────┴────────────────────────────┘
  ●=最近見過(age<10s)  ○=舊紀錄
```

動作 → 指令對應（**全部既有指令，零新 handler**）：

| UI 動作 | 實作 |
|---|---|
| 掃描 | `make_cmd(0x100D, {"reply_cid": bus.cid})` → 發射（回覆自動登記） |
| 節點清單 | 讀 `bus.shared["peers"]`（或搬家後 `kv_keys("peer.")`） |
| 綁定方向 | `exec_cmd(0x1016, {"master_cid": cid})` ＋ 寫 `@node.master_cid` |
| 解除方向 | `exec_cmd(0x1016, {"master_cid": 0xFFFF})` ＋ 清 `@node.*` |
| 忘記節點 | `peers.forget(sid)` ＋ 落盤 |
| Wi-Fi 開關 | `exec_cmd(0x1008, {"wifi_enable": 0/1})` |
| ESP-NOW 開 | `exec_cmd(0x1301, {})` |
| ESP-NOW 關 | **缺指令**（P6 補） |
| ESP-NOW 狀態 | `exec_cmd(0x1303, {})` → 讀回 |

★ **P5 的取捨**：先讓 UI 直接 `make_cmd` + 呼叫 `NowBus.broadcast()`（**與現有 task 一致**），
等 P7 抽出「發送任務」再改走 Router。理由：先讓 UI 能用，不要一次動兩件事。

---

## 9. 關鍵事實與踩坑（★ 給未來的自己）

### 9.1 Router：`self` 才是被查的來源，`vbus` 永遠不被查

```python
# signal_router.py:278-279（def name_of 在第 267 行）
def name_of(self, bus_obj):
    if is_local_bus(bus_obj):     # io is None → vBus
        return SELF               # ★ 回 "self"，不是 "vbus"
```

| 寫法 | 效果 |
|---|---|
| `{"in":"self","out":["self"]}` | ✅ 本地執行 |
| `{"in":"self","out":["now"]}` | ✅ 從 ESP-NOW 發射（V2 面板現在就是這個） |
| `{"in":"self","out":["self","now"]}` | ✅ 兩者都做 |
| `{"in":"vbus","out":["now"]}` | ❌ **什麼都不會發生**（vbus 不是被查的來源） |
| `out: ["vbus"]` | ❌ **被無視**（它不是出口） |

### 9.2 `config.json` 的 `Router` 區塊是 **Router 自己寫回去的**

```python
# tasks/bus_decode.py:132-133
bus.shared["Router"] = r.snapshot()
cfg_manager.save_from_bus(update_key="Router")
```
→ **config 是呈現，Router 的記憶體狀態才是真相。** 不要以為手改 config 就一定生效
（`finalize_router()` 會在開機與任務開始各結算一次並寫回）。

### 9.3 ★ `NowBus.write()` 是**回覆語意**，不能改成廣播

```python
# now_bus.py:129-132
def write(self, data):
    if self._last_src_mac is None:
        return False                          # 沒人 unicast 給過我們 → 丟棄
    return self.send(self._last_src_mac, data)   # 單播給「最後一個來源」
```
`handle_stream` 把它當 `ctx["send"]` 傳給 handler（**回覆路徑**）→ **必須保持單播**。

⚠️ **但這意味著：Router 轉發到 `now` 時會變成 unicast**（`signal_router.py:803` 只認 `write`）
→ 若 `_last_src_mac` 是 `None` 就**直接丟棄**。
→ **所以「面板自己發射」目前不能靠 Router 的出口**（這正是 P7「發送任務」要解的問題）。
→ 若要走 Router，需要讓管子自己宣告轉發方式（例：`NowBus.forward = broadcast`），
   或確認 `_last_src_mac` 一定存在。**動之前先讀 `doc/03_notes/18_...md` §5.1 末段。**

### 9.4 `SchemaCodec.encode` 的超界行為（P0 已修）

| 型別 | 改前 | 改後 |
|---|---|---|
| `u8` | `& 0xFF`（wrap，長度正確） | **不變**（維持現狀） |
| `u16` / `u32` | ❌ `struct.pack` 超界 raise → per-field `except` 吞掉 → **整欄消失、payload 錯位** | ✅ 夾住（長度正確） |
| `i16` / `i32` | 同上（schema 目前無此型別欄位） | ✅ 夾住 |

實測：改前 **216 組超界測試全部長度短少**；改後 0 組。合法值 472 筆逐 byte 不變。
★ schema 裡唯一「會被動態計算」的 u16 欄位是 **`MODE_SET.start_delay_ms`**。

### 9.5 已知死碼（本計劃不動，但要知道）

| 死碼 | 位置 |
|---|---|
| `_pca_actions` | `ui/lvgl/page/pca9685.py`（寫了沒人消費） |
| `_ex_ic_slot` / `_ex_ic_pending` | `tasks/control_panel.py`（只讀沒人寫） |
| `_vbtn1_event` | 兩個生產者、零讀者 |
| `_wtt_periodic_status` | 永遠是 0 |
| `PixelControlPanelTask._broadcast_mode_set()` | 孤兒（UI 直打 espnow 繞過） |
| `ui/lvgl/page/pixel_controller.py:_espnow_send_mid()` | UI **直接打硬體**（唯一一處，應收掉 → 見 P5/P7） |

### 9.6 ★ `load_setup()` 有一條「提早 return」會跳過身份建立

```python
# ConfigManager.load_setup() 開頭
if self.path not in os.listdir():        # 沒有 config.json（全新裝置）
    self.save_from_bus()
    return                               # ← ★ 原本這裡直接返回
    ...（後面才是 ensure_cID / load_node）
```
→ **首次開機時 `bus.cid` 會停在 0xFFFF、`bus.shared["node"]` 整個不存在。**
（P2 已修：這條路徑也呼叫 `ensure_cID()` + `load_node()`。）

### 9.7 ★★ 收幀時必須登記來源 MAC，而且要設上限

**為什麼必須登記**（`NowBus.poll()`）：
ESP-NOW 的 `esp_now_send()` **不接受沒註冊過的 peer**，會回 `ESP_ERR_NOT_FOUND(-12393)`
（`test/protocol/espnow_send.py` 有紀錄）。而「知道對方位址」的唯一時機，
就是它的幀到達射頻層的那一刻 —— MAC 無法從 cid 反推（面板的 `cID` 是手寫 `0001`，
不是 MAC 推導的），也無法枚舉。
→ 所以 `poll()` 每收到一個新來源就 `learn_peer(peer)`，之後 `write()`（單播回覆）才送得出去。
這是**對稱**的：兩端各自在收幀時學，沒有「誰先配對」的順序問題。

**為什麼要設上限**：ESP-NOW 的 peer 表是**硬體資源**（ESP-IDF 上限 20，加密時 6），
而 `poll()` 的輸入是**任意射頻**——附近別人的裝置、雜訊都會消耗它。
填滿之後真正要綁定的目標反而進不來，而且**沒有任何錯誤會浮上來**。
| 方法 | 誰決定 | 上限 |
|---|---|---|
| `add_peer(mac)` | 使用者的意圖（UI 綁定 / 定向發射前補通道） | **不設限** |
| `learn_peer(mac)` | 射頻上剛好有人講話（`poll()` 呼叫） | `Network.ESP_now.max_learn_peers`，預設 **16** |

滿了只印一次警告，不影響既有 peer，也不阻擋明確綁定。

### 9.8 ★ `0x1301 NOW_INIT` 無法「重新開機」已斷線的 ESP-NOW

`on_now_init` 的邏輯是「服務不存在才建」：

```python
now = bus.get_service("NowBus")
if now is None:
    ... 建立 + init ...
else:
    print("[NOW] already initialized")     # ← 服務在就什麼都不做
```

但 ESP-NOW 有**兩種**「不在跑」的狀態，這個判斷把他們混成同一種：

| 狀態 | 服務 | `connected` | 舊碼的反應 |
|---|---|---|---|
| 從來沒建過 | 不存在 | — | 建立 ✔ |
| **關掉之後** | **存在** | **False** | **什麼都不做** ✘ |

→ 所以「關掉 → 想再開」在舊碼上是**靜默失敗**（只印一行 already initialized）。
`0x1301 NOW_INIT` 的 `_now_on()` 分開處理這兩種狀態（存在但斷線 → **就地重開**）。

★ **語意定案（2026-09）：不給參數 = 0 = 查詢。** 規則只有一句，沒有例外
（`0xFF` 也沒有特殊意義，就是一般的未知值）。`0x1301` 原本是空 payload
（只有開），所以舊客戶端送空 payload 從「開」變成「查詢」—— **刻意的取捨**：
換到「不給參數不會改變裝置狀態」與「不會再有送空 payload 意外啟動射頻」。
要開就明確送 `1`。

⚠️ **本地呼叫端的陷阱**（`test/protocol/test_now_1301_action.py` 釘住）：
`SchemaCodec.encode()` 對**缺席欄位補 0**，所以「本地程式碼用 `encode` 送空
dict」wire 上是 `action=0`（查詢）。兩條路剛好都落到查詢，但**一旦有人把
「不給參數」改回「開」，本地這條路會靜默跟不上**。→ 本地要開就必須**明確**
寫 `action=1`／`2`。

**關掉的狀態刻意設計成「服務還在、`connected=False`」**，不是把服務拔掉：
- `NowTask.loop()` 靠 `connected` 停 poll
- `NowBus.send()` 靠 `connected` 直接回 `False`
- UI `_tx()` 拿到 `False` → 顯示送不出去，而不是靜默

所有既有呼叫端本來就是容錯寫法，拔掉服務反而會讓 `get_service("NowBus") is None`
的分支到處跑。**UI 判斷開關狀態必須用 `connected`，不能用「服務存在」**
（`remote.py:_sync_now_switch()` 就是踩到這個才修）。

**關的順序不能顛倒**：① 先 `bus_sources.remove(now)` → ② 才 `now.deinit()`。
反過來的話，`bus_decode._drain()` 的下一輪會拿到已 dead 的 bus
（`rx_hub` 還在、`_esp` 已是 `None`）而在射頻層炸開。

---

## 10. 環境操作備忘（上板 / REPL）

### 10.1 V2 面板的設定紅線

| 設定 | 值 | 為什麼 |
|---|---|---|
| `System.watchdog.auto_rearm_ms` | **必須 0** | `enable:0` 且 `auto_rearm>0` 時，開機後連續 N ms 沒收到指令 → 自動存 `enable=1` + reset。**面板是沒人對它發指令的裝置**（只往外廣播），silence 是常態 |
| `TFT.rotation` | **必須 0** | `ui/lvgl/lvgl_init.py` 自己送 MADCTL(0x60) 轉橫屏，改 1 會 double-rotate |
| `System.cID` | `"0001"` | 未指派時 `bus.cid = 0xFFFF` = 廣播 → 每一站都會重複執行廣播幀 |
| `Network.ESP_now.enable` | 1，`channel` 兩端一致 | 面板的生命線；少了它 UI 有畫面但送不出指令 |

### 10.2 進 REPL / 中止程式

- `main.py` 是 `if __name__ == "__main__"` → 開機自動跑 → `tm.runner_loop(0)` 阻塞 core0
- **Ctrl-C 要按兩次**：第一次觸發 `auto_disable_on_interrupt()`（存 `watchdog.enable=0` + **立即重啟一次**），第二次才真的停在 REPL
- ⚠️ **WDT 開著時，停在 REPL 約 8 秒會被硬體 WDT 重置**（沒人餵狗）→ 先關 WDT 再進 REPL
- 參考既有工具：`temp/usb_repl.py`、`temp/usb_disable_wdt.py`（改 `PORT` 即可）

### 10.3 `mpremote` 的坑

- 板上 app 一直印 log 時，**mpremote 進不了 raw REPL**（`could not enter raw repl`）
- 它失敗後可能把板子**留在 raw REPL**（表面看起來像當掉、完全沒輸出）
  → 用 `Ctrl-B`（`\x02`）退出，`Ctrl-D` soft reboot
- 上傳檔案前先在 REPL 停住 app

### 10.4 `ConfigManager` 在 **module 層** 就有副作用

```python
# ConfigManager.py 檔尾
cfg_manager = ConfigManager(bus)
cfg_manager.load_setup()      # ← import 就會建 secrets.db + 讀 config.json + ensure_cID()
```
→ 離線測試時請在暫存目錄跑，否則會在 CWD 產生 `secrets.db` / `config.json`。

---

## 11. 未決事項

| # | 問題 | 備註 |
|---|---|---|
| 1 | **`peers.json` 搬家時機** | ✅ **P4 已完成**（btree `@peer.<sid>`，舊檔改名 `.old`） |
| 2 | **「發送任務」的歸屬** | 抽成新模組？或放進既有系列（`lib/sys/`）？與 `17_tx_render_center_plan.md` 的 TxCenter 是否同一個東西？ |
| 3 | **`NowBus` 的轉發語意** | 要不要加 `forward()`（廣播）讓 Router 可用？（§9.3） |
| 4 | **多目標的極端場景** | 「管理一堆目標 + 不斷切換發射」→ `@node.targets` 的資料形狀？同時多目標（fan-out）要不要？ |
| 5 | **`status.json` 是否還需要** | 若 `@node.*` + `@peer.*` 都在 btree，就不需要第三份檔案（除非要「一次讀完的快照」） |
| 6 | **既有技術債** | 亮度三個寫入者 / `_espnow_send_mid` / 四處死碼 —— 何時收 |
| 7 | ★ **真實執行端要不要也公告？** | `ports/S3/ESP32-S3-1_18` 的 `Network.ESP_now.enable` 目前是 **0**（完全不收 ESP-NOW，連掃描都掃不到它）。改成 1 之後，要不要讓它也跑 `AnnounceTask`？**公告＝任何人都看得到你**（未加密的 ESP-NOW 廣播），這是產品決策不是技術決策 |
| 8 | **`0x1002` 要不要帶 `cid`？** | 目前 payload 是 `slave_id + pixel_count + hw_version`（schema 已定）。不帶 cid → 收到公告只學到 MAC，還要再敲一次 `0x100D` 才知道 cid。加 `cid` 可以省一趟，但要動 schema（**已定義的指令改 payload 是破壞性變更**） |
| 9 | **公告週期** | 測試對端用 10 秒。真實裝置要多長？（太短 = 無線電一直講話；太長 = 面板開機後等很久才看到它）。或改成「只在開機公告一次 + 被敲門才回」 |
| 10 | **`Router.enable=0` 時仍然寫回 `config.json`** | `finalize_router()` 不看 `enable`。所以任何 port 第一次開機，`Router.routes` 都會被補成 5 條。是預期行為（§9.2），但值得在每個 port 的 README 註明 |
| 11 | **`ESP_now.enable` 的角色是「授權」還是「狀態」？** | 目前定為**授權**（config 說「這台允許用 ESP-NOW」），執行期開關是 `0x1301{action}`。所以關掉不改 config，重開機回到 config 的狀態。若希望「關掉就記住」，就要改成寫回 config |
| 12 | **`test/protocol/router_selftest.py` 目前是壞的（既有問題）** | §1 就掛：`build()` 沒註冊介面，route 進 `_pending` 而非 `by_in` → `len(r.by_in)==0`。測試檔沒跟上 pending/autofill 機制。與本計劃無關，但會讓「跑一下 selftest」失去意義 |

---

## 12. 測試對端：`ports/S3/ESP32-S3-Test_Peer`（P6 新增）

### 為什麼需要
遙控器頁的三件事（看得到節點 / 綁定 / 查詢）都**需要兩個節點**。
用真板測的代價是：兩台板 + 兩條 USB + 對端得接 WS2812 與 SD（`/pixel/modes/*.json`）
才有模式池，而且對端改壞了還要先修對端 —— 測試與被測互相糾纏。

本 port 把對端變成**可拋棄的**：零硬體、模式池寫死在 `Core_Manager.py`、
改壞直接重上傳，面板一行都不用動。

### 它有什麼
| 能力 | 來源 |
|---|---|
| ESP-NOW 射頻 | `Network.ESP_now = {enable:1, channel:6}`（**必須與面板同頻**） |
| 身份 `cid=0x0002` | `System.cID`（與面板的 `0001` 配對；**留空會被自動填成 MAC 末 4 碼**） |
| 假模式池 3 筆 | `FAKE_MODES` → `bus.shared["pixel_maps"]`（混 LED 組 `0x00xx` 與 SERVO 組 `0x02xx`，可測 `mode_type` 過濾） |
| 開機 + 每 10 秒公告 | `AnnounceTask` → `disp.make_cmd(0x1002)` → `NowBus.broadcast()` |
| 回應查詢 | 既有 handler 全量註冊（`App()` → `register_all`），沒有為測試改一行生產程式碼 |

任務集只有 5 個：`network / now / log`(L0) + `announce`(L1) + `bus_decode`(L2)。
`pixel / render / stream / dj / audio_player / lvgl / web_ui / cpanel / pixel_cpanel / schedule`
**都不註冊**（沒硬體，或屬於面板角色）。

### ★ `AnnounceTask` 為什麼寫在 port 裡而不是 `slave/tasks/`
- 它是**測試對端的存在理由**，不是生產裝置的共性需求（真實執行端要不要公告是 §11 第 7 項的決策）
- port 的 `Core_Manager.py` 是**唯一**放「這台裝置是什麼」的地方（見 `Control_Panel_V2` 的同一慣例）
- 一旦決定生產裝置也要公告 → 整個 class 原封不動搬進 `slave/tasks/`，改成 `register_task` 一行

### 離線驗證（不用板子，兩支都已通過）
```bash
python -B /tmp/nodetest/peer_smoke.py       # 開機 → 廣播合法 0x1002（欄位逐項比對）
python -B /tmp/nodetest/peer_roundtrip.py   # 收 0x100D → learn_peer(面板 MAC) → 單播回 0x100E
```
`peer_roundtrip.py` 實測輸出（＝整條雙向閉環）：
```
注入 0x100D 探測幀 …
add_peer 呼叫: ['ffffffffffff', 'aabbccddeeff']      ← 廣播位址 + 學會的面板 MAC
送出: to=ffffffffffff cmd=0x1002 addr=0xFFFF          ← 公告（廣播）
      to=aabbccddeeff cmd=0x100E addr=0x0001          ← 回覆（單播回面板）
bus.master_cid = 0x0001                               ← 對端記住了方向
```
> ⚠️ 最後一行是**當時的**輸出。2026-10 修正後 `0x100D` 不再改 `master_cid`
> （`addr=0x0001` 仍會對，因為那來自 `reply_cid`），方向改由 `0x1016` 設定。

