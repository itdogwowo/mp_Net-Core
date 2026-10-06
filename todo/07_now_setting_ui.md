# ESP-NOW 設定 ＋ 遙控器設定 兩頁 UI 計劃書

> **用途**：把目前擠在 `remote.py` 一頁的東西**拆成兩頁** —— 一頁管**傳輸層**（射頻怎麼出去），
> 一頁管**應用層**（要操作誰、播什麼）。兩層的數量限制、持久化位置、成敗條件都不同，
> 混在一頁會讓「這個清單到底在限制什麼」永遠說不清楚。
> **狀態**：🟢 **兩頁已完成並上板驗證**（`build_all: 7 screen(s)`、掃描/套用/清除真機跑過）
> **最後更新**：2026-10
> **相關**：
> - `todo/05_node_pairing.md` §12 —— `0x1016` 認主流程（真機驗證完成）
> - `doc/03_notes/01_changelog.md` §33 / §34 / §35 —— 頻道執行期可換、ESP-NOW 設定資料層、兩頁拆分
> - `doc/02_guides/16_signal_router.md` —— `self` / `vbus` 兩個本機入口

---

## 0. 一句話

**兩頁管的是兩件不同的事：**

```
ESP-NOW 設定頁  =  「這顆無線電要對誰講話」  → MAC、頻道、加密、20/6 格硬體限制
遙控器設定頁    =  「我要操作誰、播什麼」    → cid、模式清單、播放清單，**無數量限制**
```

⚠️ 最容易搞混的是**兩個清單的鍵不一樣**：

| | 鍵 | 來源 | 上限 |
|---|---|---|---|
| ESP-NOW 已選 | **MAC**（射頻層）| 掃描到的 radio peer | **19**（一般）／**6**（加密）|
| 遙控器已選 | **cid / slave_id**（協議層）| `PeerRegistry`（任何總線）| **無** |

---

## 1. 為什麼要拆（而不是把 `remote.py` 加幾個按鈕）

`remote.py` 目前同時有「ESP-NOW 開關」與「節點清單 ＋ 模式表」。這造成三個具體問題：

1. **限制說不清楚** —— 「已選 3/19」是射頻限制，但使用者以為是「我最多能控 3 台」。
   遙控器明明沒有這個限制（UART/WS 的節點不受 ESP-NOW 的 20 格約束）。
2. **FF（廣播）語意衝突** —— 射頻層的 FF 是「不指定對象，所有人收得到」；
   應用層的「全選」是「我挑的每一台都送」。兩者混在一張表裡必然誤解。
3. **持久化位置不同** —— 射頻清單是**這台裝置的無線電設定**（`@now.*`）；
   遙控器清單是**我要控制誰**（`@node.targets`）。混在一起會變成一次「儲存」寫兩份。

---

## 2. 頁 A：ESP-NOW 設定（傳輸層）

### 2.1 佈局（320×240 橫屏；沿用 `settings.py` 的卡片風格）

```
┌ ESP-NOW 設定 ─────────────────────────────────┐
│ ┌─ 啟用 ────────┐ ┌─ 頻道 ──┐ ┌─ 加密 ──────┐ │
│ │ [●]           │ │ ◀  6  ▶ │ │ [ ]         │ │
│ └───────────────┘ └─────────┘ └─────────────┘ │
│ ┌─ 可選 (n) ───────┐ ┌─ 已選 (m/19) ─────────┐│
│ │ FF  廣播          │ │ FF  廣播      ←預設   ││
│ │ AA:BB:CC:..       │ │ AA:BB:CC:..           ││
│ │ 11:22:33:..       │ │                       ││
│ └───────────────────┘ └───────────────────────┘│
│ [掃描] [加入 ▸] [◂ 移除] [套用] [清除所有記錄] │
└───────────────────────────────────────────────┘
```

### 2.2 語意（使用者定案）

| 項目 | 規則 |
|---|---|
| **FF 與具體節點互斥** | 已選裡有節點 → FF 自動退下；節點全部移除 → FF 自動回來 |
| **預設** | 可選＝掃描到的全部；已選＝**只有 FF** |
| **上限** | 一般 **19**（20 格扣掉廣播）；**加密 6**。滿了**不自動淘汰**，直接拒絕並提示「請先移除一些」|
| **清除所有記錄** | **只清 ESP-NOW 的**：硬體 peer 表 ＋ 已選清單。`@peer.*`（節點記錄）與 `@node.*`（配對方向）**不動** |
| **加密金鑰** | **全體共用一組** PMK/LMK，存 btree（`@now.pmk` / `@now.lmk`）。**不進 `config.json`** |
| **加密沒有金鑰** | **不給開**（`encrypt=True` 硬體收得下，但對端解不開 → 什麼都收不到）|

### 2.3 資料流

