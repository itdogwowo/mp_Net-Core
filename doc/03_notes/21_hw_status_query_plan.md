# 24 — I2C 地址掃描：掃描 → 存起來 → 直接讀 — 計劃（v7：**已實作、已簡化**）

> **用途**：核心目標只有一件事 —— **掃描 I2C 匯流排上的位址，存到一個地方，本地直接讀**；
> 遠端要問，就照**正常做法**回一個封包。
> **分類**：筆記（03_notes）— PLAN
> **狀態**：🟢 **已實作**（2026-10-17）；**上板實測待做** → 追蹤清單 `todo/10_i2c_scan.md`
> **最後更新**：2026-10-17（v7）
> **v7 變更**：照使用者指示**大幅簡化** —— 刪掉 `lib/sys/i2c_scan.py`（約 200 行）與離線測試；
> 掃描邏輯回到 `driver/i2c_drv.scan_devices()`（**12 行**），action / task / UI 各幾行。
> 拿掉 `detail` / `ms` / `aborted` / 預算 / 快取 TTL / `cached_bus_devices` / `flat_devices` / `detail_to_body`。
> 回覆也只剩位址：`{"0": ["0x40", "0x70"]}`（41 B）。
> **v6**：實作完成；**v5**：只做 I2C 掃描、回覆走既有 `_reply()`；
> v4 的「九種 type 狀態查詢」在 **§9 附錄**（本輪不做）。
> **相關文件**：`19_remote_control_plan.md` §9.5（死碼清單）、`18_pixel_panel_control_path.md`、
> `01_protocol/02_command_index.md`、`todo/10_i2c_scan.md`

---

> **2026-10 後續（PCA9685 頁改版）**：`page/pca9685.py` 已從「下拉 target + 16 方格」
> 改成「左目標清單 / 右 16 通道＋數值」的遙控器式版面；UI 不再自己攤平
> `_i2c_devices`，改讀 `pca9685_drv` 新增的 **`pca9685_ctl`**（手動控制目標表：
> 每筆含 `addr` / `broadcast` / `boards` / `online` / `pca`）。`_pca_actions`
> 現在只剩 `scan`；掃描完 `pixel_control_panel` 會呼叫 `refresh_pca_ctl()`。
> 本文件 §2/§3 描述的 `_i2c_devices` 掃描與 `0x1409` 回覆仍然有效；§3.2 的
> 「UI 攤平」與舊的 `_cycle_target()` 描述已被這個改版取代。

## 0. 一句話

**三個零件都已經有了，只是從來沒接起來（而且儲存格的命名是錯的）：**

```
              ┌─ 開機：掃一次 ─────────────┐
i2c.scan() ───┤                            ├──▶ bus.shared["_i2c_devices"]
              └─ 查詢 0x1402：再掃一次（更新）┘   {bus_idx: [addr…]}（舊名 _pca_devices 要改）
                                                        │
                          ┌─────────────────────────────┼──────────────┐
                          ▼                             ▼              ▼
                 PCA 初始化讀它（不重掃）        本地直接讀     遠端 _reply() 0x1409
```

★ **兩次掃描，同一格**：
- **開機**掃一次 → 列表一開始就有內容（UI 一進來就看得到，不用先按鈕），
  **而且 PCA 初始化直接讀它 → 同一條匯流排只掃一次**（§3.7）；
- **每次 `0x1402{type=I2C}`** 再掃一次 → **更新**同一格、並回 `0x1409`。
- 所以那一格永遠是「**最後一次掃描的結果**」，沒有快取、沒有 TTL。

★ 所以本計劃**不是新增能力，是三條接線 ＋ 一個改名 ＋ 一個開機掛勾 ＋ 一個「PCA 改讀快取」**。
你說「完全不需額外添加任何東西」—— 除了「`0x1402` 本來沒有回覆指令，必須補一條 `0x1409`」以外，確實沒有。

★ **改名理由**：掃的是 **I2C 匯流排**，不是「PCA 專用」。
舊名 `_pca_devices` 會讓人以為只有 PCA9685 才進這一格（實際上 XL9555 / GT1151Q / HUSB238 也會被掃到）。
→ 新名 `_i2c_devices`；同時要改 UI 唯一的讀取點 `page/pca9685.py:116`。

---

## 1. 已定案的前提

### 1.1 緩衝：由 config 決定，本計劃**完全不碰**

幀長 = `Σ(Q×4)`；PCA9685 = `16 × 4 = 64 B`（**16 個 RGBW 槽，不是 16 B 的 0–255**），
硬體側才是每通道 2 B（`array('H')`，12-bit）。
→ 掃描不碰任何緩衝，這一節只是留著免得未來又誤解。

### 1.2 本輪不做

