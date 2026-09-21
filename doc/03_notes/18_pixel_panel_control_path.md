# 18 — 面板裝置的 pixel 控制路徑（現況盤點 + 設計草案）

> **狀態**：📋 設計討論中（尚未實作）
> **日期**：2026-09（接在 `16_pixel_panel_temp_hacks.md` 之後）
> **範圍**：`ports/S3/ESP32-S3-Control_Panel`（面板裝置）+ `slave/tasks/pixel_control_panel.py`、`slave/ui/lvgl/page/pixel_controller.py`
> **用途**：把「面板如何管理模式／計時／亮度」的現況斷點與可行設計集中一處，供拍板。

---

## 0. 一句話

**面板只廣播「設定」，查詢一律「定向」。** 所以：

1. **主路徑 = 廣播 `0x3105 MODE_SET`**：版面讓用戶直接選，廣播出去，
   **對方聽沒有就算**（廣播本來就不回覆，這是協議語意，不是缺點）。
2. **加分項 = 定向查詢**：`0x3101` 拿清單、`0x3103` 拿狀態、`0x3107` 拿名稱。
   依賴 `master_cid` / `SET_MASTER`（**機制已存在**，見 §5.1）。
3. **亮度面板不設定**：對方告訴我什麼就是什麼，面板只**存起來**、回覆時**原樣帶回**。

「模式管理器／計時器／光度紀錄」這三件事的**值**全部來自執行裝置
（協議 `doc/01_protocol/04_pixel_protocol.md` 已內建 `0x3101`~`0x3108`）。
面板不解析、不驗證、不自己播。

> **合作項目對接**：對方（第三方 firmware）會送 —— 有 `master_cid` 就送得到。
> 面板要做的第一步是開機後 `0x1016 SET_MASTER` 告知自己的 cID（見 §7.2 ⓪）。

---

## 1. 現況盤點

> ⚠️ **先講清楚哪些是「設計」、哪些是「斷點」**（2026-09 拍板）：
> 面板的 `_mode_ids == []` **是正確的設計** —— 面板不該有本地模式池，
> 清單要**主動向執行裝置查詢**取得（§2 的 `0x3101`）。所以 §1.1 不是 bug，
> 是「還沒接查詢」。真正的缺口是 §1.4 的四個裝置端實作問題。

### 1.1 模式列表空 —— **設計正確，缺的是查詢**

```python
# ui/lvgl/page/pixel_controller.py:128-141
gmode = _get_gmode()          # ✅ 存在（app.py 無條件註冊 GlobalMode）
pool  = gmode.mode_pool()     # 空 —— 這是對的
```

`gmode.mode_pool()` = `bus.shared["pixel_maps"]` + `/audio/modes/*.json`：

| 來源 | 面板現況 | 判斷 |
|---|---|---|
| `pixel_maps` | **只有 `PixelTask.on_start` 會寫**（`pixel_task.py:222`）→ 面板沒有 `PixelTask` → 永遠不存在 | ✅ **正確**，面板不該有本地模式池 |
| `/audio/modes` | `slave/audio/` 不存在 → `[]` | ✅ 正確 |

→ 清單應該由 **`0x3101 MODE_LIST_QUERY`（向執行裝置查）** 填入，
而不是讀本地 `gmode`。UI 要改的是**資料來源**，不是「修好空的池」。

### 1.2 亮度：程式碼在做一件「不該做的事」（拍板見 §4）

```python
# tasks/pixel_control_panel.py:84-87
v = max(0, min(255, int(cmd["brightness"])))
if self._st_pixel is not None and hasattr(self._st_pixel, "set_brightness"):
    self._st_pixel.set_brightness(v)      # ← 面板 st_pixel = None，整段靜默跳過
```

**判斷**：這一段**本來就不該存在** —— 面板是控制器不是燈的主人，沒有 `st_pixel`。
不是「靜默跳過可惜」，是**該刪**（拍板的設計見 §4）。

⚠️ `_broadcast_mode_set()` 目前送 `bri=0xFF`（=不設置，`pixel_control_panel.py:66`）。
協議上 `0xFF` 是「**這次不要改**」，與「**帶回存下來的值**」是兩件不同的事（見 §4）。