```
UI 動作                     呼叫                                   落地
─────────────────────────────────────────────────────────────────────────
掃描                        廣播 0x100D{reply_cid, timeout_ms}    PeerRegistry（被動學習）
                            收 0x100E → learn_from_identify_rsp
加入／移除（改已選清單）     只改 UI 狀態                          —
套用                        NowBus.apply_peers(已選 MAC 清單)      硬體 peer 表
                            成功後 ConfigManager.save_now(...)     @now.selected（btree）
啟用／關閉                   exec_cmd(0x1301{action:1|2})          Network.ESP_now.enable
頻道                        exec_cmd(0x1301{action:3, channel:N}) —
加密開關                    NowBus.set_encrypt(on, pmk, lmk)      @now.encrypt/pmk/lmk
清除所有記錄                NowBus.clear_peers() + cfg.clear_now() @now.* 刪除
```

★ **「套用」是唯一會動硬體 peer 表的按鈕** —— 掃描只累積「可選」，不偷偷佔格子。
這是刻意的：`poll()` 會對每個講過話的 MAC 被動學習（因為 MAC 無法枚舉），
那些是**雜訊**，不該在使用者決定之前就吃掉 20 格裡的位子。

### 2.4 已完成（資料層，`153/153 PASS`）

| 東西 | 位置 |
|---|---|
| `set_encrypt(on, pmk, lmk)` | `lib/sys/now_bus.py` |
| `peer_cap()`（19／6） | 同上 |
| `clear_peers()` | 同上 |
| `apply_peers(keep)` | 同上（上限檢查、廣播保留）|
| `add_peer(mac, encrypt=None)` | 同上（**廣播一律不加密**）|
| `set_channel(n)` ＋ `0x1301{action:3,channel}` | 同上 ＋ `action/now_actions.py`（**真機 10/10 驗過**）|
| `now_state / load_now / save_now / clear_now` | `lib/sys/ConfigManager.py`（`@now.*`）|

### 2.5 已完成

- [x] `ui/lvgl/page/now_setting.py`：**頁面本體**（`@register(id="now_setting", icon="sensors")`）
- [x] `ui/lvgl/page/__init__.py` 加 try-import ＋ `_PAGES_MOD`
- [x] 「可選」清單的來源：`bus.shared["peers"]`（`PeerRegistry.snapshot()`，只收有 `mac` 的）
- [x] 把 `remote.py` 的 ESP-NOW **與 Wi-Fi** 開關移除（見下方「順手修掉的排版 bug」）
- [ ] 金鑰輸入：LVGL 沒有鍵盤 → **從網頁 UI 或 `/now_keys.json` 匯入**（下一步再定）
      —— 在那之前，加密開關在沒有 `@now.pmk` / `@now.lmk` 時會**拒絕開啟**並提示

### 2.6 順手修掉的排版 bug（`remote.py`）

原本兩顆開關擺在 `(10, 224)` 與 `(168, 224)`、標籤擺在 `x=34` / `x=192`：

```
mk_switch() 的實寸是 44×24（ui_common.py:251）
  _wifi_sw  x ∈ [10, 54]   ← 標籤 x=34 落在裡面 → 文字壓在開關上
  _now_sw   x ∈ [168, 212] ← 標籤 x=192 落在裡面 → 同上
  y=224，224+24 = 248 > 240（螢幕高）→ 下緣被切掉 8px
```

移除後把空出來的 42px 還給內容：左欄清單 `h 168→180`、`c3` 面板 `h 42→58`、
底列按鈕 `y 198→210`。`c3` 多出來的一行放「總線」欄（見 §2.7）。

### 2.7 `c3` 的裸數字（使用者回報）

`_lb["sel2"]` 原本印 `"{}ms".format(age)` —— 只有數字加單位，**沒有主詞也沒有方向**，
看的人分不出那是「多久沒聽到它」還是「它回得多快」。`age_ms` 的定義是
**距離最後一次收到它的時間**。改成三行、每行都有標籤：

```
sel  : 0x0002  test-peer        ← cID + slave_id
sel2 : 最後 1234ms 前  ●在線     ← 時間 + 在線判準（RECENT_MS = 10s）
sel3 : 總線 NOW-Bus             ← 從哪條管子聽到的
```

---

## 3. 頁 B：遙控器設定（應用層，無上限）

### 3.1 佈局

```
┌ 遙控器設定 ───────────────────────────────────┐
│ ┌─ 可操作 Slave（任何總線）────────────────┐  │
│ │ ☑ 0001  s3-cpanel   [now]                │  │
│ │ ☑ 0002  slave-b     [uart0]              │  │
│ │ ☐ 0003  node-c      [net]                │  │
│ └──────────────────────────────────────────┘  │
│ [掃描節點] [掃描模式 ▸]  ← 對 ☑ 的逐一查       │
│ ┌─ 模式播放清單 ───────────────────────────┐  │
│ │ 0001 / gundam_demo                       │  │
│ │ 0002 / matrix_wave                       │  │
│ └──────────────────────────────────────────┘  │
│ [加入 ▸] [◂ 移除] [▶ 交給 Pixel 控制器]        │
└───────────────────────────────────────────────┘
```

