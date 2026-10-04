# 節點發現與配對 流程計劃書（**傳輸層無關**）

> **用途**：定義「Master 廣播收集 → 建表 → 逐一認領」這條流程，以及**兩端各自的發送路徑**與 **UI 的分界線**。
> **它是 NC4 協議層的能力，不是 ESP-NOW 專屬** —— ESP-NOW 只是其中一條管子，而且它多了一個別條管子沒有的東西（MAC）。
> **狀態**：✅ **已實作並完成真機驗證（2026-10，兩塊板）** —— 階段 1~4 全部完成，
> 離線測試 **107/107 PASS**（§11），真機驗證 **§12 全數通過**。
> ⚠️ 開發這條流程時**一定要 hard reset**，不能用 Ctrl-D 軟重開機（§12.5）。
> ⚠️ 本檔保留為**設計依據**：改了相關程式碼請回來對照，不要只看現況。
> **最後更新**：2026-10
> **相關文件**：
> - `doc/03_notes/19_remote_control_plan.md` — §2.3 配對 vs 方向、§2.4 管子抽象、**§2.5 執行 vs 發射＝意圖**、§2.6 廣播 vs 定向、§2.7 發送任務
> - `doc/02_guides/16_signal_router.md` — **§3.4 兩個本機來源（`self`/`vbus`）**、§4.2 `in` 為什麼是純量
> - `todo/06_router_local_paths.md` — 「絕對內部執行」那條路徑（**本計劃不依賴它**）
> - `todo/04_remote_pairing.md` — 配對/解除/衝突政策（鄰居計劃）

---

## 0. 一句話

**這條流程只依賴 `cid`，不依賴 MAC。** Master 負責全部複雜度，Slave 只管兩個值。

```
Master：廣播收集（0x100D）→ 建表 → 逐一認領（0x1016）
Slave ：被點名就回（隨機延遲）→ 被認領就記下 Master
```

**配對完成後，兩端各自都握有對方完整的兩個位址** —— 發送時只是**讀自己那份記錄**，
不需要任何新增的查表層、服務或模組。

---

## 1. 範圍

| | 內容 |
|---|---|
| **做** | 協議層的「發現 + 認領」流程；`0x100D` 追加 `timeout_ms`；**延後發射（抖動）掛在解碼鏈**；`NowBus.write()` 依幀頭 `addr` 解析目的地；vBus 注入點公開化；`NowBus.init()` 介面修正 |
| **不做** | 不改 NC4 幀頭；**不動 Router 政策**（`self`/`vbus` 那件事另見 `todo/06`）；不做 Slave 端清單/狀態機；不做自動重試迴圈；**不把 MAC 提升成跨傳輸層的識別**；不新增 `send_to(peer, frame)` 這種介面 |

---

## 2. 三個層次（★ 全篇核心）

| 層 | 東西 | 大小 | 通用嗎 | 誰用 |
|---|---|---|---|---|
| **協議層（NC4）** | **`cid`** | 2 B | ✅ **全部管子** | 幀頭 `addr`（`proto.py:222`）；**節點身分／定址** |
| **傳輸層（管子）** | `write(frame)` / `broadcast()` | — | ✅ 全部（各自實作）| 把位元組送出去 |
| **射頻層** | **MAC** | 6 B | ❌ **只有 ESP-NOW** | `add_peer(mac)`、`send(mac,…)` |

### 2.1 為什麼身分是 `cid` 而不是 MAC

- **UART / RS485 / WS 的節點沒有 MAC** —— 用 MAC 當身分，這些管子直接不能用
- MAC 無法從 cid 推導（cid 可以手寫，面板就是 `"0001"`）；cid 只有 16 bits，也塞不下 48 bits 的 MAC
- 所以：**`cid` = 身分；MAC = 「ESP-NOW 怎麼把位元組送出去」的細節，只活在 `NowBus` 內部**

### 2.2 `cid` 唯一性是**協議層既有前提**

> 共用匯流排（UART/RS485）上兩台同 cid → 定址指令**兩台都會執行** → 那條匯流排**本來就壞了**。

→ 「cid 撞號」是**設定錯誤**，要**偵測並回報**（§7.5），不是用 MAC 去繞過它。

### 2.3 已用真機驗證（`/dev/cu.usbmodem11401`，2026-10）

| 項目 | 值 | 結論 |
|---|---|---|
| `machine.unique_id()` | `24EC4A2CA430` | 與 STA MAC 相同 |
| ESP-NOW STA MAC | `24EC4A2CA430` | **`slave_id` 可直接當 ESP-NOW 的射頻位址** |
| AP MAC | `24EC4A2CA431`（+1）| **與 ESP-NOW 無關** |
| AP-only 時 `espnow.send()` | `ESP_ERR_ESPNOW_IF (-12396)` | **ESP-NOW 綁 STA，不是 AP** |
| `urandom` / `random` | 兩者都有 `getrandbits` | 抖動可直接用內建 |

---

## 3. ★ UI 的分界線（本計劃的核心約束）

### 3.1 UI 只做三件事 —— **兩個出口要分流**

**「這條指令是執行還是發射」由產生指令的人決定**（`doc/03_notes/19` §2.5）。
所以 UI／task／schedule 都要**明確選一個入口**：

| 意圖 | 用哪個 API | 走 Router？ | 會外送嗎 | 典型場合 |
|---|---|---|---|---|
| **① 內部執行** | `app.disp.exec_cmd(cmd, args, ctx)` | ❌ 不經 | ❌ **結構上保證不會** | 面板自己也要跑的指令 |
| **② 可能對外發出** | `vbus.inject(frame)` | ✅ 經 | 依 `{in:"self", out:[…]}` | 掃描、認領、查詢 |
| **③ 顯示狀態** | 讀 `bus.shared` | — | — | 節點清單、身份、模式表 |

**UI 絕不碰**：`NowBus` / `add_peer` / `write_to` / MAC。

> ✅ **零 Router 改動** —— 這就是「兩個本機來源」的正解（`todo/06_router_local_paths.md` 已定案）：
> 不去拆 `self`/`vbus`，而是**在產生者這一側分流**。`exec_cmd` 提供的是**結構上**的保證
> （不經 Router ＝ 不可能被路由出去）。

