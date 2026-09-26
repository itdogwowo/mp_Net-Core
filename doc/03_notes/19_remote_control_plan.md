# 遙控器強化計劃 — 配對 / 綁定 / 晶片開關

> **用途**：把「強化遙控器」這件事的**共識、現況盤點、分階段做法、踩過的地雷**集中一處。
> **本檔自足**：不依賴任何對話上下文，單獨看就能接手。
> **狀態**：PLANNED（P0 / P1 已完成並驗證）
> **最後更新**：2026-09-26
> **相關文件**：
> - `doc/03_notes/18_pixel_panel_control_path.md` — 面板 pixel 控制路徑（定向查詢的來源）
> - `doc/03_notes/17_tx_render_center_plan.md` — 統一發射中心（本計劃的上游）
> - `doc/02_guides/15_schedule.md` — schedule（vBus 注入路徑）
> - `doc/02_guides/16_signal_router.md` — Router（路由政策）
> - `ports/S3/ESP32-S3-Control_Panel_V2/README.md` — 面板角色與設定備註
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
| **掃描** | `0x100D IDENTIFY_REQ {reply_addr}` → `0x100E {cid, slave_id, ip}` | `action/net_actions.py:83-93` | ✅ **接收端有**；❌ **發起端只有 PC 測試腳本**（`test/protocol/night_run/scan.py`） |
| **方向** | `0x1016 SET_MASTER {master_cid}` → `bus.master_cid` | `action/net_actions.py:176-181` | ✅ 有；⚠️ **只存記憶體，重開機丟**（`sys_bus.py:23`） |
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
| **3** | **掃描沒有發起端** | 裝置自己不能掃 | UI 按鈕 → `make_cmd(0x100D, {"reply_addr": bus.cid})` → 發射 |
| **4** | **射頻配對從未建立** | unicast 送不出去 | 掃描完對 `peer.mac` 做 `add_peer()`（**兩端各自做 = 對稱**） |
| **5** | **沒有 `ESP-NOW 關` 指令** | 只能開不能關 | `NowBus.deinit()` 存在，需要一個指令包裝 |
| **6** | **沒有「發送任務」** | 廣播/定向的判斷散在呼叫端 | 抽一個統一發射出口（§2.6/§2.7）—— **可延後** |

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
| `@node.role` | `master` / `slave` / `peer` | ❌ 缺 |
| `@node.master_cid` / `@node.master_mac` | 目前的目標 | ❌ 缺（只在記憶體） |
| `@node.targets` | **目標清單**（多目標切換用） | ❌ 缺 |
| `@peer.<slave_id>` | 一節點一筆（含配對欄位 `paired` / `role` / `paired_at`） | ⚠️ 現在在 `/peers.json` |
| `System.cID` | 自己的 NC4 位址 | ✅ 已在 config |
| `Network.wifi.*` | 憑證 | ✅ 已在 secrets.db |

### `peers.json` 搬家：**佈局選擇**

| 佈局 | key | 取捨 |
|---|---|---|
| 一個 blob | `@peers` → 整個 dict | 最簡單，但改一筆要重寫整包（**沒用到 btree 的價值**） |
| **一節點一 key**（建議） | `@peer.<slave_id>` → 一筆 | ✅ 逐 key 更新；`kv_keys("peer.")` 直接列出清單 |

**搬家的範圍很小**：`PeerRegistry` 只有 `load()`（第 97-122 行）與 `save()`（第 124-156 行）
兩處要改成走 KV 門面，其餘 200 行（學習、查詢、`_publish`）不動。

> ⚠️ **不要移除 `PeerRegistry`** —— 它是「定向」的資料層（`cid` 給協議層、`mac` 給射頻層）。
> 沒有它，定向查詢／定向發射都做不到。它現在沒人讀，是因為**定向那一半還沒建**。

---

## 7. 分階段計劃