### 3.2 語意

| 項目 | 規則 |
|---|---|
| **數量** | **無上限**（UART/WS 的節點不受 ESP-NOW 的 20 格約束）|
| **Slave 來源** | `PeerRegistry`（**已經有** `via` / `ifaces` 標來源總線）|
| **掃描節點** | 廣播 `0x100D`（同頻道、任何總線上的都收得到就登記）|
| **掃描模式** | 對每個勾選的 Slave **逐一**送 `0x3101 MODE_LIST_QUERY`，收 `0x3102 MODE_LIST_RSP` |
| **交給 Pixel 控制器** | `bus.shared["_pixel_cmd"] = {"mode": 16-bit id}`（本機）或廣播 `0x3105 MODE_SET`（對 Slave）|

### 3.3 「逐一」的節流

`remote.py` 的註解已經寫了：**「取得細節（逐一，節流由接收端處理）」**。
掃描模式同理 —— **一次只對一台發**，收到回覆（或逾時）才換下一台。
理由：廣播出去讓 N 台同時回話就是 `0x100D` 當初撞車的那個問題（見 `todo/05` C2）。
`0x3101` 是**定向**的，所以逐一發不會撞；但同時對 N 台發，回覆仍會在射頻上互撞。

### 3.4 已完成

- [x] `remote.py` 移除全部 ESP-NOW 相關（開關、`_toggle_now`、`_sync_now_switch`）
- [x] `remote.py` 移除 **Wi-Fi** 開關（與 `settings.py` 重複的那一份）
- [x] 節點清單來源：`bus.shared["peers"]`，`ifaces` 顯示在 `c3` 的「總線」行
- [ ] 模式清單彙整：把多台的 `0x3102` 結果合成一張帶來源的清單（目前只有單一來源）
- [ ] 與 `pixel_controller.py` 的介面確認（`gmode.mode_pool()` vs 遠端清單）

### 3.5 ★ 關於 `remote_setting.py`：決定**不另開**這一頁

§3.1 原先規劃再開一個 `ui/lvgl/page/remote_setting.py`。實作時判定**不需要**，
理由：`remote.py` **本身就是應用層那一頁**（節點清單、綁定、模式表），
而 §3.4 也已經寫明「`remote.py` 只留模式表/綁定（移除 ESP-NOW 相關）」。
兩者描述的是同一頁，再拆一次只會多一個空殼。

所以「兩頁」的實際落點是：

```
頁 A  now_setting.py  ← 傳輸層：這顆無線電要對誰講話（MAC/頻道/加密）
頁 B  remote.py       ← 應用層：我要操作誰、播什麼（cid/模式表）  ← 已經存在，只是變乾淨
```

若之後「模式播放清單」（多來源彙整）真的長出獨立狀態，再從 `remote.py` 分出去。

---

## 4. 現有可用的指令（不用新造）

| 指令 | 用途 |
|---|---|
| `0x100D/0x100E` | 點名／回覆（帶 `reply_cid` ＋ `timeout_ms` 抖動）|
| `0x1016` | 設定／解除方向（`master_cid=0xFFFF` ＝ 解除）|
| `0x1301` | ESP-NOW 開／關／**設頻道**（`action=3`，2026-10 新增）|
| `0x3101/0x3102` | **模式清單**查詢／回覆 ← 「掃描模式」用這個 |
| `0x3105` | `MODE_SET` ← 「交給 Pixel 控制器」|
| `0x3107/0x3108` | 模式細節 |
| `0x1101/0x1102` | `STATUS_GET`（**空中查對方狀態**，真機 20~30ms）|

---

## 5. 待驗證（板子回來後第一件事）

### 5.1 這一輪已經驗過的（2026-10，實機 11401）