**現況對照**（`remote.py`）：

| | 現況 | 要改成 |
|---|---|---|
| `_exec()` | ✅ 已經是 `disp.exec_cmd(...)` | **不用改** |
| `_tx()` | ❌ 直接 `now.add_peer` / `write_to` / `broadcast` | → `vbus.inject(frame)` |

### 3.2 ②「可能對外發出」= 注入 vBus（走 Router）

```
UI / task / schedule
      ↓  產生指令（addr = 目標 cid，或 0xFFFF）
   vbus.inject(frame)
      ↓
   BusDecodeTask 解碼（來源 = self，is_local_bus）
      ↓
   Router 判定 verdict＝「要不要執行」＋「要不要轉發」
      ├── V_EXECUTE → 本地執行（dispatch 給 handler）
      └── V_FORWARD → 送出去（`_forward` → `dst.write(frame)`）
```

> `self` 是 Router 的**保留字＝本地執行的開關**；`out` 裡的管子名（`now` / `uart0`…）才是出口。
> 兩者都寫＝兩個路徑都走，例如 `{ "in": "self", "out": ["self", "now"] }`。
> 面板現在是 `{ "in": "self", "out": ["now"] }` —— 只外送、不本地執行。

**但目前 vBus 的注入點是 `ScheduleTask._inject()` 的私有方法** → 要搬到公開位置：

```python
ScheduleTask._inject(cb, frame)   →   cb.inject(frame)      # 通用能力，搬到 bus 上
```

### 3.3 管子自己解析幀頭的 `addr`

**協議層只需要「把這一幀送出去」一個動作** —— 目的地已經寫在 `addr` 裡：

```python
b.write(frame)     # 依 frame 的 addr 決定去哪裡（見 §6.2）
```

| bus | `write(frame)` 實作 |
|---|---|
| `NowBus` | 解析 `addr` → `by_cid()` 或 `master_mac` → `send(mac, frame)`；`0xFFFF` → 廣播 |
| `CircuitBus` | 靠 header `addr` 過濾（**共用線不需要實體位址**）|
| `NetBus`(WS) | 單一連線 |
| `NetBus`(UDP) | 對應位址（待補）|

> ✅ **不需要 `send_to(peer, frame)`** —— 位址已在 `addr`，管子自己解析。
> ✅ **MAC 從此不出現在協議層或 UI。**

### 3.4 被動回覆**不**繞 vBus

收到別人的幀要回話時，**走 `ctx["send"]`（來源那條管子）**，不注入 vBus：

- 繞 vBus 會變成**迴路**（自己的回覆又進自己的解碼鏈）
- 而且會**失去「這一幀從哪條管子來」**這個資訊

### 3.5 管子能力表

| 管子 | 廣播 | 定向要不要實體位址 | 共享媒介（會碰撞）|
|---|---|---|---|
| **ESP-NOW** | ✅ 射頻廣播 | ✅ **要 MAC** | ✅ 會 |
| **UART / RS485** | ✅ 寫出去就是廣播 | ❌ 只要 cid | ✅ **會（且更嚴重）** |
| **WS** | ❌ 逐連線 | ✅ 連線本身 | ❌ 不會 |
| **UDP** | ✅ 廣播位址 | ✅ IP | ❌ 不會 |

★ **抖動（§7.1）是所有共享媒介的需求** —— RS485 匯流排上 N 台同時回話是**電氣層**的硬碰撞。

---

## 4. 方向圖

### 4.1 Master 端

```
   Master 端（面板／遙控器）
   ═══════════════════════════════════════════════════════════════════

   【發起】UI / schedule / task ── ★ 兩個入口要分流（§3.1）
        │
        ├──① 內部執行 → disp.exec_cmd(cmd, args, ctx) ──► handler（到此為止）
        │                不經 Router，**結構上保證不會外送**
        │
        └──② 對外發出 → 組幀（addr = 目標 cid，或 0xFFFF）
                          ↓  ★ 不碰 NowBus、不碰 MAC
                     vbus.inject(frame)
                          ↓
                     BusDecodeTask 解碼（來源 = self）
                          ↓
                     Router 判定 verdict
                        ├── V_EXECUTE → 本地執行（handler）
                        └── V_FORWARD → 送出去（dst.write(frame)）
                                              ↓
                                    NowBus.write(frame) 解析 addr（讀既有記錄）：
                                      0xFFFF              → 射頻廣播
                                      PeerRegistry 有這台  → 它的 mac → 單播
                                      查不到              → 「剛剛講話的人」（回退）

   【收集】廣播 0x100D{ reply_cid = 我的cid, timeout_ms = 500 }
        ◄── 各 Slave 隨機延遲回 0x100E{cid, slave_id, ip}
             → 自動進 PeerRegistry（cid + mac 都記下）
             → 通道建立（ESP-NOW 要 add_peer(mac)；UART/WS 不需要）

   【認領】對清單逐台送 0x1016{ master_cid = 我的cid }   ← 單對單，天然不碰撞
```

### 4.2 Slave 端（三條發送路徑）

```
   Slave 端（執行端）—— 沒有清單，只有 master_cid + master_mac
   ═══════════════════════════════════════════════════════════════════

   【A】被動回覆 ── 收到指令要回話（協議回覆語意）
   ─────────────────────────────────────────────────────────────────
     實體管子收到幀 → BusDecodeTask 解碼 → handler
                                              ↓
                                     ctx["send"](frame)   ← 發射出口（§7.1）
                                              │
                                              ↓
                             解析 addr（讀我手上那份記錄）：
                               addr == bus.master_cid → master_mac   ← 唯一那筆
                               0xFFFF                 → 廣播
                               查不到                 → 「剛剛講話的人」（回退）

   【B】抖動 ── 延後回覆 0x100E（★ 不能走 A 的「立即」）
   ─────────────────────────────────────────────────────────────────
     實體管子收到廣播 0x100D
          ↓
     on_identify_req：組 0x100E（addr = reply_cid）＋ 抽隨機 t
          ↓
     ctx["send"](frame, delay_ms = t)          ← ★ 只多一個參數
          ↓
     解碼鏈把它排進「這條管子的 pending」（深度 1）
          ↓  （handler 立刻返回，不等待）
     每輪迴圈尾端：到期 → 用**排程當下捕獲的來源**送出去

   【C】主動 ── 本機發起（Slave 自己的 UI / task / schedule）
   ─────────────────────────────────────────────────────────────────
     UI / task ── ★ 分流（§3.1）
        ├──① 內部執行 → disp.exec_cmd(...)      （不經 Router）
        └──② 對外發出 → vbus.inject(frame)
                              ↓
                        BusDecodeTask（來源 = self）
                              ↓
                        Router 判定 verdict
                         ├── V_EXECUTE → 本地執行
                         └── V_FORWARD → 實體管子 → 解析 addr
                                                       ↓
                                             addr == master_cid → master_mac
```