### 1.3 查詢 vs 設定：面板只用廣播「設定」，查詢一律定向（2026-09 拍板）

> 「我絕對不會廣播查詢，我只會廣播設定。」

這與協議 §4 第 7 條一致，也是**回覆能成立的前提**：

| 面板送出 | 位址 | 對方行為 |
|---|---|---|
| `0x3105 MODE_SET` / `0x3106 MODE_STOP` | **廣播**（`ADDR=0xFFFF`） | 執行，**不回覆**（協議明定廣播不回） |
| `0x3101 MODE_LIST_QUERY` / `0x3103 MODE_GET` / `0x3107` | **定向**（`addr = 執行裝置的 cID`） | 執行並**回覆**（回覆 addr = 面板的 cID，見 §5.1） |

⚠️ 目前 `PixelControlPanelTask` 一律 `self._now_bus.broadcast(...)`，
所以它**對查詢類是錯的**（廣播＋不回覆＝永遠沒答案）。設定類才是對的。

⚠️ 面板的 `_espnow_send_mid()`（UI 硬體直發，見 §6）也是廣播 —— 設定類 OK，
但它是繞過 task 的另一條路，要一起收掉。

### 1.4 實作缺口：查詢鏈有四個問題（`action/pixel_actions.py`）

> 前三項是**我方（slave）要補的**；第四項與 NC4 定址無關，屬射頻層，見 §5.1 末段。

| 缺口 | 證據 | 影響 |
|---|---|---|
| **`0x3103 MODE_GET` 沒有 handler** | 全 `slave/` 只有 `schema/pixel.json:33` 有定義；`pixel_actions.register()` 只註冊 `0x3101/0x3105/0x3106/0x3107` | 面板問「現在播什麼」**永遠沒人回** → 計時器與狀態沒有來源 |
| **`0x3104 MODE_GET_RSP` 沒人送** | 同上 | 同上 |
| **`0x3108` 的 `total_ms` 恆為 0** | `pixel_actions.py:132` 寫死 `"total_ms": 0` | 面板拿不到時長 → **倒數無從起算** |
| **`0x3102` 的回覆在射頻層可能送不出去** | `_send()` → `ctx["send"]` = `NowBus.write`；`write()` 在 `_last_peer is None` 時回 `False`（`now_bus.py:129-132`） | 第一次回覆可能丟失 —— **這與 NC4 定址無關，見 §5.1 末段** |

> NC4 層的**位址**已經解決了（`master_cid` / `SET_MASTER`，見 §5.1）；
> 這四個是「有沒有人送」與「射頻層送不送得出去」，是另一回事。

---

## 2. 協議現成可用的部分（不用發明）

`doc/01_protocol/04_pixel_protocol.md` §1 指令總表：

| CMD | 名稱 | 方向 | Payload | 面板用途 |
|---|---|---|---|---|
| `0x3101` | `MODE_LIST_QUERY` | 面板→執行 | `mode_type:u8`（0=全部/1=LED/2=SERVO/3=AUDIO） | 取模式清單 |
| `0x3102` | `MODE_LIST_RSP` | 執行→面板 | `mode_type`(回音) + `count:u8` + `entries`（每筆 **2B LE = 16-bit id**） | 模式清單來源 |
| `0x3103` | `MODE_GET` | 面板→執行 | (空) | 查目前狀態 |
| `0x3104` | `MODE_GET_RSP` | 執行→面板 | `mode_type, mode_id, elapsed_ms:u32, total_ms:u32, running:u8` | **計時器來源** |
| `0x3105` | `MODE_SET` | 面板→執行 | `mode_type, mode_id, start_delay_ms:u16, brightness:u8` | 設模式 + 亮度 |
| `0x3106` | `MODE_STOP` | 面板→執行 | `action:u8`（0=暫停、1=全關） | 停 |
| `0x3107`/`0x3108` | `MODE_DETAIL_QUERY/RSP` | 面板→執行 | 名稱（`str_u16len`，UTF-8 可中文） | 顯示模式名 |