- 九種 type 的完整狀態查詢（已設計，見 §9 附錄）
- SPI 多線／SD 雙模式的語意回報（見 §9）
- 執行期 reinit、測試輸出、亮度、識別
- `init_pwm` 那個註解（PWM 未初始化）—— 另一件事

---

## 2. 現況：零件都在，缺接線

| 零件 | 現況 | 位置 |
|---|---|---|
| **掃描能力** | ✅ 早就有了 —— 但**實際只有 PCA 用到**（`xl9555` / `gt1151q` / `husb238` 三個 driver 有 `scan()`，卻**沒被 boot.py 呼叫**） | `driver/pca9685_drv.py:73,112` |
| **掃描重複** | ⚠️ 若開機掃一次、PCA 又自己掃 → **同一條匯流排掃兩次**；本計劃讓 PCA 改讀快取（§3.7） | 同上 |
| **儲存位置** | ⚠️ 位置存在、**命名錯**：`bus.shared["_pca_devices"]` → 改名 `_i2c_devices`（掃的是 I2C，不是 PCA）；UI 讀取點要同步改 | `ui/lvgl/page/pca9685.py:116` |
| **請求入口（UI）** | ✅ **已經寫好**：掃描按鈕 → `_post({"action":"scan"})` → `bus.shared["_pca_actions"]` | `page/pca9685.py:32-33,128-129` |
| **回覆做法** | ✅ **已經有**：`_reply()`（`hw_actions.py:8-17`）＋ `Proto` / `SchemaCodec`；WS、UART、ESP-NOW 三條都通 | `action/hw_actions.py` |
| **接線** | ❌ **缺**：沒有人「掃描 → 寫 `_i2c_devices`」；`_pca_actions` 沒有消費者；`HW_QUERY` 不認 I2C | — |
| **回覆指令** | ❌ `0x1402` 沒有對應的 RSP（`hw_actions.on_hw_query` 只 `print`） | `schema/hw.json` |

★ 所以工作量 = **一條接線 ＋ 一條 RSP ＋ 改名兩處 ＋ PCA 改讀快取（§3.7）**。
★ 舊名 `_pca_devices` **全 repo 沒有任何地方寫入**（`grep` 零命中）→ 所以 UI 的目標選擇永遠只有「廣播」。
★ `doc/03_notes/19_remote_control_plan.md:581` 也把 `_pca_actions` 列為「寫了沒人消費」的死碼。

---

## 3. 設計（三行就講完）

```python
# ① 掃描
devs = scan_i2c()                        # {0: [0x40, 0x70]}  逐 bus（PCA 需要知道在哪條匯流排）

# ② 存起來（同一格，只改名：_pca_devices → _i2c_devices）
bus.shared["_i2c_devices"] = devs        # UI 攤平後讀、PCA 直接讀 [i2c_idx]

# ③ 回覆（照正常做法，跟 SPEED_QUERY→SPEED_STATUS 一模一樣）
_reply(ctx, 0x1409, {"ok":1, "type":3, "id":0xFF, "json": "..."})
```

### 3.1 為什麼「不需要額外添加任何東西」

| 使用場景 | 怎麼拿結果 | 需要新增什麼 |
|---|---|---|
| **本地（UI / task / REPL）** | 讀 `bus.shared["_i2c_devices"]` | **無** —— 這就是你說的「存起來，直接讀」 |
| **遠端（PC / 遙控器）** | 收 `0x1409` | 一條 RSP 定義（因為 `0x1402` 本來沒回覆） |

- `_reply()` 本身已經有 `if "send" not in ctx: return` → **內部呼叫不會炸**，也不需要回。
- 所以**不需要**：新的交付機制、新的 key 家族、`dispatcher` 改動、跨核握手、仲裁。
  （v4 的「三個入口／兩個交付面」整個拿掉 —— 那是把「讀儲存格」講複雜了。）

### 3.2 儲存的內容與命名（定案：**per-bus**）

```python
bus.shared["_i2c_devices"] = {0: [0x40, 0x70]}    # {bus_idx: [addr_int, ...]}
```

★ **為什麼是 per-bus（不是合併成一個 flat list）—— 這是「PCA 直接讀清單」的前提**：
`pca9685_drv` 需要知道「這個位址在哪一條匯流排上」才能建 `PCA9685(i2c, addr)`。
合併去重會把 bus1 的位址餵給 bus0 的 driver → 對錯匯流排發。
（P4-ETH 目前只有一條 I2C，但設計不能假設只有一條。）

**讀取方式**：

| 誰 | 怎麼讀 |
|---|---|
| **driver（PCA）** | `devs.get(i2c_idx)` → 直接拿到該匯流排的清單 |
| **UI** | 攤平：`sorted({a for l in devs.values() for a in l})`（`_devices()` 那一行，本來就要改） |
| **協定回覆** | 逐 bus 列出（見 §3.3） |