### 4.3 兩端的差別只有一個

| | 解析 `addr` 時讀什麼 |
|---|---|
| **Master** | **清單**（`PeerRegistry`，每台的 `{cid, mac}`）|
| **Slave** | **那一個值**（`master_cid` → `master_mac`）|

**兩邊都只是「用已經存好的資料」。**

---

## 5. 儲存：兩端各存什麼

### 5.1 Master 端

#### (a) 節點表 `PeerRegistry`（key = 硬體 id）

btree：`@peer.<slave_id>`（逐 key，**2 秒寫入節流**）｜記憶體：`bus.shared["peers"]`

```python
{
  "24EC4A2CA430": {                 # key = slave_id（硬體 id，大寫 hex）
      "slave_id": "24EC4A2CA430",
      "cid":       0x2CA4,          # ← 協議層：身分／定址（未學到 = None）
      "mac":       "24EC4A2CA430",  # ← 射頻層：只有 ESP-NOW 才有，其他管子 = None
      "ip":        "{\"lan\":\"192.168.1.50\"}",
      "name":      "",
      "via":       "identify_rsp",  # "identify_rsp" / "announce" / "frame"
      "first_seen": 123456,         # ticks_ms（跨開機無意義）
      "last_seen":  234567,         # 載入時歸 0 = 本次開機未見過
      "hits":       3,
      "ifaces":    ["NOW-Bus"]
  },
}
```

> **key 為何是 `slave_id` 而不是 cid**：它是**硬體 id**（`machine.unique_id()`），每台 ESP32 都有、
> 跨管子都拿得到（`0x100E`/`0x1002` 都帶它），而且唯一。
> `cid` 是**協議位址**（可能撞號、可能被改），拿來當儲存鍵會讓撞號變成資料損毀。
> → **儲存鍵 = 硬體 id；定址 = cid；射頻 = mac。**

#### (b) 目標清單 `@node.targets`（**只存 cid**）

```python
[
  {
    "cid":   0x2CA4,           # ← 唯一欄位（定址用；發射時填幀頭 addr）
    "name":  "執行A",          #   顯示名
    "active": True             #   恰一個為 True = 目前發射對象
  },
]
```

> ✅ **回到文件原本的設計**（`sys_bus.py:31` / `ConfigManager.py:380`）：
> 「**目標的 MAC 不重複存** —— 由 peers 表 `by_cid()` 查（單一事實）」
> **文件是對的，是程式碼漂走了**（`remote.py:170` 自己存了一份 `mac`）。

#### (c) 節點自身狀態 `@node.*`（已有，不動）

```python
{"cid":1, "mac":"24EC4A2CA430", "hostname":"s3-cpanel", "role":"master",
 "master_cid":0xFFFF, "bound":False, "targets":[…]}
```

### 5.2 Slave 端 —— 三個值，沒有清單

| 值 | 存哪 | 開機 | 用途 |
|---|---|---|---|
| `master_cid` | btree `@node.master_cid` | 載入（**半永久**）| 協議層：定址（同時是「這是不是我 Master」的判準）|
| `master_mac` | btree `@node.master_mac` | 載入（**半永久**）| 射頻層：解析 `addr == master_cid` 時用它（**唯一那筆記錄**）|
| `_claimed` | **純記憶體 1 bit** | **歸零** | 本次開機是否已被認領 |

`bus.shared["node"]`：

```python
{"cid":0x2CA4, "mac":"24EC4A2CA430", "hostname":"", "role":"slave",
 "master_cid":0x0001,
 "master_mac":"24EC4A2CA430",     # ← 新增
 "bound":True, "targets":[]}
```

**為什麼 Slave 要記 `master_mac`**：

| 用途 | 沒有它會怎樣 |
|---|---|
| 解析 `addr == master_cid` | 只能用「剛剛講話的人」回退 → **延後發射（抖動）時會回錯人** |
| **主動發送**（`0x1002` 公告）| 只能廣播；有了它可**定向**送 |
| 重開機後 | 不必等 Master 先開口就能定向 |

在 UART/WS 上這個欄位可以是空的 —— 「怎麼送」是傳輸層的事。

---

## 6. 指令

### 6.1 `0x100D IDENTIFY_REQ` —— 追加一個欄位

```json
{"cmd": "0x100D", "name": "IDENTIFY_REQ", "payload": [
    {"name": "reply_cid", "type": "u16"},
    {"name": "timeout_ms", "type": "u16"}      // ← 新增（追加在尾端）
]}
```

**向後相容**：`SchemaCodec` 兩條 decode 路徑都有

```python
if pos >= plen and tc != 5:
    break          # schema_codec.py:52（viper）／:183（Python）
```

→ payload 比 schema 短就跳脫，**後續欄位不存在於 dict**（不是補 0）。舊客戶端只送 2 B 時 `args.get("timeout_ms", 0)` = `0` ✓

| 值 | 語意 |
|---|---|
| `0` | **不抖動，立即回**（＝現行行為；沿用 `0x1301` 的「不給參數＝不做額外的事」）|
| `> 0` | 隨機延遲 `[0, timeout_ms]` 後回 |

**不寫進 config** —— 由 Master 每次掃描自己決定（彈性）。

### 6.2 ★ 位址解析規則（唯一一張表，兩端共用）

| 幀頭 `addr` | 讀哪份既有記錄 | 結果 |
|---|---|---|
| `0xFFFF` | — | **廣播** |
| `== bus.master_cid` | **`bus.master_mac`** | 單播給 Master（**Slave 端**主要路徑）|
| 其他 | **`PeerRegistry.by_cid(addr)`** | 單播給該 peer（**Master 端**主要路徑）|
| 都查不到 | — | **「剛剛講話的人」**（`_last_src_mac`）—— 保留既有回覆語意，相容 |