| 項目 | 結果 |
|---|---|
| 頁面註冊 ＋ `build()` 真的跑得起來 | ✅ 開機 log `[app] build_all: 7 screen(s)`（原 6），無 `import skip` |
| `registry.ordered()` | ✅ `[remote 0, control_panel 1, now_setting 2, pixel_controller 2, pca9685 2, settings 3]` |
| 顯示格式 `_short` / `_full` / `_cid_txt` | ✅ 含 `cid=0` 不被誤判成「不知道」 |
| 掃描（真的廣播 `0x100D`） | ✅ 收到 `2C:65:B8 → 0x0002 [NOW-Bus]`（對板 11201） |
| 套用 `apply_peers([mac])` | ✅ `(1, 0, 0)`，peer 1→2，**廣播格保留** |
| 套用空清單 `apply_peers([])` | ✅ `(0, 1, 0)` → 只剩廣播（＝「已選＝只有 FF」的預設語意） |
| 清除 `clear_peers()` ＋ `clear_now()` | ✅ 移除 1 筆；`@now.selected` → `[]`、`@now.encrypt` → `0` |
| 清除**不越界** | ✅ `@node.cid` / `@node.master_cid` 不變、`@peer.*` 節點記錄不變 |
| `_radio_on()` / `_flag_on()` / `_channel()` / `_cap()` | ✅ `True / True / 6 / 19` |

⚠️ **沒驗到的**：「可選 ↔ 已選」的游標移動、`_do_add` / `_do_remove` 的上限拒絕、
`_do_encrypt` 的無金鑰拒絕 —— 這三支都會經過 `lb.set_text()`（LVGL），
而 REPL 是**另一個執行緒**，跨執行緒碰 LVGL 有 hard fault 的風險，
所以沒有從 REPL 直接呼叫。它們由「`build()` 沒報錯」＋程式碼審閱涵蓋，
要真機確認請**用手轉編碼器**走一遍 UI。

### 5.2 還沒驗的

- [ ] **`_hw_add_peer` 的加密形式**：`add_peer(mac, lmk, None, 0, True)`（位置）還是
      `add_peer(mac, encrypt=True)`（kwarg）—— 兩條路都寫了，但**沒真機驗過**
- [ ] `set_pmk` 的實際行為（16-byte 金鑰）
- [ ] **加密的 6 格上限**是否真的是 6（文件說 ESP-IDF `ESP_NOW_MAX_ENCRYPT_PEER_NUM=6`，
      但 MicroPython 有沒有另外的限制要量）
- [ ] 「加密後廣播仍可送」（我斷定廣播不能加密，要用真機確認）
- [x] `0x3101` 對遠端 Slave 的實際回覆時間 → 見 changelog §35（20~30ms 級）

---

## 6. 已知陷阱（這幾件事踩過，別再踩）

1. **`mpremote` 預設用軟重開機收尾** → 內部 SRAM 被 WiFi/ESP-NOW 驅動累積耗盡 →
   開機 hard fault → **USB-CDC 整個消失**（連 `/dev` 節點都沒有）。
   用 `--no-soft-reset`，或連續操作之間做 hard reset。見 changelog §34.5。
2. **上傳要在板子「站穩」之後** —— `machine.reset()` 剛觸發、USB 還在重新列舉時上傳會全失敗。
   上傳後**一定在板上驗證**新程式碼真的在（`print('del_peer' in open(...).read())`）。
3. **ESP-NOW 廣播不能加密**（加密只支援單點）。讓廣播 peer 帶 `encrypt=True`
   會變成「加得進去、但廣播從此送不出去」。
4. **`espnow.recv()` 會回 `(mac, None)`**（peer event，實測 3 秒 2871 次）
   → 所有接收迴圈都要擋 `msg is None`。
5. **同一塊板連續跑多輪測試**要先 `active(False)` 舊的 ESP-NOW，
   否則 `recv()` 拋 `ValueError: ESPNow.recv(): buffer error`。
6. **`0x1016` 無 ACK、實測會掉幀**（送 5 到 4）→ 要「送→用 `0x1101` 查→沒到就重送」。
7. **MicroPython 沒有 refcount** —— `open(p, "w").write(src)` 之後檔案還開著，
   要等 GC 才 flush。接著馬上 `machine.reset()` → **寫入整批消失**，
   開機讀到的還是舊內容（而我印了「✓ 已改」）。
   ★ 一定要 `f = open(p,"w"); f.write(src); f.close()`，而且**寫完讀回來印出那一行**再重置。
   （這一輪就是這樣才查到 PixelTask 沒啟動的真正原因。）
8. **板上的檔案可能比 repo 舊**，而且失敗方式很陰險：
   `now_bus.py` 少了 `peer_cap()` / `set_encrypt()` / `clear_peers()`，
   但頁面裡的 `try: now.peer_cap() except: 19` **不會報錯**，
   只會**安靜地顯示錯的上限**（加密時該是 6）。
   → 動到既有 API 之後，**上板前先問板子**：
   ```python
   for n in ("peer_cap","set_encrypt","clear_peers"):
       print(n, hasattr(bus.get_service("NowBus"), n))
   ```
9. **`espnow` / `NowBus` 的 peer 清單回的是 `bytes`，不是 hex 字串** ——
   `"FFFFFFFFFFFF" in now.peers` 恆為 False（還會噴
   `Warning: Comparison between bytes and str`）。要比對請用 `mac_bytes()`。