- **名稱 = `_i2c_devices`**（不是 `_pca_devices`）——
  掃的是 I2C 匯流排，XL9555 / GT1151Q / HUSB238 也會被掃到，叫 PCA 會誤導。
- **要同步改的讀取點只有一個**：`ui/lvgl/page/pca9685.py:116` 的 `_devices()`（改名 ＋ 攤平）。
  （`temp/`、`ports/*/temp/` 底下有幾份同名舊檔，是歷史副本，不動。）
- UI 的 `_cycle_target()` 需要 int：`opts = [-1] + devs`、`"0x{:02X}".format(tgt)`（`page/pca9685.py:140-148`）。
- 這一格是「**最後一次掃描的結果**」—— 純快照，沒有時間戳、沒有狀態機。
- ★ 存的是「**匯流排回報的事實**」：`0x70`（ALLCALL）也會 ACK，所以會出現在清單裡。
  要排除是**消費端**的事（PCA driver 建 controller 時才排除 0x70）。

### 3.3 指令

| CMD | 名稱 | 方向 | Payload | 說明 |
|---|---|---|---|---|
| `0x1402` | `HW_QUERY` | M→S | `type(u8)` `id(u8)` | **payload 不變**；本輪只實作 `type=I2C(3)`、`id` = I2C 匯流排索引（`0xFF` = 全部） |
| `0x1409` | `HW_QUERY_RSP` | S→M | `ok(u8)` `type(u8)` `id(u8)` `json(str_u16len)` | 新回覆；`0x1401`~`0x1408` 後第一個空號 |

回覆內容（v7 簡化後就是位址本身）：

```json
{"0": ["0x40", "0x70"]}
```

- 用 JSON 是因為**清單長度不定**（與 `SYS_TASK_RSP` / `STATUS_RSP` 同風格）。
- `id` 只是回音（掃描一律掃全部已啟用的匯流排）；不回 `ms` / `trunc` / `aborted`。
- `type` 不是 I2C → 維持原狀（只印 log、不回覆），不擅自改變既有行為。

### 3.4 沒有超時上限（誠實記錄）

- 只掃 `I2C.list` 裡 `enable=1` 的匯流排。
- **`I2C.scan()` 是一次性阻塞呼叫，無法中途打斷** → 原本設計的 300 ms 預算在同 v7 拿掉
  （它也只能擋在「匯流排之間」，對單一匯流排無效）。
- 單一死線最壞 ≈ 112 位址 × I2C timeout（ESP32 預設 50 ms）≈ **5.6 s**，
  仍低於 `System.watchdog.timeout_ms`（預設 8000）→ 會卡住但不會重開機。
- 不做節流、不做快取 —— 每次問就每次掃。真的需要保護再調低 `I2C(timeout=…)`。

### 3.5 UI 按鈕（順手復活，一個 `if`）

`tasks/pixel_control_panel.py` 的 `loop()` 加一段消費 `_pca_actions`：

```python
q = bus.shared.get("_pca_actions")
if q:
    bus.shared["_pca_actions"] = None
    for ev in q:
        if ev.get("action") == "scan":
            bus.shared["_i2c_devices"] = scan_i2c()
```

★ 該任務已在 core0、所有裝置都註冊（`Core_Manager.py:153`），不用新 task。
★ **不在 LVGL 執行緒裡掃**（死線最壞數秒，會卡畫面）—— 走佇列，非阻塞。

> **為什麼 `_pca_actions` 不改名？**
> 它是「**某個 UI 頁面發出的請求佇列**」，而那個頁面就是 PCA 頁（同一個佇列還有 `start` / `stop`
> 兩個**真的是 PCA 專屬**的動作）。`_i2c_devices` 則是「**匯流排的事實**」，與頁面無關。
> 兩者抽象層級不同 —— 請求看頁面、結果看硬體。
> 若未來掃描鈕搬到「I2C 頁」，再把請求入口一般化即可（那時 `_pca_actions` 只剩 start/stop，名實相符）。

### 3.6 開機先掃一次（讓列表一開始就有內容）

```
i2c_drv.init_i2c()：建 bus → register_service("i2c_list")
                    ↓
                 scan_devices()          ← 掃一次 → bus.shared["_i2c_devices"]
                    ↓
                 之後每次 0x1402{type=I2C} 再呼叫同一支 scan_devices()
```

**放哪裡？→ `i2c_drv.init_i2c()`（driver 內），不是 `boot.py`。**

> 📌 **v6 修正**：原本計劃放 `boot.py`，使用者指出「這應該是 driver 的事」。實作後確認**使用者是對的**：