★ **這張表就是「不需要額外設定」的落實**：兩端各自讀自己已經存好的記錄。
★ `by_cid()` 因此**復活**（目前全專案零呼叫者）—— 它不是死碼，是**本來就該被這條路徑用**。

### 6.3 `0x1016 SET_MASTER` —— payload 不變，Slave 端行為改（§7.3）

---

## 7. 流程細節

### 7.1 ★ 抖動（延後發射）—— 掛在**解碼鏈**，不動任何 bus 類別

**為什麼不放 `NowBus`**：抖動是**所有共享媒介**的需求（RS485 更需要），
放單一 bus 等於其他管子要各做一次。而且 `NowBus` / `NetBus` / `CircuitBus`
**沒有共同基底類別**。

**放哪**：`BusDecodeTask` —— 它三個條件都符合：每輪跑 ✓、已迭代所有 bus ✓、已有 housekeep 尾端 ✓
而且 **`ctx["send"]` 就是它交給 `handle_stream` 的**。

```python
# tasks/bus_decode.py —— 本計劃唯一新增結構的地方
class _TxOut:
    """一條管子的發射出口（**每條 bus 建一次，不是每幀**）。"""
    __slots__ = ("mgr", "bus")
    def __init__(self, mgr, bus):
        self.mgr, self.bus = mgr, bus
    def __call__(self, frame, delay_ms=0):
        if delay_ms > 0:
            self.mgr.defer(self.bus, frame, delay_ms)   # 排程，立刻返回
            return True
        return self.bus.write(frame)                    # 立即

class BusDecodeTask(Task):
    def on_start(self):
        ...
        self._tx = {}          # id(bus) → _TxOut（快取）
        self._pending = {}     # id(bus) → (fire_at, bus, frame)  ← 深度 1／管子

    def loop(self):
        ...
        self._fire_due()       # ← 掛在既有 housekeep 旁邊
```

**效果：**

| | 改動 |
|---|---|
| `NowBus` / `NetBus` / `CircuitBus` | **抖動這件事上一行都不用改** ✓ |
| handler | **一行都不用改**（`ctx["send"](frame)` 照舊；`_reply` 多帶一個 `delay_ms`）|
| 每幀配置 | ❌ 沒有（`_TxOut` 每條 bus 快取一個）|
| `_drain` | 一行：`b.write` → `self._tx_of(b)` |

**契約**：
- `defer()` 當下就把「這一幀的來源」定下來（延後時「剛剛講話的人」早就換了）
- 深度 1：新掃描覆蓋舊的未發項
- 所有共享媒介的管子天生都有延後能力 → 未來 RS485 要用不用再改

**隨機源**：`urandom.getrandbits(16) % (timeout_ms + 1)`（真機已驗有）

| 兩個必要性質 | 為什麼 |
|---|---|
| ①同一台每次不同 | 否則每次都用同一個時槽，還是會撞 |
| ②不同台不同 | 這才是抖動的目的 |

**Airtime 估算（ESP-NOW）**：`0x100E` 約 30 B、空中時間 < 1 ms。10 台散在 500 ms 內，
碰撞機率 ≈ C(10,2) × (1/500) ≈ **9%** → 可接受，掉了下次掃描再收。

> 這是**統計**保證不是硬保證。節點數若長到 > 20，再上 LBT。

### 7.2 Master 端：收集

```
廣播 0x100D{reply_cid, timeout_ms=500}  →  各 Slave 隨機延遲回 0x100E
                                        →  自動進 PeerRegistry
```

**收集一輪就夠**；漏掉的（碰撞）**由使用者再按一次掃描** —— 符合「沒有等待」。

### 7.3 Slave 端狀態機

```
收到 0x100D：
    ├ timeout_ms == 0 → 立即回 0x100E（delay_ms = 0）
    └ timeout_ms > 0  → ctx["send"](frame, delay_ms = rand(0, timeout_ms))
    ★ 永遠回，沒有靜默

收到 0x1016{M}：
    ├ if not _claimed:
    │      master_cid = M                    （協議層，payload）
    │      master_mac = 收幀當下的來源        （射頻層）
    │      落盤（★ 值沒變則不寫 flash）
    │      _claimed = True
    └ else:
           忽略（要換 Master 就重啟）
```

**`_claimed` 為什麼必要**：`master_cid` 是**半永久**的，光靠它會變成「永遠不能被換」。
加了「本次開機」的 1 bit，就得到「**重啟一下，再讓新的 Master 執行**」。

**為什麼「永遠回」可以**（不需要靜默）：
- Master 已有完整清單，認領是**單對單**，不需要靠「沒人回」判斷收斂
- Slave 永遠回 → **第二台 Master 掃描時看得到所有節點**（不會是空白清單）
  → 送 `0x1016` 被忽略後，可從 `0x1101` 查詢看出「已被佔用」（＝ `todo/04` 的 D1/D2 可見性）

### 7.4 ★ 半永久：值沒變不寫 flash

現行 `on_set_master` **無條件** `save_node()`（3 個 btree key + `flush()`），
而 `kv_set` 沒有變更偵測（`self._db[key] = …` 一律寫）
→ 掃描會重複確認同一台 → **每輪每台一次 flash 抹寫**。

```python
prev = (bus.master_cid, bus.master_mac, bus.role)
... 賦值 ...
if (bus.master_cid, bus.master_mac, bus.role) == prev:
    return                      # 半永久狀態沒變 → 不碰 flash
cfg_manager.save_node()
```

### 7.5 🆕 `cid` 撞號偵測

學習點（`on_identify_rsp` / `on_slave_announce`）與認領點比對既有記錄：

```
同一 cid 出現在兩個不同 slave_id → 印 ❌ 警告並標記 "cid_conflict": True
```

理由見 §2.2：這是**設定錯誤**，會讓共用匯流排上的定址指令被兩台同時執行。

---

## 8. 檔案改動清單