**16-bit id 慣例**：內部 id = `(mode_type << 8) | mode_id`（`pixel_actions._combine()`）；
`modes/*.json` 的 `id` 即此值。送 `MODE_SET` 時要拆回 `(id >> 8, id & 0xFF)`。

> ⚠️ `0x3101/0x3102` 的 `mode_type` 是「組別選擇／回音」（0=全部）；
> `0x3107/0x3108` 與 entries 內的 `mode_type` 是「模式識別碼高位」。兩個用法不同（協議 §2.1 註）。

---

## 3. 三件事各自的歸屬

| 需求 | 放哪 | 現況 |
|---|---|---|
| **模式管理器** | `PixelControlPanelTask` 持有面板側鏡像；`GlobalMode` **面板不碰**（它是執行裝置的解析器，面板本地池永遠空） | 要新建 |
| **計時器** | `lib/sys/timer.py` 直接用：收到 `0x3104` → `remaining = total_ms - elapsed_ms` → `Timer.start([remaining], loop=False)`；UI 讀 `remaining_ms()` | `Timer` 已完成（絕對時間零漂移 + viper 快路徑），**UI 沒在用**（`control_panel.py` 手刻 5 個模組級變數 `_tick_last/_tick_carry/_tick_armed/_tick_seq/_tick_raw`） |
| **光度紀錄** | 面板側存「最後送出值」+「執行裝置回報值」，**單一範圍** | 現在散在三處、範圍三套（見 §4） |

> `GlobalMode.set_mode()` 對面板**不可用**：它 `resolve()` 不到就 `return False`
> （`global_mode.py:109-112`）。`0x0200`（SERVO 組 mode 0）是**執行裝置**的模式，
> 面板本地沒有 → 一定被拒。這正是 `16_pixel_panel_temp_hacks.md` §2.2「版本 A 被攔截」的成因。

---

## 4. 亮度：面板只是儲存格（2026-09 拍板）

**原則**：面板**不設定**亮度。對方告訴我什麼就是什麼，面板只找個位置**存起來**，
回覆時**原樣帶回、不修改**。

```
執行裝置 →（0x3104 / 0x3102 或任何帶亮度的回報）→ 面板存入 pixel_panel.brightness
面板 →（0x3105 MODE_SET）→ 原樣帶回同一個值
```

| 來源 | 目前範圍 | 處置 |
|---|---|---|
| **`action_task_1._UART_BRIGHTNESS_MAX`** | **31**（APA102 5-bit） | ✅ **基準：全部統一跟隨 APA102 = `0–31`** |
| `doc/01_protocol/04` §2.4 | `0`–`30`（`0xFF`=不設置） | ⚠️ 文件與實作差 1，要同步成 `0–31` |
| `waiting_to_trash_actions.on_status` | 夾 `0`–`36` | ❌ 協議 §6 待決 #3 已判「舊 WTT 亮度棄用」 |
| `pixel_controller` slider | `0`–`255` | ❌ 要改成 `0–31`；**且面板不主動設定**，slider 只反映收到的值 |
| `doc/01_protocol/04` §7（fastLED 對方） | `1`–`190` 映射到 `0`–`30` | ⚠️ 對方映射表要跟著改 |

⚠️ **`0xFF` 與「存下來的值」不是同一件事**：`0xFF` 在協議上是「這次不要改」
（`0` 是合法值，不能用 0 當「不改」）。面板要「不修改」就該送**存的那個值**，
而不是送 `0xFF`（現在 `pixel_control_panel.py:66` 送的是 `0xFF`）。

⚠️ 面板**不需要** `st_pixel.set_brightness()` —— 它沒有燈。那一段該刪，不是「靜默跳過可惜」。

---

## 5. 待辦與待確認（不是「未解難題」）

> §5.1 原本被列為「未解」，2026-09 查證後**基礎設施已存在**，改列為修正記錄 +
> 一個要跟合作方確認的問題。

### 5.1 回覆路徑 —— **基礎設施已經在了，不用新發明**（2026-09 修正）