| 階段 | 內容 | 驗收 | 狀態 |
|---|---|---|---|
| **P0** | `Dispatcher` 三出入口（`exec_cmd` / `make_cmd`）＋ `SchemaCodec.encode` u16/u32 超界保護 | 離線三關：`dispatch` 逐字不變、`exec_cmd` 與之觀測一致、`make_cmd`→解析→dispatch 端到端 | ✅ **完成** |
| **P1** | `ConfigManager` KV 門面（btree） | 六項實測：set/get、命名空間、`kv_keys` 前綴、`kv_del`、flush 後重開、壞 JSON 容錯 | ✅ **完成** |
| **P2** | 身份／角色／目標 → btree（`@node.*`）＋ `bus.shared["node"]` 執行期鏡像 | 寫入後斷電重開仍讀得到；UI 能讀到 `node` | ⏳ |
| **P3** | `bus.register_service("disp", app.disp)` | 頁面能 `bus.get_service("disp").exec_cmd(...)` | ⏳ |
| **P4** | `PeerRegistry` 搬家 → `@peer.<sid>`（只改 `load` / `save`） | 學習 → 落盤 → 重開 → 清單一致；`/peers.json` 不再產生 | ⏳ |
| **P5** | **遙控器頁**（見 §8） | 掃描得到節點、能綁定方向、晶片開關可切 | ⏳ |
| **P6** | 射頻配對（`add_peer`）＋ ESP-NOW 關閉指令 | 定向 unicast 真的送得出去 | ⏳ |
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
| 掃描 | `make_cmd(0x100D, {"reply_addr": bus.cid})` → 發射（回覆自動登記） |
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

## 8. 關鍵事實與踩坑（★ 給未來的自己）

### 8.1 Router：`self` 才是被查的來源，`vbus` 永遠不被查

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

### 8.2 `config.json` 的 `Router` 區塊是 **Router 自己寫回去的**

```python
# tasks/bus_decode.py:132-133
bus.shared["Router"] = r.snapshot()
cfg_manager.save_from_bus(update_key="Router")
```
→ **config 是呈現，Router 的記憶體狀態才是真相。** 不要以為手改 config 就一定生效
（`finalize_router()` 會在開機與任務開始各結算一次並寫回）。

### 8.3 ★ `NowBus.write()` 是**回覆語意**，不能改成廣播

```python
# now_bus.py:129-132
def write(self, data):
    if self._last_peer is None:
        return False                          # 沒人 unicast 給過我們 → 丟棄
    return self.send(self._last_peer, data)   # 單播給「最後一個來源」
```
`handle_stream` 把它當 `ctx["send"]` 傳給 handler（**回覆路徑**）→ **必須保持單播**。

⚠️ **但這意味著：Router 轉發到 `now` 時會變成 unicast**（`signal_router.py:803` 只認 `write`）
→ 若 `_last_peer` 是 `None` 就**直接丟棄**。
→ **所以「面板自己發射」目前不能靠 Router 的出口**（這正是 P7「發送任務」要解的問題）。
→ 若要走 Router，需要讓管子自己宣告轉發方式（例：`NowBus.forward = broadcast`），
   或確認 `_last_peer` 一定存在。**動之前先讀 `doc/03_notes/18_...md` §5.1 末段。**

### 8.4 `SchemaCodec.encode` 的超界行為（P0 已修）

| 型別 | 改前 | 改後 |
|---|---|---|
| `u8` | `& 0xFF`（wrap，長度正確） | **不變**（維持現狀） |
| `u16` / `u32` | ❌ `struct.pack` 超界 raise → per-field `except` 吞掉 → **整欄消失、payload 錯位** | ✅ 夾住（長度正確） |
| `i16` / `i32` | 同上（schema 目前無此型別欄位） | ✅ 夾住 |

實測：改前 **216 組超界測試全部長度短少**；改後 0 組。合法值 472 筆逐 byte 不變。
★ schema 裡唯一「會被動態計算」的 u16 欄位是 **`MODE_SET.start_delay_ms`**。

### 8.5 已知死碼（本計劃不動，但要知道）