| 檔案 | 改動 |
|---|---|
| `slave/schema/sys.json` | `0x100D` 追加 `timeout_ms(u16)` |
| `slave/tasks/bus_decode.py` | ★ **主要改動都在這**：`_TxOut`（每 bus 快取）、`_pending`（深度 1）、`_fire_due()`（掛 housekeep 旁）、`_drain` 傳 `self._tx_of(b)` |
| `slave/lib/sys/circuit_bus.py` | 🆕 **`inject(frame)`**（從 `ScheduleTask._inject` 搬過來；通用能力）|
| `slave/tasks/schedule.py` | 改用 `cb.inject(frame)`（移除私有 `_inject`）|
| `slave/lib/sys/now_bus.py` | **`write(frame)` 依 `addr` 解析目的地**（§6.2）+ **`init()` 介面修正**（§10.1）。⚠️ **不碰延後機制** |
| `slave/action/net_actions.py` | `on_identify_req`（排程/立即）＋ `on_set_master`（`master_mac`、`_claimed`、沒變不寫 flash）＋ `cid` 撞號偵測 |
| `slave/lib/sys/sys_bus.py` | 新增 `master_mac`、`pair_claimed` |
| `slave/lib/sys/ConfigManager.py` | `_NODE_KEYS` 加 `node.master_mac`；`node_state/save_node/load_node/clear_node` 一併帶上 |
| `slave/lib/sys/peer_registry.py` | `by_cid()` **啟用**；加入 cid 撞號偵測 |
| `slave/ui/lvgl/page/remote.py` | **移除直接呼叫 `NowBus`** → 改走 `vbus.inject(frame)`；`targets` **不再存 `mac`**；掃描帶 `timeout_ms`；清單顯示 cid 撞號警示 |
| `test/protocol/test_node_pairing.py` | **新增**：68 項（§11 全部涵蓋）|
| `slave/ui/lvgl/page/pixel_controller.py` | 🆕 **計畫外但同類**：它**直接抓 `now._esp`（私有屬性）自己 `send()`** —— 比 `remote.py` 舊版更嚴重（繞過 `connected`、統計、Router）。已改走 vBus，函式改名 `_send_mode_broadcast()` |
| `slave/action/now_actions.py` | `_mac_str_to_bytes` 失敗改印警告（§10.2）|
| `slave/action/sys_actions.py` | 🆕 `0x1001 on_sys_info_get` 補 `master_mac` 顯示（可選）|
| `doc/*`、`todo/*` | 語意同步 |

> **實作完成度（2026-10）**：階段 1~4 全部落地。`todo/04` 的 **Task 1（解除雙邊同步）**
> 順帶完成（handler 的 `0xFFFF`=解除 ＋ UI 的 `_do_unbind` 通知）；
> Task 2（`0x1101` 驗證）仍待做 —— 那需要「送查詢 + 下次刷新比對」的非阻塞配對層（`todo/04` L1）。

---

## 9. 已定案的決策

| # | 決策 | 值 |
|---|---|---|
| D1 | 抖動視窗放哪 | **寫進 `0x100D` 的 `timeout_ms`**，不寫 config |
| D2 | `timeout_ms = 0` | **不抖動、立即回**（＝現行行為）|
| D3 | 誰會抖動 | **只有 `0x100D`**（全專案「廣播出去、N 台回話」只有它）|
| D4 | 抖動寫在哪 | `on_identify_req` 內 → 範圍由**程式碼位置**保證 |
| D5 | 佇列深度 | **1**（新掃描覆蓋舊的）|
| D6 | 隨機源 | `urandom`（真機已驗）；保留一行 try/except 保險 |
| D7 | Slave 端靜默 | **不做**。永遠回覆 |
| D8 | 換 Master | **重啟 Slave**（`_claimed` 開機歸零）→ 每次開機先到先得 |
| D9 | Slave 記錄 | `master_cid`（協議）**＋** `master_mac`（射頻）|
| D10 | **身分** | **`cid`（協議層，所有管子通用）**；MAC 只是 ESP-NOW 的傳輸屬性 |
| D11 | 收斂方式 | **不靠「沒人回」** —— Master 有清單，逐一認領 |
| D12 | 半永久寫入 | **值沒變不寫 flash** |
| D13 | `ctx["addr"]` | **不需要**（沒有靜默判準）|
| D14 | **MAC 的邊界** | MAC **不得出現在協議層或 UI**；只在 `NowBus.write()` 內解析 |
| D15 | **cid 撞號** | **偵測並回報**，不改變身分（§7.5）|
| D16 | **本機發起走哪** | `vbus.inject(frame)` → Router（不直接呼叫實體管子）|
| D17 | **位址解析** | `write(frame)` 依 `addr` 讀**既有記錄**（§6.2）—— 不新增查表層、不新增服務 |
| D18 | **被動回覆** | 走 `ctx["send"]`（**來源那條管子**），**不繞 vBus** |
| D19 | **`targets`** | **只存 `cid`**；MAC 由 `PeerRegistry` 查（＝文件原本的設計）|
| **D20** | ★ **延後機制的歸屬** | **解碼鏈（`BusDecodeTask`）**，不是任何單一 bus —— 所有共享媒介共用，未來 RS485 不用再改 |
| **D21** | ★ **「絕對內部執行」** | ✅ **已決（不改 Router）**：`disp.exec_cmd()` ↔ `vbus.inject()`，**分流在產生者這一側**（§3.1）。詳見 `todo/06_router_local_paths.md` |

---

## 10. 建議一起修（真機稽核找到的）

### 10.1 ★ `NowBus.init()` 在 AP-only 時**靜默失效**

```python
# now_bus.py:57
if not sta.active() and not ap.active():   # AP 開著 → False → STA 不會被開
    sta.active(True)
self._esp = espnow.ESPNow()
self._esp.active(True)                     # 這個「成功」（不報錯）
self.connected = True                      # → 宣稱連上了
```

**實測**：AP-only 時 `espnow.send()` 回 `ESP_ERR_ESPNOW_IF (-12396)`。
而 `send()` 的錯誤處理只有一個計數器 → **UI 開關顯示 ON，但什麼都送不出去**。

```python
if not sta.active():
    ch = channel if channel is not None else (ap.config('channel') if ap.active() else None)
    if ch is None:
        print(f"❌ [{self.label}] 沒有可用頻道"); return False
    sta.active(True); sta.config(channel=ch); time.sleep_ms(100)
```