| 位置 | 評價 |
|---|---|
| **`i2c_drv.init_i2c()`**（✅ 採用） | ① 「掃我自己這條 bus」是 driver 職責，不是裝配層政策；② **所有 port 自動都有** —— port 的 `boot.py` 是**整檔 delta 覆蓋**，寫在 boot 就得每個 port 手動同步（實作時真的要去改 `ports/P4/ESP32-P4-ETH_mp3/boot.py`）；③ **順序有保證**：掃描一定在「建 bus 之後、任何讀快取的 driver（PCA）之前」，不會因為有人重排 `_init()` 而讓 PCA 退回自己掃 |
| `boot.py`（❌ 原本計劃） | 會讓 port delta 漏掉這個功能；而且「開機掃一次」不是政策問題 —— 沒有 bus 就沒有東西可掃，本來就跟著 bus 走 |
| `App.__init__` | 太晚（`boot.py` 才是硬體階段），而且 App 不該碰硬體 |

```python
# i2c_drv.py —— 全部就是這樣（12 行）
def scan_devices(sysbus=None):
    sysbus = sysbus or bus
    out = {}
    for i, io in enumerate(sysbus.get_service("i2c_list") or []):
        try:
            out[i] = io.scan()
        except Exception as e:
            print("[I2C] bus {} scan error: {}".format(i, e))
            out[i] = []
    sysbus.shared["_i2c_devices"] = out
    return out

# init_i2c() 尾端：
    sysbus.register_service("i2c_list", i2c_list)
    d = scan_devices(sysbus)
    print("[I2C] devices: {}".format({k: [hex(a) for a in v] for k, v in d.items()}))
```

★ **失敗不擋開機**：單一匯流排掃壞只記 `[]`，bus 照樣可用（其他人仍可 `scan()`）。
★ `I2C.enable=0` → `init_i2c` 直接 return，連掃都不掃（沒有 bus 可掃）。
★ 掃到空也要寫（`{}` / `{0: []}`）—— 讓「掃過了、沒有裝置」和「沒掃過」分得開。

### 3.7 ★ PCA 初始化改成「讀清單」，不再自己掃（消除重複掃描）

**現況查核**（這是使用者指出的重點）：

| 位置 | 現況 |
|---|---|
| `pca9685_drv.py:73`（`_scan_and_register`，`address:["0xFF"]` 時走） | `found = [a for a in i2c.scan() if a != 0x70]` |
| `pca9685_drv.py:112`（`init_pca9685` 沒給 address 時的 fallback） | 同樣再掃一次 |

→ P4-ETH 的 config 就是 `"address": ["0xFF"]`，所以**開機時 PCA 一定會掃**。
若 §3.6 的開機掃描也做一次，就是**同一條匯流排掃兩次**（都是 ms 級，但沒必要）。

**改法**：PCA 直接吃 `_i2c_devices[i2c_idx]`。

```python
# pca9685_drv.py —— 全部就這 3 行
def _found_on_bus(sysbus, i2c_idx):
    """該匯流排上掃到的位址（**含 0x70**）。快取不存在 → []。"""
    devs = sysbus.shared.get("_i2c_devices") or {}
    return list(devs.get(i2c_idx) or [])

# _scan_and_register() 與 init_pca9685() 的 fallback 都改成：
    found = [a for a in _found_on_bus(sysbus, i2c_idx) if a != _PCA_ALL_CALL]
```

- ★ **0x70 的過濾留在 driver**：快取是「匯流排事實」（含 0x70），
  PCA 建 controller 前才 `if a != 0x70` —— 不然把「事實」也過濾掉，以後查不到 0x70 為何沒回應。
- ★ **不需要 fallback 自己掃**：`scan_devices()` 在 `init_i2c()` 就跑完了，
  PCA 一定在它之後（同一個 boot Phase）；而且 `I2C.enable=0` 時 PCA 也拿不到 `i2c_list`。
- ★ **順序有保證**：掃描在 `init_i2c()` 內，PCA 是後面的 `_init("pca9685")` —— 不靠註解提醒。

**這個改法的價值**：

1. 同一條匯流排**只掃一次**；
2. **UI 看到的清單 = PCA 用的清單**（不會出現「UI 顯示 0x40，但 PCA 沒接上」這種兩個真相）；
3. 之後要診斷「為什麼 PCA 沒起來」，只要看 `_i2c_devices` 就夠。

> ⚠️ **快取是開機時的快照**：之後插上的裝置，查詢會更新 `_i2c_devices`，
> 但 **PCA 的 controller 不會因此重新註冊**（那是 §8 不做／§9 的 reinit）。
> 所以「熱插拔後要能用」仍需要重開機或未來的 reinit。