| 死碼 | 位置 |
|---|---|
| `_pca_actions` | `ui/lvgl/page/pca9685.py`（寫了沒人消費） |
| `_ex_ic_slot` / `_ex_ic_pending` | `tasks/control_panel.py`（只讀沒人寫） |
| `_vbtn1_event` | 兩個生產者、零讀者 |
| `_wtt_periodic_status` | 永遠是 0 |
| `PixelControlPanelTask._broadcast_mode_set()` | 孤兒（UI 直打 espnow 繞過） |
| `ui/lvgl/page/pixel_controller.py:_espnow_send_mid()` | UI **直接打硬體**（唯一一處，應收掉 → 見 P5/P7） |

---

## 9. 環境操作備忘（上板 / REPL）

### 9.1 V2 面板的設定紅線

| 設定 | 值 | 為什麼 |
|---|---|---|
| `System.watchdog.auto_rearm_ms` | **必須 0** | `enable:0` 且 `auto_rearm>0` 時，開機後連續 N ms 沒收到指令 → 自動存 `enable=1` + reset。**面板是沒人對它發指令的裝置**（只往外廣播），silence 是常態 |
| `TFT.rotation` | **必須 0** | `ui/lvgl/lvgl_init.py` 自己送 MADCTL(0x60) 轉橫屏，改 1 會 double-rotate |
| `System.cID` | `"0001"` | 未指派時 `bus.cid = 0xFFFF` = 廣播 → 每一站都會重複執行廣播幀 |
| `Network.ESP_now.enable` | 1，`channel` 兩端一致 | 面板的生命線；少了它 UI 有畫面但送不出指令 |

### 9.2 進 REPL / 中止程式

- `main.py` 是 `if __name__ == "__main__"` → 開機自動跑 → `tm.runner_loop(0)` 阻塞 core0
- **Ctrl-C 要按兩次**：第一次觸發 `auto_disable_on_interrupt()`（存 `watchdog.enable=0` + **立即重啟一次**），第二次才真的停在 REPL
- ⚠️ **WDT 開著時，停在 REPL 約 8 秒會被硬體 WDT 重置**（沒人餵狗）→ 先關 WDT 再進 REPL
- 參考既有工具：`temp/usb_repl.py`、`temp/usb_disable_wdt.py`（改 `PORT` 即可）

### 9.3 `mpremote` 的坑

- 板上 app 一直印 log 時，**mpremote 進不了 raw REPL**（`could not enter raw repl`）
- 它失敗後可能把板子**留在 raw REPL**（表面看起來像當掉、完全沒輸出）
  → 用 `Ctrl-B`（`\x02`）退出，`Ctrl-D` soft reboot
- 上傳檔案前先在 REPL 停住 app

### 9.4 `ConfigManager` 在 **module 層** 就有副作用

```python
# ConfigManager.py 檔尾
cfg_manager = ConfigManager(bus)
cfg_manager.load_setup()      # ← import 就會建 secrets.db + 讀 config.json + ensure_cID()
```
→ 離線測試時請在暫存目錄跑，否則會在 CWD 產生 `secrets.db` / `config.json`。

---

## 10. 未決事項

| # | 問題 | 備註 |
|---|---|---|
| 1 | **`peers.json` 搬家時機** | 先做 P5（UI 讀舊檔）再搬？或先搬（P4）再讓 UI 讀新的？ |
| 2 | **「發送任務」的歸屬** | 抽成新模組？或放進既有系列（`lib/sys/`）？與 `17_tx_render_center_plan.md` 的 TxCenter 是否同一個東西？ |
| 3 | **`NowBus` 的轉發語意** | 要不要加 `forward()`（廣播）讓 Router 可用？（§8.3） |
| 4 | **多目標的極端場景** | 「管理一堆目標 + 不斷切換發射」→ `@node.targets` 的資料形狀？同時多目標（fan-out）要不要？ |
| 5 | **`status.json` 是否還需要** | 若 `@node.*` + `@peer.*` 都在 btree，就不需要第三份檔案（除非要「一次讀完的快照」） |
| 6 | **既有技術債** | 亮度三個寫入者 / `_espnow_send_mid` / 四處死碼 —— 何時收 |