### 10.2 `_mac_str_to_bytes()` 解析失敗**靜默回廣播**

`now_actions.py:11-18`：任何解析失敗 → 回 `BCAST_MAC` → 打錯 MAC 會變成「廣播給所有人」。改為印警告。

### 10.3 文件／測試保留原設計、實作漂走 —— **已知兩例**

| # | 文件說 | 實作 | 追蹤 |
|---|---|---|---|
| 1 | 「目標的 MAC 不重複存，由 `by_cid()` 查」 | `remote.py` 自己存了一份，`by_cid()` 零呼叫者 | 本計劃 D19 |
| 2 | `router_selftest.py` 期望 `VBUS → "vbus"` | `_LABEL_EXACT` 摺成 `"self"` | `todo/06` |

> 動這兩塊之前**先讀文件，不要相信現況**。

---

## 11. 測試（離線）—— ✅ 已完成，107/107 PASS

```bash
python -B test/protocol/test_node_pairing.py     # 68 項
python -B test/protocol/test_master_writer.py    # C3 回歸 19 項（不能壞）
```

**實作時測試抓到的 3 個真 bug**（都修了）：

| # | 症狀 | 根因 |
|---|---|---|
| 1 | 抖動值**全部聚在一起**（`[306,310,313,…]`）→ 等於沒抖動 | 我原本用 `time.ticks_us()` 當隨機源，誤以為「各板開機基準獨立」。**同時上電的兩台會很接近** → 改用真機已驗的 `urandom` |
| 2 | `cid_conflict` 標記**永遠留著**（另一筆清了、這筆沒清）| 撞號是「一對」的性質 → 改成**整表重算**（表很小，成本可忽略）|
| 3 | 解除綁定只改本機 → 對方永遠回給已不要它的主控 | 補 `_do_unbind` 的 `0x1016{0xFFFF}` 通知（`todo/04` G1）|

**測試項目**（照專案慣例：自包含、`✅ PASS / ❌ FAIL`、PC 與裝置都能跑）

| # | 測項 |
|---|---|
| 1 | `timeout_ms` 缺席（舊客戶端 2 B payload）→ `args.get("timeout_ms", 0) == 0`，且 `reply_cid` 正確解出 |
| 2 | `timeout_ms > 0` → handler **立刻返回**（不阻塞），該管子的 pending 有 1 筆 |
| 3 | 到點才發射，且**去向 = 排程當下捕獲的來源**（不是「剛剛講話的人」）|
| 4 | 深度 1：連兩次掃描 → 只留最新那筆 |
| 5 | `timeout_ms = 0` → 立即發射（不進 pending）|
| 6 | 隨機落點在 `[0, timeout_ms]` 內，且多次取樣不全等（性質①）|
| 7 | **`write()` 依 addr 解析**：`0xFFFF`→廣播／`==master_cid`→`master_mac`／其他→`by_cid()`／查不到→`_last_src_mac` |
| 8 | `on_set_master`：`master_mac` 同時寫入；**值沒變不觸發 flush**（計數 `_db.flush()`）|
| 9 | `_claimed`：第一次接受、第二次忽略；重設後可再接受 |
| 10 | **`cid` 撞號偵測**：兩個不同 slave_id 回報同 cid → 標記 `cid_conflict` |
| 11 | **`ui/` 不得直接碰實體管子**：AST 檢查 `slave/ui/` 底下**不得**出現 `NowBus` / `add_peer` / `write_to` / `.broadcast(` |
| 12 | **延後機制與 bus 無關**：AST 檢查三個 bus 類別裡**不得**出現 `_pending` / `defer` / `fire_due` |
| 13 | `CircuitBus.inject(frame)` == 舊 `ScheduleTask._inject` 的行為（byte-for-byte 進 rx_hub）|
| 14 | AST：`node.master_mac` 三個函式（save/load/clear）都有帶 |
| 15 | AST：`ui/` 或 `action/` 不得把 `mac` 寫進 `@node.targets`（D19）|
| 16 | `NowBus.init()`：模擬 AP-active/STA-inactive → **必須呼叫 `sta.active(True)`** |

---

## 12. 真機驗證清單 —— ✅ 已完成（2026-10，兩塊板）

腳本都在 `temp/`（未進版控）：
`board_util.py`（共用工具）、`two_board_jitter_test.py`、`two_board_status_probe.py`、
`probe_panel_reset.py`。

| # | 項目 | 環境 | 結果 |
|---|---|---|---|
| 1 | 單板：排程不阻塞、延遲落點正確 | 1 板 | ✅ §12.1 |
| 2 | 單板：`master_mac` 落盤後重開仍在 | 1 板 | ✅ §12.2 |
| 3 | 2 板：廣播收集，抖動有效 | 2 板 | ✅ §12.1 |
| 4 | 2 板：`0x1016` → `master_cid`/`master_mac` 都對 | 2 板 | ✅ §12.2 |
| 5 | 2 板：重啟 Slave → 新 Master 能認領 | 2 板 | ✅ §12.2 |
| 6 | 2 板：重複送不重複寫 flash | 2 板 | ✅ §12.3 |
| 7 | 單板：UI 走 vBus，Router `{in:"self",out:["now"]}` 真的送得出去 | 1 板 | ✅ |
| 8 | AP-only 時 ESP-NOW 失敗已被修掉 | 1 板 | ✅（`iface=STA, channel=6`）|
| 9 | **另一條管子**（UART/WS）跑同一條流程 | — | ⬜ 待排 |

★ 側面收穫：`0x1101 STATUS_GET` 的**空中查詢**在真機上可用（20~30ms 往返），
這條路同時就是 `todo/04_remote_pairing.md` **D1 / Task 2** 指定的「問對方認誰」——
本輪順便完成。

### 12.1 抖動：真的散開，而且 handler 不等待

`0x100D{reply_cid=0x0002, timeout_ms=500}` ×12。B 板用**純內建 `espnow`/`network`**
自己造幀（不依賴本專案程式碼）—— 這順便證明**對端不必跑同一套程式**，協定真的只是協定。