> 📌 **順帶查核**：`xl9555_drv` / `gt1151q_drv` / `husb238_drv` 也有 `i2c.scan()`，
> 但**這三個 driver 在 `boot.py` 裡根本沒有被呼叫**（`grep init_xl9555` 等零命中）。
> 所以現階段「重複掃描」的實際來源**只有 PCA 一處**；那三個要不要接上、要不要也吃快取，是另一件事。

---

## 4. 分階段

| 階段 | 內容 | 狀態 |
|---|---|---|
| **P0** 定案 | §3.2 存的內容、§3.3 指令 | ✅ |
| **P1** 掃描函式 | `driver/i2c_drv.scan_devices()`（**12 行**；v7 已把原本的 lib 刪掉） | ✅ |
| **P2** 接線 | ① `init_i2c()` 尾端掃一次；② **PCA 改讀快取**；③ `0x1409` schema；④ `on_hw_query` 的 I2C 分支；⑤ `pixel_control_panel` 消費 `_pca_actions`；⑥ `page/pca9685.py` 改名＋攤平 | ✅ 上板驗收待做 |
| **P3** 收尾 | 指令索引、`19_remote_control_plan.md` §9.5、`todo/10` | ✅ |

> 上板驗收清單在 `todo/10_i2c_scan.md`（冷開機就有清單／只掃一次／回 `0x1409`／UI 目標可選／拔掉 PCA 變空）。

---

## 5. 風險與上板必驗

| # | 風險 | 對策 |
|---|---|---|
| 1 | **I2C 拉死 → `scan()` 卡住 ~5.6s** | §3.4：`scan()` 無法中斷；最壞仍 < 8s WDT（不會重開機） |
| 2 | **ESP-NOW 250 B**：127 個位址 ≈ 762 B | 截斷 `found` 尾端、標 `trunc:1` ＋ 保留 `count`；或分 bus 問 |
| 3 | 多條 I2C 的歸屬 | **本輪就存 per-bus**（`{bus: [addr]}`）→ 不會失去歸屬；PCA 才能直接讀（§3.7） |
| 4 | UI 在 LVGL 執行緒掃描會卡 | §3.5：走 `_pca_actions` 佇列 |
| 5 | 寫 `_i2c_devices` 的型別錯（hex 字串 / 攤平沒做） | P1 測試釘住「`{bus: [int]}`」；UI 的攤平與 `0x{:02X}` 用法 |
| 6 | 舊值殘留（掃到空時沒清） | 掃到空也要**寫入 `{}`**（覆蓋），不能什麼都不做 |
| 7 | **改名漏改**：程式寫 `_i2c_devices`、UI 還在讀 `_pca_devices` → 看起來「掃不到」 | §3.2 的唯一讀取點 `page/pca9685.py:116` 必須同一批改 |
| 8 | 把 `0x70`（ALLCALL）當成一般裝置 | §3.2／§3.7：**快取存事實、不過濾**；PCA 建 controller 前才排除 |
| 9 | **開機掃描失敗擋住開機** | §3.6：獨立 try/except，只 print；清單空著不影響任何功能 |
| 10 | 開機掃描拖慢開機（死線最多 ~5.6s） | 已知限制；真的在意再調低 `I2C.timeout`（本輪不做） |
| 11 | **PCA 讀到空快取 → 靜默不建 controller** | §3.7：`i2c_idx not in devs` 才 fallback 自己掃；空 list = 真的沒裝置 |
| 12 | **順序錯**（PCA 比 i2c 先 init）→ 讀不到快取 | §3.6／§3.7：掃描在 `init_i2c()` 內、PCA 是後面的 `_init("pca9685")`；上板必驗「開機 log 只有一次掃描」 |

**上板必驗**

- [ ] **開機就有**：冷開機後**什麼都不按**，直接讀 `bus.shared["_i2c_devices"]` → `{0: [0x40, 0x70, …]}`
- [ ] **只掃一次**：開機 log 只看到一次 I2C 掃描（PCA 是讀快取，不是自己再掃）
- [ ] **PCA 確實吃到快取**：暫時讓 `i2c.scan()` 在 PCA 段不被呼叫（或加 log）→ PCA 仍能建出 controller
- [ ] **查詢會更新**：插上／拔掉 PCA 後發一次 `0x1402{type:3}` → 同一格**跟著變**（不是舊值）
- [ ] 開機時把 I2C 拔掉（或短路 SDA）→ 裝置**仍能開機**，`_i2c_devices == {}`（PCA 走 fallback、不炸）
- [ ] P4-ETH：按 LVGL 掃描鈕 → 攤平後含 `0x40`、`0x70`；目標可切到 `0x40`
- [ ] P4-ETH：遠端 `0x1402{type:3,id:0}` → 收到 `0x1409`，`found` 與本地那一格一致
- [ ] **改名驗證**：`bus.shared` 裡**沒有** `_pca_devices` 這個 key（`"_pca_devices" in bus.shared` → `False`）
- [ ] 拔掉 PCA 再掃 → `_i2c_devices == {0: []}`（**不是保留舊值**）
- [ ] ESP-NOW 通道問一次 → 不崩、`trunc` 正確
- [ ] `type:0`（PIN）→ `ok=3`（未支援），不亂回