> 上一版把這條寫成「裝置端必須改、三個選項挑一個」是**錯的**。
> `SET_MASTER` / `master_cid` / `reply_addr` 這套主動告知 master address 的機制**已經實作**。

**機制**（`lib/sys/sys_bus.py` + `action/net_actions.py`）：

```python
# sys_bus.py:22-23 —— 兩個 cID 是不同東西
self.cid        = 0xFFFF   # 裝置自己的協議短身份（ConfigManager 於 T0 由 System.cID 推動）
self.master_cid = 0xFFFF   # 回應定址「目標」；0xFFFF = 未設定 = 廣播；僅內存，重開機丟失

# net_actions.py:40-47 —— 所有回應都回 master_cid
def _reply(ctx, rsp_cmd, fields):
    ctx["send"](Proto.pack(rsp_cmd, payload, addr=bus.master_cid))   # ★ 關鍵

# net_actions.py:160-163 —— 0x1016 SET_MASTER：顯式設定
def on_set_master(ctx, args):
    bus.master_cid = args.get("master_cid", 0xFFFF) & 0xFFFF

# net_actions.py:67-71 —— 0x100D IDENTIFY_REQ：也可用 reply_addr 順帶告知
def on_identify_req(ctx, args):
    reply_addr = args.get("reply_addr", 0xFFFF) & 0xFFFF
    if reply_addr != ADDR_BROADCAST:
        bus.master_cid = reply_addr      # 這一輪開機保持住
```

**完整迴路**：

```
面板（cID = "0001"）                              執行裝置（cID = XXXX）
   │ ① 定向查詢：addr = XXXX（對方 cID）           │
   ├──────────────────────────────────────────────>│  收（ADDR 過濾：addr==自己 cID 才收）
   │                                               │
   │                                               │ ② 回覆 addr = bus.master_cid
   │                                               │    （要先被 ① 告知；見下）
   │<──────────────────────────────────────────────┤
   │ ③ app.handle_stream ADDR 過濾：                │
   │    addr(0001) == bus.cid(0001) → 收下 ✅        │
```

**兩個前提條件**：

| # | 條件 | 現況 |
|---|---|---|
| 1 | **面板要有自己的 `System.cID`** —— 沒設時 `bus.cid = 0xFFFF` = 廣播，**什麼都收**（會誤收別人的回覆） | ✅ 本 port 已設 `"cID": "0001"`（`ports/S3/ESP32-S3-Control_Panel/config.json`）|
| 2 | **執行裝置要先知道「master 是面板」** —— `master_cid` **只存內存、重開機丟失**，所以面板開機後要**主動告知一次** | ❌ 面板還沒有這一步 |

→ 面板要送 **`0x1016 SET_MASTER`（payload = 面板自己的 cID）** 給執行裝置。
可以廣播（`master_cid` 欄位本身就帶目標，所有執行裝置都會記住面板）；
協議原本的慣例是 `0x100D IDENTIFY_REQ` 帶 `reply_addr` 逐 address 掃描 —— 對「一台面板 + 少數執行裝置」用 `SET_MASTER` 直接設定更省事。

> **合作項目**：因為要跟別人合作的項目對接，**對方會送**（有 `master_cid` 就送得到）。

#### 剩下的真缺口：§1.4 那四個（與本節無關）

回覆路徑解決的是「位址對不對」，**不是「有沒有人送」**。
`0x3103 MODE_GET` 沒 handler、`0x3104` 沒人送、`0x3108.total_ms` 恆為 0 —— 這三個仍然要補。

#### 唯一的殘留風險（ESP-NOW 層，與 NC4 定址無關）

即使 NC4 位址正確，`NowBus` 的**射頻層**仍可能擋掉第一次回覆：

```python
# now_bus.py:129-132
def write(self, data):
    if self._last_peer is None:
        return False      # 沒人 unicast 給過我們 → 回 False
```

`_last_peer` 只在**有人 unicast 進來**時才設定（`now_bus.py:178`）。
若面板用**射頻廣播**發查詢，裝置端的 `_last_peer` 仍是 `None` → 第一次回覆回 `False`。

**三個緩解方向**（未拍板）：