| | 值 |
|---|---|
| B 板量到的回覆延遲 | 132 79 462 358 219 264 350 169 159 312 429 257 |
| 面板自報的抖動值 | 111 61 447 327 201 247 329 150 141 290 400 240 |
| 差值（＝固定量測開銷） | 21 18 15 31 18 17 21 19 18 22 29 17 → **≈20ms** |

- 12 次**全不重複**、range ≈ 385ms（視窗 500ms）→ 不是固定值、也不是叢集
- 差值穩定在 15~31ms → 抖動被**忠實**實現，不是「差不多延後」
- 回覆幀的 NC4 幀頭 `addr` **12/12 == `reply_cid`(0x0002)**，payload 的 `cid` 仍是面板自己的 `1`
  → ★ `reply_cid` 只作用在**這一封**，**沒有**改全域方向（C3 的真機證明）

**非阻塞**：連發 5 次（`timeout_ms=0`、間隔 60ms、中間不等回覆）→ **5/5 都收到**，
每封 0~1ms 到手。若 handler 是「`sleep_ms(jitter)` 再回」，後面的請求會卡在
解碼鏈上掉光 —— 這裡證明它只**排程**、不等待。

### 12.2 認主：用 `0x1101` 讀**對方記憶體**，不要讀檔

⚠️ **不要讀 `config.json` 驗證節點狀態** —— 節點狀態存在 **btree**
（`ConfigManager.kv_set`），config.json 裡永遠沒有 `node` 這一節。
第一版探針就是這樣量出四個**假失敗**（`master_cid=None`），
而實際上每一步都成功。教訓：**驗證要走專案自己的機制**。

```
[S0] 還沒配對
     {"node": {"master_cid": 65535, "master_mac": null, "role": null,
               "bound": false, "cid": 1, "hostname": "s3-cpanel", ...}}
[S1] 送 0x1016{0x0002}
     {"node": {"master_cid": 2, "master_mac": "A085E3E86704",
               "role": "slave", "bound": true, ...}}          ← 認主生效
[S4] hard reset 後 → master_cid 仍是 2                        ← btree 還原 ✓
     送 0x1016{0x0003} → master_cid 變成 3                     ← 換 master ＝ 重啟 Slave ✓
[S5] 送 0x1016{0xFFFF} → master_cid 回 65535、master_mac null、bound false ✓
```

`master_mac` 的值是 **B 板的 MAC**（`A085E3E86704`），且是**收幀當下從射頻來源學到的**
—— 這正是「兩個位址一起記」的另一半在真機上成立。

### 12.3 flash 寫入次數 ＝ 「認主」日誌行數

`on_set_master` 的成功路徑新增一行 `[Net] SET_MASTER 認主 ...`，而它排在
「值沒變就 `return`」**之後** → **那行出現幾次，就等於抹寫幾次**。

| 動作 | 面板日誌 | 意義 |
|---|---|---|
| `0x1016{2}` ×1 | `認主 0x0002 (mac=A085E3E86704, role=slave)` ×1 | 認主 ＋ 寫 flash 一次 |
| `0x1016{2}` ×4 | （無） | ★ **同值 → 完全不碰 flash** |
| `0x1016{3}` ×5 → `{7}` ×2 | `忽略 0x0003…` ×3、`忽略 0x0007…` ×2 | ★ 拒絕看得見，`master_cid` 維持 2 |
| 重開機後 `0x1016{3}` | `認主 0x0003` ×1 | ★ 換 master ＝ 重啟 Slave（先到先得） |

為什麼不去讀檔案 mtime：**要讀檔就得進 REPL，而進 REPL 本身會觸發存檔** → 量測被污染。

#### 12.3b ★ 換 master **不必重啟** —— 先解除就好（`temp/probe_master_swap.py`）

上一輪只驗了「**重啟後**換手」，漏掉「**先解除、再換手、全程不重啟**」這一格。
補測結果（**整場只開機一次**）：

| 步驟 | `master_cid` | 面板日誌 |
|---|---|---|
| `claim(0x0002)` | **2** | `認主 0x0002` ×1 |
| `claim(0x0003)` ×3 | 2（不變） | `忽略 0x0003` ×3 |
| `claim(0xFFFF)` | **65535** | `解除配對` ×1 |
| `claim(0x0003)` | **3** ← ★ | `認主 0x0003` ×1 |
| `claim(0x0007)` ×3 | 3（不變） | `忽略 0x0007` ×2 |
| `claim(0xFFFF)` | **65535** | `解除配對` ×1 |

```
Boot complete ×1     認主 ×2     忽略 ×5     解除 ×2
```

**結論（對照 D8）**：

- **A 直接換 B** → 被 `pair_claimed` 擋掉 → 這才是「要重啟」的那條路
- **A → 解除 → B** → `clear_node()`／解除分支把 `pair_claimed = False`
  → **不必重啟**，新的 master 立刻接手 ✓
- 兩條路都保留是刻意的：前者防止「掃描就把人家的 master 搶走」，
  後者給「人明確要換手」一條不必動電源的路。

> 對照組：§12.3 的「重開機後 `0x1016{3}` 被接受」＝ 重啟那條路；本節＝不重啟那條路。
> 兩者在真機上都成立。

### 12.4 ★ `0x1016` 是 fire-and-forget：「送了」不等於「到了」

上表 `0x1016{3}` **送 5 次只被拒絕 4 次**，`{7}` 送 2 次到 2 次。
→ **ESP-NOW 廣播在共用媒介上實測會掉幀**（這還是只有兩塊板的乾淨環境）。
`0x1016` 是設計上**沒有 ACK** 的指令，所以「掉一次就永遠沒生效」是預期行為，不是 bug。

**正確用法**（`todo/04` D1 的查法在這裡第一次真的被用上）：
**送 → 用 `0x1101` 查 → 沒到就重送**。探針裡的 `claim_until()` 就是這樣寫的，
加上重試之後 §12 全數穩定通過。

> 這也是為什麼 `on_set_master` 的拒絕**必須看得見**：請求者唯一的回饋就是去查。

### 12.5 ⚠️ 開發這條流程時**一定要 hard reset**（真實踩到的坑）

**症狀**：面板開機失敗，而且**失敗點每次都不一樣** ——
第一次 `Core_Manager.py:10 MemoryError 512 bytes`，下一次
`driver/pixel_drv.py:13 MemoryError 1756 bytes`，重試四次都失敗。