---

## 6. 要一併更新的文件

| 文件 | 改什麼 |
|---|---|
| `slave/driver/i2c_drv.py` | **掃描放這裡**（§3.6）：`scan_devices()`（12 行）＋ `init_i2c()` 尾端呼叫，失敗只記 `[]` |
| `slave/schema/hw.json` | 新增 `0x1409 HW_QUERY_RSP`（`0x1402` payload 不動） |
| `slave/action/hw_actions.py` | `on_hw_query()` 加 I2C 分支（**再掃** → 覆蓋 `_i2c_devices` → `_reply`） |
| `slave/driver/pca9685_drv.py:73,112` | **改讀快取**（§3.7）：不再自己 `i2c.scan()`，改讀 `_i2c_devices[i2c_idx]`，保留 fallback |
| `slave/ui/lvgl/page/pca9685.py:116` | **改名 ＋ 攤平**：`_devices()` 從 `{bus:[addr]}` 攤成 `list[int]` |
| `slave/tasks/pixel_control_panel.py` | 消費 `_pca_actions`（§3.5） |
| `doc/01_protocol/02_command_index.md` | `0x14xx` 補 `0x1409`；`0x1402` 標「I2C 掃描已實作」；112 → 113 條 |
| `doc/03_notes/19_remote_control_plan.md` §9.5 | `_pca_actions` 從死碼表移除（`_i2c_devices` 是新 key，順便寫進這份清單避免再變死碼） |
| `doc/03_notes/18_pixel_panel_control_path.md` | 補「掃描 = `i2c.scan()` → `_i2c_devices`；UI 讀那一格」 |
| `Skills/mp-netcore/SKILL.md` | 補「掃描結果放哪（`_i2c_devices`）」＋ `0x1409` |
| `doc/README.md` / `README.md` | 索引描述更新為 v5 |

---

## 7. 決策（已定案）

| # | 問題 | 定案 |
|---|---|---|
| **D1** | 儲存格命名？ | ✅ **`_i2c_devices`**（掃的是 I2C，不是 PCA）；`page/pca9685.py` 已同步改 |
| **D2** | 多條 I2C 的儲存形狀？ | ✅ **per-bus `{bus: [addr]}`** —— PCA 讀 `devs[i2c_idx]`；UI 自己攤平（9 行） |
| **D3** | 300 ms 中止？ | ❌ **v7 拿掉**（`scan()` 無法中斷，留著只是假保護）；限制見 §3.4／§10.3 |

> 開機掃描的位置定案為 **`boot.py`**（§3.6）。

---

## 8. 本輪**不做**

- 九種 type 的完整狀態查詢（§9 附錄，已設計）
- 執行期 reinit／熱套用（前提是「拓樸會變」，已定案由 config 決定）
- 測試輸出／亮度／識別（要由 RenderTask 出幀，單一 writer）
- `init_pwm` 的註解（PWM 未初始化 → `HW_CTL{PWM}` 失效，另一件事）

---

## 9. 附錄 — 完整狀態查詢設計（已設計、本輪不做，未來接同一條指令）

> 這一節保留 v4 的設計。等 §3 的 I2C 掃描上板穩定後，其餘 type 用**同一個 `0x1402` / `0x1409`** 慢慢加，
> 不必再開新指令。

### 9.1 `0x14xx` 的四件事

| # | 動作 | 指令 | 狀態 |
|---|---|---|---|
| 1 | 設值 | `0x1401 HW_CTL` | ✅ |
| 2 | 設速 | `0x1403 SPEED_SET` | ✅ |
| 3 | 查速 | `0x1407` → `0x1408` | ✅ |
| 4 | **查狀態** | `0x1402` → `0x1409` | 本輪只做 I2C；其餘見下 |

### 9.2 統一的回覆形狀（未來每種 type 都用它）

```json
{"ok":1, "t":3, "id":0, "name":"i2c", "ms":3,
 "cfg":{"id":1,"freq":100000,"scl":20,"sda":21},          ← 設定值（速度、腳位）
 "now":{"found":["0x40","0x70"],"guess":"pca9685"}}        ← 當前值（讀不到就 null）
```