| 方向 | 做法 |
|---|---|
| A. 面板在射頻層 unicast | 面板要知道裝置 MAC（`NowBus.add_peer(mac)`；`NowBus.discover()` 存在但**沒人呼叫**，或 config 寫死） |
| B. 裝置端改成廣播回覆 | 改 `NowBus.write()` 的 fallback；但那是**對方（合作項目）的實作**，管不到 |
| C. 靠 `add_peer` 的副作用 | 若面板曾 unicast 進來過（例如對方先送），`_last_peer` 就有了 —— 時序依賴，不可靠 |

> 這條要跟合作方對接時一起確認：**「你們收到查詢時怎麼回？」**

### 5.2 面板 UART 的用途（`ports/S3/ESP32-S3-Control_Panel/README.md` 待決 ①）

接收路徑四段：**① poll ✅ / ② 進 `bus_sources` ❌ / ③ Router 放行 ❌ / ④ 有人解析 ❌**。
第 ④ 環最關鍵 —— `ControlPanelTask._poll_ex_ic()` 讀的 `_ex_ic_slot` **全 repo 沒人寫**，
且它等的 `0x1403` 已被指派給 `SPEED_SET`（`schema/hw.json`）。真正的 EX-IC 5-byte 格式是
`[0xB4][mode][bri 0-31][time][0xFF]`（定義在 `action_task_1.py`，執行裝置端）。

⚠️ **不要用 `ActionTask1` 來讀 UART**：它 `_uart = uart_list[0]` 與 `CircuitBus` 包的是
**同一個 UART 物件**，且 `_try_bind_circuit_bus()` 失敗時 fallback 直接 `self._uart.read()`
（`action_task_1.py:462`）→ 同一條線兩個讀者，幀被瓜分。面板的正規介面是
`CircuitBus.read_into()`（`circuit_bus.py:100`，走 `cache_hub`，與 NC4 解碼無關）。

### 5.3 兩頁的輸出通道要分開

面板兩個控制頁服務**不同協議系列**，暫且各自獨立：

| 頁面 | 狀態區 | Task | 協議 |
|---|---|---|---|
| `pixel_controller` | `_pixel_cmd` / （新建）`pixel_panel` | `PixelControlPanelTask` | pixel `0x31xx` |
| `control_panel` | `_display_cmd` / `_display_*` | `ControlPanelTask` | WTT `0x1501/0x1502`（`waiting_to_trash` 系列，**等它進垃圾桶**） |

`control_panel.py` 的 echo 三態色契約（`_display_mode/_brightness/_time`）在面板上
**目前沒有生產者** → 永遠停在琥珀 pending。那不是壞掉，是**那一系列協議還沒到**
（`waiting_to_trash` 的命名本身就是待廢）。**先做 pixel 這條。**

---

## 6. UI 現況（`pixel_controller.py` 的硬體直發要收）

`_espnow_send_mid()` 直接抓底層 `espnow` 物件 `esp.send()`，繞過 `NowBus.broadcast()`
與 task 層（`16_pixel_panel_temp_hacks.md` §2.1 已標記）：

- 呼叫點：`_set_mode()`（列表選中即發）、`_apply_movable()`（「可動」按鈕）
- 後果：`PixelControlPanelTask._broadcast_mode_set()` 成為**孤兒**（永遠不會被走）
- 正規做法：UI 只寫 `bus.shared["_pixel_cmd"] = {"mode": id}`，由 task 轉發

⚠️ `_espnow_send_mid()` 的 fallback 分支還會 `sta.config(channel=6)` —— **channel 寫死 6**，
與 `config Network.ESP_now.channel` 可能不一致。

---

## 7. 設計：兩條路（2026-09 拍板）

**面板只廣播「設定」，查詢一律「定向」。** 所以設定是主路徑（不需要回覆），
查詢是加分項（有回就更新）。

### 7.1 主路徑：廣播「設定」—— 不需要任何回覆