**指紋**：失敗點會**往前跑**，這是資源**洩漏**、不是程式碼 bug。
同一次開機前量 `esp32.idf_heap_info(esp32.HEAP_DATA)`：

```
total=236360  free=624    largest=200     ← 236 KB 的堆只剩 200 bytes 最大塊
total=22308   free=4      largest=0       ← 完全耗盡
```

**根因**：ESP32-S3 的 `MPY: soft reboot`（＝ `Ctrl-D`、**也是 `mpremote` 的預設收尾**）
**不會拆掉 WiFi / ESP-NOW 驅動**，驅動佔走的**內部 SRAM** 每次軟重開機都留著。
開發時反覆軟重開機（上傳檔案、進 REPL）會一路累積到連 `boot.py` 的小配置都配置不出來。
注意 `gc.mem_free()` 這時還會報 **7 MB**（那是 PSRAM），完全看不出問題。

**解法**：用 `machine.reset()`（hard reset）。代價是 USB-CDC 會重新列舉，
`/dev/cu.usbmodem*` 消失約 7 秒再出現 → **控制代碼必須重開**（見 `temp/board_util.py`）。

**併發的教訓**：`Ctrl-B` 在 **friendly REPL 是無作用的**（它只在 raw REPL 裡有意義），
所以「送 `\x02` 當重開機」是錯的 —— 這讓第一次的除錯多繞了一圈（日誌全空，
看起來像沒開機，其實是根本沒重開）。重開機只有 `Ctrl-D` 或 `machine.reset()`。

---

## 13. 待決問題（開工前要定）

**✅ 五題全部已決（2026-10）—— 可以開工。**

| # | 問題 | **定案** | 理由 |
|---|---|---|---|
| **Q1** | 「最後點名」怎麼觸發 | **掃描只收集**；用現有「綁定」按鈕逐台 ＋ 加一顆「**全部綁定**」 | 保留「綁定是使用者的明確動作」 |
| **Q2** | 收集輪數 | **一輪**，漏的靠**使用者再按** | 使用者：「總不會那麼倒楣按第二次也撞車，概率很低，而且用戶喜歡不斷按」 |
| **Q3** | 認領用廣播還是定向 | **定向逐一** | 「原本設計出來也是為了掃描用的，所以一定是逐一點名」；且廣播會認領射程內全部 → 清單與現實不一致 |
| **Q4** | `inject()` 放哪 | **`CircuitBus.inject(frame)`**（bus 層）＋ `bus.register_service("vbus", …)` 1 行 | `inject` 是「寫進自己 hub」的**單一 bus 操作**；`defer` 才是跨 bus 的排程（放解碼鏈）—— 不對稱有理由，見下 |
| **Q5** | 第二條管子要不要現在做 | **先只做 ESP-NOW，抽象留好** | Router 能選管子 ≠ 管子真的通了；要驗 UART 得真的接一條 |

### Q2 的補充事實（為什麼不做自動重試）

| 觀測點 | 現況 |
|---|---|
| **NC4 層**（`pop_frame`）| CRC 不通過 → `self._start += 1` **靜默重同步，零計數、零 log** |
| **ESP-NOW 驅動層** | `espnow.ESPNow()` 有 `stats()`，但**欄位與「碰撞會不會讓它增加」未經真機確認**（探針時板子斷線）|

→ **「收到訊號但亂碼」目前觀察不到**，所以自動重試的觸發條件無法可靠判定 → 採用人工再按。
（未來若有第 2 塊板：可加 CRC 失敗計數 + `rx_dropped`，就能升級成「連續兩輪集合相同即收斂」或「最多 10 輪」。見 §14 L9）

### Q1 / Q4 的落地

| | 內容 |
|---|---|
| **Q1** | `_do_scan()` 只廣播收集；`_do_bind()` 逐台（現有）；🆕 `_do_bind_all()` = 對 `peers` 裡每一台送定向 `0x1016` |
| **Q4** | `ScheduleTask._inject(cb, frame)` → `CircuitBus.inject(frame)`（8 行搬家）；`_get_vbus()` 多 1 行 `register_service("vbus", …)`，讓 UI 不必靠 label 搜尋 |

### 「兩種本地入口」是刻意的（Q4 附帶確認）

`dispatch()` 與 `exec_cmd()` **不是兩套 handler，是同一個核心的兩層**（`dispatch` 內部呼叫 `exec_cmd`），
但語意有真實差異：

| | `exec_cmd`（①內部）| `dispatch`（②線上路徑，含 `vbus.inject`）|
|---|---|---|
| 輸入 | args（dict），不經編解碼 | bytes，要解碼 |
| `args` 多帶 | 無 | `_name` / `_cmd`（診斷用）|
| **省略欄位** | **＝用 handler 自己的預設值** | encode 會**補 0** |

`app.py` header 已明說：「語意是『**我呼叫一個函式**』，而不是『我假裝收到一幀』。**兩者場合不同，不要混用。**」
→ `todo/05` §3.1 的 UI 分流就是照這條。

---

## 14. 已知限制（不在本計劃）

| # | 限制 |
|---|---|
| L1 | 抖動是**統計**保證，不是硬保證 |
| L2 | 碰撞遺失的節點需要**人手再按一次掃描**（不做自動重試）|
| L3 | **被動回覆**（§4.2【A】）如果查不到位址，仍會退回 `_last_src_mac` —— 相容性保留，不是保證 |
| L4 | 多 Master 時，第二台看得到節點但認領會被忽略 → 要靠 `0x1101` 查詢才知道被佔用 |
| L5 | `test/` 被 `.gitignore` 忽略（`todo/04` L6）→ 新測試檔除非 `git add -f`，否則不進版控 |
| L6 | 本計劃**不處理** `todo/04` 的 Task 1（`0x1016{0xFFFF}` = 解除）與 Task 2（D1 驗證）|
| L7 | WS/UDP 的 `write()` 位址解析尚未實作（本計劃只先做 ESP-NOW 與 UART）|
| L8 | 「絕對內部執行」= `disp.exec_cmd()`（**不經 Router**，所以也不在路由表上）—— 見 §3.1 |