- `id=0xFF` → 該類型全部；`type=0xFF` → 全部類型（ESP-NOW 請單獨問一種）。
- **`cfg` 回的是「解析後的語意」，不是 config 原文**；有設但沒生效的欄位放 `ignored:[...]`。
- 執行期「速度」多半讀不到（`machine.I2C/UART/SPI` 都沒有 getter）→ 速度只放 `cfg`；
  有 getter 的才放 `now`（`Pin.value()`、`PWM.freq()/duty()`、`UART.any()`、`I2C.scan()`）。

### 9.3 九種 type 的欄位

| type | 名稱 | `cfg`（解析後） | `now` |
|---|---|---|---|
| `0` | PIN | `gpio` `label` `dir` `pull` `initial` | `value`；`edges`（IN，**唯讀不消費**） |
| `1` | PWM | `gpio` | `freq` `duty`；沒初始化 → `null` ＋ `not_initialized` |
| `2` | SPI（多線） | `id` `mode` `freq` `sck` `data[]` `lanes` `phase` `polarity` `ignored[]` | `present` `kind` |
| `3` | **I2C** | `id` `freq` `scl` `sda` | **`found`（現場掃描）** `guess` `ms` `trunc` |
| `4` | PIXEL | `g` `q` `order` `stay` | `fs` `present` |
| `5` | LCD | `driver` `width` `height` `rotation` | `present` |
| `6` | SD | `phat` `mode` `slot` `width` `freq` `sck` `cmd` `data[]` `lanes` `ignored[]` | `mounted` `free` |
| `7` | UART | `id` `baudrate` `rxbuf` `txbuf` `tx` `rx` `en` | `present` `rs485` `any` |
| `8` | VBTN | `bytes` `active_low` | `pressed`（**正邏輯**）`raw` |

**`present` 語意**：`1` 確認在線（I2C ACK）／`0` 確認不在／`-1` 無法驗證（WS2812/APA102 單向）。

### 9.4 SPI 多線（未來要回報時照這個）

| 實際走法 | 觸發條件 | `data` | 生效欄位 |
|---|---|---|---|
| `lcd_bus.SPIBus`（多線） | lcd_bus 可用且 `force_machine != 1` | `data_pins`（空 → 退 `mosi`） | `sck` `freq` `host` **`data[]`** |
| `machine.SPI`（單線） | `force_machine=1` 或 lcd_bus 失敗 | `data_pins[0]` 當 `mosi` | `baudrate` `phase` `polarity` `sck` `mosi` `miso` |

★ **`phase`/`polarity` 在 lcd_bus 模式不生效** → `ignored:["phase","polarity"]`。
★ `data_pins: []` ＋ `mosi: 7` → 實際**單線**（`d=(mosi,)`）。

### 9.5 SD 雙模式（未來要回報時照這個）

| `slot` | 模式 | `machine.SDCard` | 不生效 |
|---|---|---|---|
| `< 2` | SDIO/SDMMC | `slot width sck cmd data[] freq` | — |
| `>= 2` | SPI | `slot sck mosi=cmd miso=data[0] cs=data[3] freq` | **`width`**、`data[1..2]` |

★ `GPIO.cmd` 兩種身分（SDIO=CMD、SPI=MOSI）；`data[]` 就是 io list，`-1` = 未用。

### 9.6 三個入口（為何內部不需要通訊）

| 入口 | `ctx["send"]` | handler 回傳值 | 結果怎麼拿 |
|---|---|---|---|
| `disp.exec_cmd(cmd,args,ctx)` | **沒有**（UI 的 `_ctx()`） | **被丟棄** | 讀 `bus.shared` |
| `vBus.inject(frame)` | 有（`_TxOut`） | — | `0x1409` 走路由 |
| `dispatch(bytes,ctx)` | 有 | — | `0x1409` 送回來源 |

`signal_router.py:43-46`：「程式內部直呼 handler 請用 `exec_cmd`，語意是『我呼叫一個函式』。」
→ 本計劃的實作正好符合：**掃描結果存一格，本地直接讀**，回覆只是給外部的順便。

### 9.7 其他已知坑（未來會用到）

- `boot.py:132` 的 `# _init("pwm", init_pwm)` 是註解 → `pwm_list` 不存在 → PWM 全鏈路失效。
- **`xl9555_drv` / `gt1151q_drv` / `husb238_drv` 從來沒被 `boot.py` 呼叫**（`grep init_xl9555` 等零命中）
  → 三個 driver 與它們的 `i2c.scan()` 都是死碼；`tft_drv` 想取 `"xl9555"` 服務也取不到。
  本計劃不動它們，但這也是「重複掃描」只有 PCA 一處的原因。