```
┌─ UI 頁面（只寫狀態、只讀狀態；不碰 espnow / gmode / st_pixel）──┐
│  版面直接讓用戶選（可為固定清單，或查到的清單）                  │
│  寫: _pixel_cmd = {"mode": <16-bit id>}                        │
│  讀: pixel_panel = {mode, brightness, elapsed_ms, total_ms,    │
│                     running, modes[]}                          │
└────────────────────────────────────────────────────────────────┘
                    │ 單一消費者（讀後清）
                    ▼
┌─ PixelControlPanelTask ────────────────────────────────────────┐
│  送出: 0x3105 MODE_SET  （addr = 廣播；拆 id → (id>>8, id&0xFF)；│
│                          brightness 帶「存下來的那個值」）        │
│        0x3106 MODE_STOP （action=1）                            │
│  對方聽沒有就算 —— 不等待、不重試、不報錯                        │
└────────────────────────────────────────────────────────────────┘
                    │ ESP-NOW 廣播（channel 兩邊一致）
                    ▼  執行裝置（它自己跑 gmode，面板不解析、不驗證）
```

### 7.2 加分項：定向「查詢」—— 有回就更新（面板只廣播設定，不廣播查詢）

```
PixelControlPanelTask
  ├─ ⓪ 開機後：0x1016 SET_MASTER（payload = 面板自己的 cID）
  │      → 執行裝置記住 master_cid → 之後所有回覆都回面板（見 §5.1）
  │      ※ master_cid 只存內存、重開機丟失 → 每次開機都要告知一次
  ├─ 送出 0x3101 MODE_LIST_QUERY（addr = 執行裝置 cID，mode_type=0 全部）
  │      ← 「獲取列表」按鈕（定向，不是廣播）
  ├─ 收到 0x3102 → 重建 pixel_panel.modes（entries 每筆 2B = 16-bit id）
  ├─ 逐個 0x3107 MODE_DETAIL_QUERY 取名稱 → 0x3108 填 labels
  └─ 送出 0x3103 MODE_GET（定向輪詢）→ 0x3104 更新狀態
```

⚠️ **定向查詢需要知道對方 cID**，所以面板要先有「對象清單」。
三個取得方式（未拍板）：`0x100D IDENTIFY_REQ` 逐 address 掃描／config 寫死／
靠對方主動送進來時記下來源。

⚠️ **這條路目前還走不通**，因為 §1.4 的裝置端缺口
（`0x3103/0x3104` 沒 handler、`0x3108.total_ms` 恆為 0）與 §5.1 的射頻層 `_last_peer`。
**先把主路徑（7.1）做出來，加分項再補。**

> 📌 面板**只廣播設定、定向查詢** —— 這是本設計的硬規則（§1.3）。
> 任何「廣播查詢然後等回覆」的寫法都違反協議 §4 第 7 條，不會有答案。

### 7.3 計時器

收到 `0x3104` 時：`remaining = total_ms - elapsed_ms` →
`Timer.start([remaining], loop=False)` → UI 每幀讀 `Timer.remaining_ms()`。

⚠️ **目前沒有時長來源**：`0x3104` 沒有 handler、`0x3108.total_ms` 又恆為 0。
倒數要等 §1.4 的裝置端缺口補上（或合作方實作 `0x3104` 時一起接）。

**面板的原則**（不變）：模式池、模式名稱、目前狀態、總時長 —— **全部來自執行裝置**。
面板不猜、不驗證、不自己播（`16_pixel_panel_temp_hacks.md` §4「大家各自讀」）。

---

## 相關文件

- `doc/01_protocol/04_pixel_protocol.md` — 0x31xx 協議（**唯一真相**，本檔的依據）
- `doc/03_notes/16_pixel_panel_temp_hacks.md` — 前一份面板臨時改動清單（§2.1 硬體直發、§2.2 孤兒 task）
- `doc/02_guides/08_pixel_subsystem.md` — pixel 子系統四層資料
- `doc/02_guides/16_signal_router.md` — Router（面板的解碼介面宣告；`uartN` 的 N = list 索引）
- `ports/S3/ESP32-S3-Control_Panel/README.md` — 本板任務集與待決事項
- `slave/lib/sys/timer.py` — 計時器（直接用，不要再手刻）
- `slave/lib/sys/global_mode.py` — gmode（**面板不適用**，見 §3 註）