- provider 清單**零個硬體狀態**（`audio_*`/`stream_*`/`ips`/`fs_scan_busy`）→ `0x1101` 查不到硬體。
- `vbtn` 是 active-low（0=按下）；協議層要翻正。
- 遙控器未認主時，`_reply()` 寄 `master_cid` 會收不到 → 需要 `reply_cid`（照 `0x100D` 的做法）。

---

## 10. 實作結果（2026-10-17）

### 10.1 改動清單（v7 簡化後）

| 檔案 | 內容 |
|---|---|
| `slave/driver/i2c_drv.py` | **唯一新增邏輯**：`scan_devices()`（12 行）＋ `init_i2c()` 尾端呼叫一次。失敗只記 `[]` |
| `slave/driver/pca9685_drv.py` | 兩處 `i2c.scan()` → `_found_on_bus(sysbus, i2c_idx)`（3 行）；0x70 過濾留在 driver |
| `slave/schema/hw.json` | 新增 `0x1409 HW_QUERY_RSP`（`ok/type/id/json`）；`0x1402` payload **不變** |
| `slave/action/hw_actions.py` | `on_hw_query()` 的 I2C 分支：`scan_devices()` → `_reply(0x1409)`（+ `import json`） |
| `slave/tasks/pixel_control_panel.py` | `_pca_actions` 的 `scan` → `scan_devices()`（`start`/`stop` 仍不支援） |
| `slave/ui/lvgl/page/pca9685.py` | `_devices()` 改讀 `_i2c_devices`（攤平 per-bus，9 行） |
| `slave/boot.py` / `ports/…/ESP32-P4-ETH_mp3/boot.py` | 只留一行註解指向 driver（不在 boot 掃描） |

> **刪除**：`slave/lib/sys/i2c_scan.py`（約 200 行）與 `test/protocol/test_i2c_scan.py`。
> v7 拿掉了 `detail` / `ms` / `aborted` / 預算 / `scan_fn` 注入 / `cached_bus_devices` / `flat_devices` / `detail_to_body`。

### 10.2 驗證

- **stub 整合驗證**（machine/time stub，沒有進 repo）：
  ① `init_i2c()` → `{0: [0x40, 0x70]}`；② 掃描失敗 → `{0: []}`、bus 仍建立；
  ③ `I2C.enable=0` → 回 `[]`、不掃；④ `scan_devices()` 可獨立呼叫；
  ⑤ `on_hw_query({type:3})` → 回 `0x1409`、`json == {"0": ["0x40", "0x70"]}`、封包 **41 B**；
  ⑥ 內部呼叫（ctx 無 `send`）不送包、但快取更新。
- `SchemaStore` 載入 **113 條**（112 ＋ 0x1409）。
- 全部改動檔案 `compile()` 通過；`hw.json` 合法 JSON。
- **沒有離線單元測試**（v7 隨 lib 一起刪除）—— 驗證靠上面的 stub 與上板。

### 10.3 與計劃的差異（誠實記錄）

1. **v7 大幅簡化（使用者指示）**：原本做了 `lib/sys/i2c_scan.py` + 17 個離線用例，
   對「拿 i2c 物件、掃、寫一格」這件事是過度設計。現在只有 `scan_devices()` 12 行，
   連 `scan_fn` 注入都沒有 —— 要測就在 REPL 直接呼叫。
2. **沒有 300 ms 上限**：`I2C.scan()` 無法中途打斷，原本的「預算」也只能擋在匯流排之間，
   乾脆拿掉。單一死線最壞 ≈ 112 位址 × I2C timeout ≈ 5.6s（< 8s WDT）。
3. **非 I2C 的 type 不回 `ok=3`**：維持原狀（只印 log），不擅自新增回覆封包。
4. **PCA 沒有 fallback 自己掃**：`scan_devices()` 在 `init_i2c()` 內就跑完，
   PCA 一定在它之後；`I2C.enable=0` 時 PCA 也拿不到 `i2c_list`。
5. **開機掃描放 `i2c_drv`（使用者指正）**，不是 `boot.py`：
   port 的 `boot.py` 是整檔 delta 覆蓋，寫在 boot 就得每個 port 手動同步；
   放 driver 後順序天然正確（建 bus → 掃 → PCA 讀）。

### 10.4 待上板驗收

見 `todo/10_i2c_scan.md`。最關鍵三條：
① 冷開機後什麼都不按就有清單；② 開機 log 只有一次掃描；③ 拔掉 PCA 後查詢 → 清單變空。

---

## 相關文件

- `todo/10_i2c_scan.md` — 上板驗收清單與已知限制
- `19_remote_control_plan.md` — §9.5 死碼清單（`_pca_actions` 已解）
- `18_pixel_panel_control_path.md` — 面板的 pixel 控制路徑
- `01_protocol/02_command_index.md` — 指令索引（`0x1402` I2C 分支 ＋ `0x1409`）
