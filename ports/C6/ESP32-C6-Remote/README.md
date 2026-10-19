# ESP32-C6 無顯示兩鍵遙控器（NC4 / ESP-NOW）

> **用途**：一顆只有兩顆按鈕、沒有螢幕的 C6 遙控器 —— 開機問一次對方目前的模式，
> 之後「進入可動模式」與「下一個模式」兩個動作都只發 NC4 指令，不做路由、不做 UI。
> **內建手動探索**：長按按鈕1（或 REPL 打 `scan()`）會把掃到的對方位址印成
> **可直接複製的兩行**（`PEER_MAC` / `PEER_CID`）——**不自動配對**，暫時預設廣播。
> **分類**：port（實體板子 delta）／**獨立固件**
> **最後更新**：2026-10
> **狀態**：✅ 協議層＋配對層＋開機流程已離線驗證（`test/protocol/test_c6_remote.py`，196 項全過，
> 含用假射頻跑完整 `main()`）；⏳ 真機（C6 + 對方裝置）尚未驗證
> **相關文件**：
> - `doc/01_protocol/01_nc4_protocol.md` — NC4 幀格式、定址詞彙表（cid vs MAC）
> - `doc/01_protocol/04_pixel_protocol.md` — `0x31xx` 模式指令（`0x0200` 的來源）
> - `doc/02_guides/18_node_discovery.md` — 掃描／綁定流程（本檔 §4 的依據）

---

## 0. 這個資料夾是什麼（不是什麼）

| | 說明 |
|---|---|
| **是** | 一個**獨立單檔 MicroPython 固件**：`main.py` 燒進 C6 就完成，不依賴 `slave/` 的任何模組 |
| **不是** | **不是** `slave/` 的 delta。它不覆蓋 `slave/` 的任何檔案，也不跑 App / Router / TaskManager / UI |

所以燒錄方式與 `ports/README.md` 的一般規則不同 —— 這裡**只上傳一個檔案**，
C6 上不需要（也不應該）先鋪整套 `slave/`。

---

## 1. 檔案

| 檔案 | 說明 |
|---|---|
| `main.py` | 全部的東西：檔頭「① 使用者參數」＋協議組包／解包＋按鈕＋ESP-NOW＋主流程 |
| `../../test/protocol/test_c6_remote.py` | 離線驗證：遙控器組出來的幀與 `slave/lib/sys/proto.py` **逐位元組相同** |

燒錄（C6 的 MicroPython 需含 `espnow` 模組，官方 `ESP32_GENERIC_C6` 版本有）：

```bash
# 1) 上傳（COMxx / /dev/cu.usbmodemXXXX 依實際埠改）
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX fs cp ports/C6/ESP32-C6-Remote/main.py :main.py
# 2) 重啟 → REPL 會印出 MAC / 頻道 / 開機同步結果
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX reset
# 3) 看即時 log（DEBUG=True 時每一幀 TX/RX 都會印）
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX repl
```

---

## 2. 硬體接線

```
                    ┌──────────────── C6 ────────────────┐
按鈕1（可動模式）   │  BTN_MOVABLE_PIN = 2  ──┤ ├── GND   │  內部上拉：按下 = 低電位
按鈕2（下一個）     │  BTN_NEXT_PIN    = 3  ──┤ ├── GND   │  （BTN_ACTIVE_LOW = True）
                    │                                    │  去抖 DEBOUNCE_MS = 30
回饋 LED（可選）    │  LED_PIN = None（預設不用）        │
                    └────────────────────────────────────┘
   C6-DevKitC 板載 RGB 在 GPIO8 → 設 LED_PIN=8 / LED_TYPE="ws2812"
```

> ★ 兩個 GPIO 就是檔頭 **A. 按鈕接線** 那兩行（`main.py` 第 63–64 行附近），
> 兩顆都接 GND、不需要外部電阻；接錯腳或兩顆接同一支腳，開機的參數檢查會直接報 ❌。

- 兩顆按鈕**直接接 GND**，不需要外部電阻（用 MCU 內部上拉）。
- **腳位避雷**（換晶片時最容易踩）：

  | 晶片 | 目前設定 | 要避開 |
  |---|---|---|
  | **ESP32-S3** | 依實際接線（目前 可動=`11`、下一個=`12`） | `0/3/45/46`（strapping）、`19/20`（USB D-/D+）、`33–37`（八線 PSRAM 的模組） |
  | **ESP32-C6** | 可動=`2`、下一個=`3` | `8/9/15`（strapping）、`12/13`（USB D-/D+）；GPIO9 是 BOOT 鍵 |

  → 同一份程式在 S3/C6 都能跑（ESP-NOW 與 MicroPython 行為相同），**只有 GPIO 編號要改**。
  C6 的 `12/13` 是 USB，但 S3 的 `12` 是普通 GPIO —— 所以「同一支腳」在不同晶片上意思不同，換板時一定重看這張表。
- 按鈕事件是**去抖後的邊緣觸發**，且等待回覆期間仍在輪詢 → 不會因為「正在等 GET 回覆」而漏按。
- **短按＝放開時成立；長按（`BTN_LONG_PRESS_MS`，預設 3 秒）＝按住達標立刻觸發**，
  且長按會吃掉那一次短按 → 同一顆鈕不會又掃描又切模式。

---

## 3. 檔頭參數（① 使用者參數）

> **參數區有目錄**：`main.py` 的『① 使用者參數』按 **A→F** 排
> （**A 按鈕接線** → B 射頻與對象 → C 模式指令 → D 手動探索 → E LED → F 時序），
> 一開頭就有目錄註解，不用往上翻。
> **最常改的四個都在最上面**：
>
> | 參數 | 在哪 | 意思 |
> |---|---|---|
> | `BTN_MOVABLE_PIN` | **A** | 按鈕1「進入可動模式」的 GPIO（預設 2） |
> | `BTN_NEXT_PIN` | **A** | 按鈕2「下一個模式」的 GPIO（預設 3） |
> | `PEER_MAC` | B | 對方的射頻 MAC（`None` = 廣播） |
> | `PEER_CID` | B | 對方的協議 cid（`None` = 廣播） |

### 3.1 廣播還是單播？（**先看這張表**）

**沒有「單播/廣播開關」** —— 本樹的定案（`doc/03_notes/19_remote_control_plan.md` §2.6）：
> ❌ 不需要「廣播/單播設定開關」（原設計方向錯誤）
> ✅ 知道目標 → 定向；不知道 → 廣播

所以「有沒有對象」由**資料**回答（`PEER_MAC`/`PEER_CID` 填了沒），
`SEND_TARGET` 只表達唯一的例外：**什麼時候要刻意對全部講**。

| 情況 | 結果 | 為什麼 |
|---|---|---|
| 兩個位址都填好 | **單播** | 知道對象就打一台（`dst=PEER_MAC`、幀頭 `addr=PEER_CID`） |
| `PEER_MAC = None`（預設） | 廣播 | **沒對象就只能廣播**；也防止「填了半套卻靜默送去不存在的位址」 |
| 只填了 MAC、沒填 cid | 廣播 ＋ 開機警告 | 單播要「射頻 MAC ＋ 協議 cid」兩層齊備 |
| `PEER_MAC` 格式錯 | 廣播 ＋ 開機警告 | 同上 |
| `PEER_MAC = "ffffffffffff"` | 廣播 | 明寫射頻廣播位址 |
| `SEND_TARGET = "all"` | 廣播 | 就算知道對方，這次也要對全部講（例如全部一起換模式） |
| `SEND_TARGET = "first"`（預設）且還沒破冰 | 第一幀廣播 | 破冰：讓對方學到我們的 MAC（ESP-NOW 單播的接收方要先認識發送方） |

> ★ **填了位址就會自動變單播，沒有第二個開關要開**；反過來沒填就是廣播。
> 開機那行 `[CFG] 目前是…` 與每行 `[TX] … (原因)` 都會直接講答案。

### 3.2 你必須提供的

| 參數 | 意義 | 怎麼填 |
|---|---|---|
| `CHANNEL` | Wi-Fi 頻道 1–13 | **必須與對方完全相同**，否則一幀都收不到（本樹預設 6） |
| `PEER_MAC` | 對方的**射頻**位址（6B MAC） | **`None` = 沒確認 → 廣播**；要單播就填 `scan()` 印出來的值（12 hex） |
| `PEER_CID` | 對方的**協議**位址（cid） | **`None` = 沒確認 → 不能單播**；`scan()` 印出來的值（只在單播時送進幀頭 addr） |
| `MY_CID` | **自己的**協議位址 | **`None` = 用不到**。它是**你自己挑的**（不是掃出來的），只有 `PAIR_AS_MASTER=True` 或 `SCAN_REPLY_DIRECTED=True` 才需要（別跟對方撞號）。★ 這是「我們自己的 cid」**唯一**的存放處 |
| `BTN_MOVABLE_PIN` / `BTN_NEXT_PIN` | 兩顆按鈕的 GPIO | 預設 2 / 3，改成你實際接的 |

> ★ **MAC 與 cid 是兩層，不可互推**（`01_nc4_protocol.md` 的定址詞彙表）：
> 射頻用 MAC 送達，協議用 cid 決定「誰該收下這一幀」。單播時兩個都要對。

### 3.3 行為開關

| 參數 | 預設 | 說明 |
|---|---|---|
| `SEND_TARGET` | `"first"` | 「這一幀送給誰」的**唯一**參數（不是單播/廣播開關，見 §3.1）：`"first"`=開機第一幀先廣播破冰、之後照對象走；`"peer"`=一開始就照對象走；`"all"`=一律廣播（對全部講）。打錯字 → 當作 `"first"` 並在開機印 ❌ |
| `PAIR_AS_MASTER` | `False` | 開機額外送 `0x1016 SET_MASTER{MY_CID}`（＝「以後回信都給我」）。**這就是自動配對，刻意預設關**——要配對請用 §4 的 `scan()` 手動複製位址。⚠️ 與 `SCAN_REPLY_DIRECTED` 是**不同機制**（這個會寫進對方 flash、可能被拒絕） |
| `BTN_LONG_PRESS_MS` | `3000` | 按住按鈕1 幾 ms = 觸發 `scan()`（`None` = 停用長按） |
| `SCAN_REPLY_DIRECTED` | `False` | 對方那封掃描回信要「只有我們收」嗎？`False` → 回信用 `0xFFFF`（**廣播回覆**，不需配對就收得到）；`True` → 用 `MY_CID` 定向（**cid 的值來自 `MY_CID`，不在這裡再填一次**；沒填會自動退回廣播並警告） |
| `SCAN_WINDOW_MS` | `800` | 點名的抖動視窗：對方隨機延遲 0~此值才回（避免 N 台同時回話碰撞）。★ 一個值兩個角色：既是協議欄位 `timeout_ms`，也是本地收件視窗（再多等 600ms 收尾） |
| `SCAN_FORCE` | `True` | 每次掃描都要求重新自我介紹（對方對同一個問的人只回第一次） |
| `HELLO_WINDOW_MS` | `400` | 開機「破冰點名」（`0x100D`）的收件視窗；不用像手動掃描那麼久 |
| `PEER_PROBE_MS` | `5000` | 對端**已知不回覆**時，待機每隔幾 ms 偷偷試一次它是否恢復（`0` = 停用）。探測失敗不印任何東西，只有成功才印 `[PROBE] 對端恢復回應` |
| `MOVABLE_MODE` | `0x0200` | 按鈕1 的目標模式 = `mode_type 2(SERVO)` / `mode_id 0` |
| `BOOT_MODE` | `0x0000` | 開機的**本地預設**模式（GET 有回覆就被覆蓋） |
| `STEP` | `1` | 按鈕2 的增量（16-bit 合併 id 直接 +1） |
| `STEP_BASE_IF_UNKNOWN` | `0x0200` | 從未同步成功時的 +1 基準（見 §6） |
| `BRIGHTNESS` | `255` | `0–254`=設定亮度；**`255`=不改**（`0` 是合法值＝最暗，不能拿來當「不改」） |
| `START_DELAY_MS` | `0` | 對方收到後延遲幾 ms 起播 |
| `CONFIRM_AFTER_SET` | `True` | `MODE_SET` 後再 GET 一次確認 |
| `CONFIRM_DELAY_MS` | `80` | 送 SET 後等幾 ms 再 GET（`MODE_SET` 無 ACK，要給對方時間套用） |
| `RSP_TIMEOUT_MS` / `RETRIES` | `300` / `3` | 等一次回覆的時間／重送次數（ESP-NOW 無 ACK，掉幀是常態） |
| `ADDR_FILTER` | `True` | 只收 `addr == MY_CID` 或 `0xFFFF` 的幀 |
| `DEBUG` | `True` | 印出每一幀 TX/RX |

---

### 3.4 開機時的參數檢查（❌ 就不跑）

`main()` 第一件事是 `_report_config()`：把**每一個**使用者參數檢查一次，
有 ❌ 就**不啟動**（不會拿猜的值繼續操作），⚠️ 只提醒。

```text
❌ [CFG] CHANNEL=0 必須是 1..13 的整數（Wi-Fi 頻道）
❌ [CFG] BRIGHTNESS=256 必須是 0..255（255=不改；0 是合法亮度）
❌ [CFG] DEBUG='False' 必須是 True/False（字串 "False" 是**真**的，會靜默生效）
❌ [CFG] 共 3 個參數有問題 → 修好再上電（本檔不會拿猜的值繼續跑）
```

| 類別 | 檢查什麼 |
|---|---|
| 範圍 | `CHANNEL` 1–13、`BRIGHTNESS` 0–255、`START_DELAY_MS` 0–65535、模式 id 0–0xFFFF、時序 >=0、`RETRIES` >=1 |
| 型別 | `STEP`/`MOVABLE_MODE`/`BOOT_MODE` 必須是整數（**字串 `"0x0200"` 會被抓**）、`LED_PIN` 必須是整數或 `None` |
| 位址 | `PEER_MAC` 必須是 12 個 hex；`PEER_CID`/`MY_CID` 必須是 `None`／整數／`"0x1234"` 字串 |
| 布林 | 八個開關必須真的是 `True`/`False` —— 字串 `"False"` 是**真**的，`ADDR_FILTER=0` 也不是 bool |
| 列舉 | `SEND_TARGET`、`LED_TYPE` 必須是清單裡的值（打錯字會被抓，不是靜默退回） |
| 組合 | 兩顆按鈕接同一支腳 → ❌；`PAIR_AS_MASTER=True` 但沒有 `MY_CID` → ⚠️；只填 MAC 沒填 cid → ⚠️；`STEP=0` → ⚠️ |

> ★ **cid 的合法寫法有三種**（`None` / `0..0xFFFF` 整數 / `"0x1234"`、`"1234"`、`"0002"` 字串），
> 其他一律 ❌。舊版是「硬吞」：`"0x0002"` → 靜默變 `0xFFFF`、`65536` → 環繞成 `0x0000`、
> `True` → `0x0001`、`1.5` → `0x0001`，而且 `[CFG]` 那行還會**印 0xFFFF 騙你**。

## 4. 手動探索／配對（**不自動配對**）

### 4.0 掃描送出的指令是什麼

| | 內容 |
|---|---|
| **CMD** | `0x100D` `IDENTIFY_REQ`（Master → **廣播**） |
| **Payload** | `reply_cid(u16) timeout_ms(u16) force(u8)` = 5B |
| **射頻** | `dst = ff:ff:ff:ff:ff:ff`、幀頭 `addr = 0xFFFF`（**永遠廣播**：它是「問全部你是誰」） |
| **預設值** | `reply_cid=0xFFFF`、`window=800`、`force=1` |
| **完整幀** | `4e4304ffff0d100500ffff200301bda4652c`（18 B） |
| **回覆** | `0x100E IDENTIFY_RSP`：`cid(u16) slave_id(str_u16len) ip(str_u16len)` |

拆解：`4e43`=SOF`NC`、`04`=VER、`ffff`=addr、`0d10`=CMD `0x100D`(LE)、`0500`=LEN 5、
`ffff`=`reply_cid`、`2003`=`window 800`(LE)、`01`=`force`、`bda4652c`=CRC32。

### 4.1 三個入口

| 入口 | 做什麼 | 什麼時候用 |
|---|---|---|
| 長按按鈕1（3 秒） | `scan()`：廣播點名 → 收 0x100E → 把 `PEER_MAC`/`PEER_CID` 印出來 | 沒接電腦、只想拿到位址 |
| REPL `scan()` | 同上（可帶參數：`scan(1500)`） | 接著 USB 看輸出、複製貼上 |
| REPL `listen(15)` | **純被動**：印出這段時間每一幀的來源 MAC（不發東西） | 對方不回點名時的唯一辦法 |

掃描**只印不改**：不改 `PEER_MAC`/`PEER_CID`、不寫 flash、不送 `0x1016`。
配對的動作是「把印出來的兩行貼回檔頭」——由你決定。

```text
>>> scan()
[SCAN] 廣播 0x100D IDENTIFY_REQ{reply_cid=0xFFFF, window=800ms, force=1} …
[TX] cmd=0x100D addr=0xFFFF dst=ffffffffffff len=14 ok
  [0x100E] cid=0x0002  slave_id=AABBCCDDEEFF  ip={"sta":"192.168.1.50"}  ← src=aa:bb:cc:dd:ee:ff
────────────────────────────────────────────────────────────────
[SCAN] 共 1 台。下面兩行複製回 main.py『① 使用者參數』即可（本檔不會自動套用）：
  #1  src_mac=aa:bb:cc:dd:ee:ff  cid=0x0002  slave_id=AABBCCDDEEFF  ip={...}
        PEER_MAC = "aabbccddeeff"
        PEER_CID = 0x0002
  ★ 填回檔頭就變單播；單播還要對方認識我們，把本機 MAC 給對方：my_mac = 11:22:33:44:55:66
────────────────────────────────────────────────────────────────
[SCAN] 共 1 台。…→ 綠閃×1（掃到 1 台）／紅閃×5（沒人回）
```

### 4.1.1 真機實例（面板那一側的 log → 你該填什麼）

`0x100D/0x100E` 已在真機上驗證（面板 ↔ 節點）：

```text
[now_set] 掃描 已送出
🔹 [NOW-Bus] IDENTIFY_RSP (0x100E)
[INFO] [Peers] ＋ A0F262EAEB04 cid=None mac=A0F262EAEB04 via=frame iface=NOW-Bus
[INFO] [Peers] ↻ A0F262EAEB04 cid=60164 mac=A0F262EAEB04 via=identify_rsp iface=NOW-Bus
```

逐行解讀：

| log | 意思 |
|---|---|
| `掃描 已送出` | 面板廣播了 `0x100D`（`reply_cid` = 面板自己的 cid → **定向回覆**） |
| `＋ … cid=None via=frame` | 先在射頻層認識了 MAC（**這一刻還不知道 cid**） |
| `↻ … cid=60164 via=identify_rsp` | `0x100E` 回來，補上 cid：`60164 = 0xEB04` |

**`cid` 是 MAC 末四碼**（`ConfigManager.ensure_cID()` 在 `System.cID` 空著時自動填）：
`A0:F2:62:EA:EB:04` → `EB04` → `0xEB04` → 60164。

→ 要控制這台節點，遙控器檔頭就填這兩個值（`scan()` 印出來的就是同一組）：

```python
PEER_MAC = "a0f262eaeb04"
PEER_CID = 0xEB04         # 寫 60164 也接受（_parse_cid 認十進位/0x/字串）
```

> ⚠️ 面板那次掃描用了**定向回覆**（`reply_cid` = 面板 cid），所以那封 `0x100E`
> 只有面板收得到，遙控器不會看到 —— 自己跑 `scan()` 才拿得到。
> 反之遙控器預設用 `0xFFFF`（**廣播回覆**），所以兩者可以各自掃、互不干擾。
> 想要好認的 cid（例如 `0x0101`）就改那台節點的 `config.json` → `System.cID`。

**遙控器自己跑 `scan()` 的實測輸出**（2026-10，ESP32-S3 `e4:b3:23:f9:32:00`）：

```text
[SCAN] 廣播 0x100D IDENTIFY_REQ{reply_cid=0xFFFF, window=800ms, force=1} …
[TX] cmd=0x100D addr=0xFFFF dst=ffffffffffff len=18 ok (強制廣播)
[RX] cmd=0x100E addr=0xFFFF src=a0:f2:62:ea:eb:04 len=18 04eb0c004130463236324541454230340000
  [0x100E] cid=0xEB04  slave_id=A0F262EAEB04  ip=-  ← src=a0:f2:62:ea:eb:04
        PEER_MAC = "a0f262eaeb04"
        PEER_CID = 0xEB04
```

用 `reply_cid = MY_CID(0x0101)` 也驗過：對方會**單播**回給我們，而 `ADDR_FILTER` 收得到
（`[RX] … addr=0x0101`）—— 也就是「定向回覆 + 自己的 cid」這條路在真機上成立。

### 4.2 掃描用到／看到的四個指令

| CMD | 名稱 | 方向 | Payload | 這支遙控器怎麼用 |
|---|---|---|---|---|
| `0x100D` | `IDENTIFY_REQ` | 遙控器 → **廣播** | `reply_cid(u16) timeout_ms(u16) force(u8)` = 5B | 點名「你是誰？」 |
| `0x100E` | `IDENTIFY_RSP` | 節點 → 回 `reply_cid` 指的地方 | `cid(u16) slave_id(str_u16len) ip(str_u16len)` | **cid 的唯一來源** → `PEER_CID`；射頻來源 MAC → `PEER_MAC` |
| `0x1002` | `SLAVE_ANNOUNCE` | 節點 → 廣播 | `slave_id(str_u16len) pixel_count(u16) hw_version(str_u16len)` | 被動看到它是誰（**不含 cid**，不能拿來配對） |
| `0x1016` | `SET_MASTER` | 遙控器 → 單播 | `master_cid(u16)` | **本檔不自動送**（`PAIR_AS_MASTER=False`）；它是「方向」不是「身分」 |

### 4.3 為什麼「還沒配對也收得到掃描回覆」★

`0x100D` 的 `reply_cid` 決定**這一封回信的 NC4 幀頭 addr**，而射頻去向由 `NowBus.write()`
依 addr 解析：`0xFFFF` → 射頻**廣播**。所以：

| `reply_cid` | 回信 addr | 射頻去向 | 我們要先配對嗎 |
|---|---|---|---|
| `0xFFFF`（本檔預設） | `0xFFFF` | **廣播** `FF:FF:FF:FF:FF:FF` | ❌ 不用，只要 `add_peer(BCAST)`（開機就做了） |
| 具體 cid（例 `MY_CID`） | 該 cid | 單播給問的人 | ⚠️ 要：對方要知道我們的 MAC，**而且我們要收得到單播**（見 §7 第 2 點）。由 `SCAN_REPLY_DIRECTED=True` 開啟 |

→ 這是 ESP-NOW 上**唯一「不需要先互相認識」就能完成的探索路徑**，所以預設走它。
代價是同頻道的其他裝置也看得到那封回信（內容只有 cid/MAC/IP，沒有祕密）。

### 4.4 配對為什麼刻意不做自動

1. **身分 ≠ 方向**：掃描是「問你是誰」，`0x1016` 才是「我以後聽誰的」。
   早期版本讓掃描順便認主，結果是「第二台遙控器按一下掃描就把所有節點搶走」
   （`doc/02_guides/18_node_discovery.md` §3 記載的真實事故）。
2. **`0x1016` 有副作用**：它會**落盤**（`@node.master_cid`）、而且對方「本次開機已被認領」
   就拒絕換手 → 隨手送會把別台遙控器的控制權搶掉。
3. **我們還不需要它**：本檔的 `0x3103`/`0x3105` 都是遙控器主動發、對方回，
   回信位址由對方自己決定（`0x3102` 之類是廣播回），不依賴 master 設定。

要自動配對時再打開 `PAIR_AS_MASTER=True`（＝開機送一次 `0x1016{master_cid=MY_CID}`）。

### 4.5 探索流程建議

```text
① 先 listen(15)   → 確認「射頻上有沒有人、頻道對不對」（只有 MAC，沒有 cid）
② 再 scan()       → 有回 0x100E 才有 cid → 才能填 PEER_CID
③ 貼回檔頭        → PEER_MAC / PEER_CID（不貼＝維持 None＝廣播，功能照常）
④ 想只打一台      → 把位址填回檔頭（**沒有開關要開**），並把本機 MAC（開機印的 my_mac）給對方
```

> ⚠️ 若對方**不是**本樹的 mp_Net-Core（你說是另一套系統），它不一定實作 `0x100D`。
> 那就只能靠 `listen()` 等它自己講話（公告／心跳），或直接從對方那一側拿 MAC。

---

### 4.6 對端能力探測（2026-10 真機實測）

同一塊對端（`a0:f2:62:ea:eb:04`）逐一探測的結果：

| 指令 | 名稱 | 結果 |
|---|---|---|
| `0x100D` | `IDENTIFY_REQ` | ✅ 回 `0x100E`（`cid`、`slave_id` 有；`ip` 欄位是**空的**） |
| `0x1014` | `GET_IP` | ❌ 無回應 |
| `0x3101` | `MODE_LIST_QUERY` | ❌ 無回應 |
| `0x3103` | `MODE_GET` | ❌ 無回應 |
| `0x1101` | `STATUS_GET`（其他 key） | ❌ 無回應（`slave_id`／`?`／多 key 都不答） |
| `0x1101{node.master_cid}` | `STATUS_GET`（單一 key） | ✅ 回 `{"node.master_cid":1}` |
| `0x1016` | `SET_MASTER` | ❌ **被忽略**：送 `{master_cid=2}` 後再查，`node.master_cid` 仍是 `1` |
| （被動監聽 5s） | — | ❌ 沒有任何主動幀（不發公告/心跳） |

→ **這塊對端只實作了 discovery**（`0x100D/0x100E`）。
而且它的 `0x100E` 把 `ip` 回成空字串，本樹的 `_ips_json()` 永遠至少回 `"{}"`
→ 可以確定它**不是**本樹固件，是另一套系統。

> ★ **2026-10 再測：頻道上有兩台**（第一次掃描只看到一台，是因為另一台回得慢）：
>
> | 節點 MAC | cid | 支援 |
> |---|---|---|
> | `a0:f2:62:ea:eb:04` | `0xEB04` | `0x100D`＋`0x1101{node.master_cid}`（master=1） |
> | `1c:db:d4:80:99:a8` | `0x99A8` | **只有** `0x100D` |
>
> 兩台都實測過 `0x3103`／`0x3101{0,2}`／`0x3107`／`0x1014`（單播與廣播都試）→ **全部無回覆**。
> 所以「模式查詢」在這兩台上都不存在；`scan()` 想抓到回得慢的節點，就把 `SCAN_WINDOW_MS` 調大。

**對遙控器的影響**：

- `scan()`／`listen()` 可用 ✅（位址抓得到，所以 §3.2 那兩個值填得出來）
- `0x3103` 拿不到目前模式 → 開機同步會逾時，走 §6 的 16-bit +1 後備 ✅（設計本來就這樣）
- `0x3101` 拿不到模式清單 → 「下一個」只能靠 +1，不能依清單跳 ✅（也已是後備路徑）
- `MODE_SET(0x3105)` **尚未驗證**（會真的驅動機構，沒有在實機上送過）

要讓「GET 模式」真的成立，**對端必須補上 `0x3103/0x3104`**（請求空 payload；
回覆 11 bytes：`mode_type u8` + `mode_id u8` + `elapsed_ms u32 LE` + `total_ms u32 LE` + `running u8`）。

### 4.7 逐個指令取得的參數（2026-10 真機原始位元組）

> 做法：**不用任何 helper**，在 REPL 用原生 NC4 逐條送出、逐條看回覆。
> 這比「跑一支腳本」可靠 —— 每一條指令的 TX/RX 都在眼前，錯在哪一行一目了然。

| # | 指令 | 位址 | TX 幀 | RX 幀 | 得到什麼 |
|---|---|---|---|---|---|
| 1 | `0x100D` | `addr=0xFFFF` 廣播 | `4e4304ffff0d100500ffff200301bda4652c` | `4e4304ffff0e1012 00 04eb0c00 413046323632454145423034 0000 cf121226` | `cid=0xEB04`、`slave_id=A0F262EAEB04`、`ip` 空；射頻來源 `a0f262eaeb04` |
| 2 | `0x1101{node.master_cid}` | **`addr=0xEB04` 單播** | `4e430404eb011111000f006e6f64652e6d61737465725f6369645d365d29` | `4e4304ffff0211 1700 1500 7b226e6f64652e6d61737465725f636964223a317d 9e7f90a5` | **`{"node.master_cid":1}`** ← master 的 ID ＝ **1** |
| 3 | `0x3103` | `addr=0xEB04` | `4e430404eb03310000fc6faa9a` | — 無回覆 | MODE_GET **未實作** |
| 4 | `0x3101{0}` / `{2}` | `addr=0xEB04` | … | — 無回覆 | MODE_LIST **未實作** |
| 5 | `0x1101{?}` / `{slave_id}` / `{System.cID}` / 多 key | `addr=0xEB04` | … | — 無回覆 | 這塊對端**只認得 `node.master_cid` 一個 key** |

**三個關鍵教訓（都是真機才會發現）**：

1. **`ADDR` 必須是對方的 cid**。用 `0x0001`（＝它的 *master_cid*，不是它的 cid）
   送 `0x1101` 會**完全靜默** —— 同一個 key、同一塊板，只有 addr 不同，結果天差地遠。
   → 這也是為什麼 `PEER_CID` 填錯時「按了沒反應」卻沒有任何錯誤。
2. **`0x100D` 是唯一可以純廣播的**（它的回覆用 `reply_cid` 指定）。
   其餘指令的回覆會依 `master_cid`/廣播策略走，請求本身通常要定址。
3. **「對端的 master id」是查出來的**：`0x1101{keys="node.master_cid"}` → `1`。
   ★ 但那是**對方**的資料（它目前回話的對象＝面板，cid `0x0001`），
   **不是我們的 id**。我們自己的 cid 由 `MY_CID=None` 自動推導（本機 `0x3200`）。
   把 `1` 填進 `MY_CID` 等於冒充面板 —— 這是曾犯過的錯，已改回自動。

## 5. 行為流程

```
開機
 ├─ STA 起來 + 設頻道 + ESPNow active + add_peer(BCAST[, PEER_MAC])
 ├─ 印出「這一幀會走哪裡」：單播 dst=aa:bb:.. addr=0x0002 ／ 廣播 dst=ffffffffffff（含原因）
 ├─ 本地模式 = BOOT_MODE (0x0000)
 ├─ 【只此一次】0x3103 MODE_GET → 0x3104 MODE_GET_RSP
 │    ├─ 有回覆 → 本地模式 = 回覆值（並印出 type/id/elapsed/total/running）
 │    └─ 沒回覆 → 沿用本地預設（之後 +1 用 STEP_BASE_IF_UNKNOWN 當基準）
 └─ （可選）0x1016 SET_MASTER{MY_CID}

按鈕1（可動模式）
 └─ 目標 = 0x0200 → 0x3105 MODE_SET{type=2, id=0, delay, bri}
      → 等 CONFIRM_DELAY_MS → 0x3103 GET 確認
           ├─ 回覆 = 0x0200 → ✅
           ├─ 回覆 ≠ 0x0200 → ⚠️ 以對方回報的為準
           └─ 逾時        → ⚠️ 本地保留 0x0200（不覆蓋）

按鈕2（下一個）
 └─ 目標 = (目前模式 + STEP) & 0xFFFF → 同上（SET → GET 確認）

按鈕1 長按（BTN_LONG_PRESS_MS，預設 3s）
 └─ scan()：廣播 0x100D → 收 0x100E → 印出 PEER_MAC / PEER_CID（不套用、不送 0x1016）
```

**為什麼 `MODE_SET` 之後還要 GET**：協議規定 `MODE_SET` **不回 ACK**，
「有沒有生效」只能另外查 —— 這正是 `0x3104` 存在的理由。

**開機只 GET 一次**：GET 不是「每次發送的前置條件」，只是用來取得起點；
之後的 +1 由本地值推進（GET 逾時也不會跳回 0）。

---

## 6. 「+1」的兩種情況（後備方案）

| 目前本地值 | 按鈕2 送出 | 為什麼 |
|---|---|---|
| `0x0200`（已同步） | `0x0201` | 16-bit 合併 id 直接 +1 |
| `0x02FF` | `0x0300` | 直接進位 → **會跨組**（SERVO → AUDIO）。要避免就改成「同組內 +1」或先用 `0x3101` 取清單 |
| `0x0000`（從未同步成功） | `0x0201` | 高 byte 0 = 系統模式（協定規定 Master **不可** `MODE_SET` type 0）→ 改用 `STEP_BASE_IF_UNKNOWN` 當基準，否則會送出無用的 `0x0001` |
| `0xFFFF` | `0x0000` | 環繞 |

設 `STEP_BASE_IF_UNKNOWN = None` 就是**純 16-bit +1**（`0x0000 → 0x0001`）。

> 對方對「不存在的模式」是**直接忽略**（`gmode.set_mode` 只印警告），
> 所以送錯 id 不會壞掉，只會沒反應 —— 但也就很難察覺，這就是為什麼要有 LED 回饋。

---

## 7. 四個最容易「靜默失敗」的點

1. **頻道不一致** —— 兩端各自在 1–13 中不同頻道 → 收不到、也不會報錯。
   本樹的 `Network.ESP_now.channel` 預設是 6。
2. **ESP-NOW 單播要求「接收方也認識你」** —— 接收方的 peer 表要有發送方的 MAC，
   否則單播幀會被丟棄。而它**只在你送過去的幀到達時**才學得到你的 MAC
   （MAC 無法枚舉）→ 所以 `SEND_TARGET="first"`（預設）讓開機第一幀走**廣播**
   是最省事的破冰方式。開機 log 會印 `my_mac=`，也可以拿去對方的綁定清單直接加。
3. **`PEER_MAC` / `PEER_CID` 是佔位值但開了單播** —— 已由 `resolve_target()` 擋掉：
   任一為 `None`（預設）或 MAC 格式錯時**一律退回廣播**並在開機印警告，
   不會靜默地把幀送去一個不存在的位址（那種失敗連 log 都看不出來）。
   → 「以為在單播、其實在廣播」不會發生，反過來「以為填了就單播」也不會
   （填了位址就自動單播，沒有第二個開關；開機那行 `[CFG] 目前是…` 會直接講答案）。
4. **`0x3103 / 0x3104` 在對方的實作狀態** —— 協議文件明載：在本樹的
   `slave/action/pixel_actions.py` 裡這兩條**只有 schema、沒有 handler**。
   本遙控器假設對方（另一個系統）已依協議實作；若對方其實是本樹的 slave，
   要先把這一對補上，否則開機同步永遠逾時（功能仍可用，會走 §6 的後備路徑）。

其他常見症狀：

| 症狀 | 最可能原因 |
|---|---|
| 完全沒有 `[RX]` | 頻道不同 / 對方沒開 / 已單播但對方不認識你的 MAC |
| `[TX] ... FAIL` | `PEER_MAC` 格式錯（長度不是 12 hex）或對方不在射程 |
| `[GET] 沒有回覆` ×3 | 對方沒實作 `0x3103`／對方只在單播回、但你用廣播問／掉幀 |
| `[RX] 丟棄（addr 不是本機）` | 對方回覆的 `addr` 不是 `MY_CID` 也不是 `0xFFFF` → 檢查 `MY_CID` 或把 `ADDR_FILTER=False` |
| Traceback 停在 `pump()` 的 `_sleep_ms(2)` | 那行是主迴圈裡 CPU 待最久的地方 —— 幾乎都是 **`KeyboardInterrupt`**（Ctrl-C／`mpremote` 斷線）。看 traceback 最後一行：若是 `KeyboardInterrupt:` 就不是 bug。若是別的例外，現在會印出來並計入 `_STATS`，程式會繼續跑 |

### 7.1 主迴圈不會因為例外而死

無顯示、無人看守的裝置最怕「停在 traceback 上，使用者只看到按了沒反應」。所以：

**按鍵路徑不等一個不回話的對端**（實測：那塊對端沒實作 `0x3103`）：

| 情況 | 按鍵時的行為 |
|---|---|
| `_PEER_RESPONDS` 未知／`True` | `MODE_SET` → 等 `CONFIRM_DELAY_MS` → `GET` 確認（約 300ms） |
| `_PEER_RESPONDS is False`（已知沉默） | **只送 `MODE_SET` 就完成**（毫秒級），輸出仍只有一行：`[SET] 0x0201 …（對方不回覆→不確認）` |
| 待機背景 | 每 `PEER_PROBE_MS` 用 `quiet` 模式試一次；恢復時印 `[PROBE] 對端恢復回應 → 目前模式 0x…` 並把 `_MODE` 同步成對方的值 |

| 位置 | 行為 |
|---|---|
| `loop()` | 每一輪都經過 `_guard()`：任何例外 → 印一次 `❌ [RUN]` ＋ `sys.print_exception`，計數 `_STATS["loop_err"]`，睡 50ms 後**繼續跑** |
| `pump()` | 單幀處理出錯 → 只跳過那一幀（`_STATS["rx_err"]`），接收路徑不停擺；壞資料不會被放進結果 |
| 查詢 | REPL 打 `_STATS` 看兩個計數器；`_STATS["loop_err"] > 0` 就代表有東西在出錯 |

---

## 8. LED 回饋（`LED_PIN` 不為 None 時）

| 顯示 | 意義 |
|---|---|
| 藍閃 ×1 | 發射 |
| 綠閃 ×2 | 收到回覆且確認成功 |
| 紅閃 ×5 | 逾時／失敗／對方回報的值與要求不同 |
| 藍閃 ×N、停一拍、白閃 ×(M+1) | 目前模式 = `type=N, id=M`。`0x0200` → 藍×2、白×1 |
| 橘快閃 ×6 | 模式編號太大（type 或 id+1 > 12），不逐次閃 |
| 青閃 ×2 → 綠閃 ×N | **長按掃描**：開始掃描 → 掃到 N 台（最多 5 下） |
| 青閃 ×2 → 紅閃 ×5 | 長按掃描：沒有任何回覆 |

---

## 9. 離線驗證

```bash
python3 -B test/protocol/test_c6_remote.py
```

196 項，全部不需要硬體：

| 節 | 覆蓋 |
|---|---|
| §1–§2 | 幀與 `slave/lib/sys/proto.py` 的 `Proto.pack` **逐位元組**比對（含 LEN=0/250 邊界）、CRC 範圍 |
| §3 | `MODE_SET`/`MODE_GET` payload 佈局（`0` 亮度、65535 delay 等邊界） |
| §4 | 黏包／半包／垃圾前綴／CRC 壞幀重同步／錯 VER |
| §5–§6 | `MODE_GET_RSP` 解碼、+1 與後備基準（含 0x02FF→0x0300 跨組的已知行為） |
| §7 | MAC 字串 → bytes（espnow 只吃 6-byte） |
| §8 | 與權威 `StreamParser` **雙向**互解 |
| §9 | **配對層**：`0x100D`/`0x3105` payload 與權威 `SchemaCodec` 比對、`0x100E`/`0x1002` 解碼（UTF-8／空字串／截斷）、`str_u16len` 邊界、`None` 安全（`_cid_or_bcast`/`_addr_ok`）、廣播/單播判定、掃描預設值 |
| §12 | **韌性**：`_guard()` 吞掉例外並計數、`_loop_step()` 在按鈕未建好時安全返回、`pump()` 遇到壞幀只跳過那一幀且不污染結果 |
| §11 | **參數驗證**：`_parse_cid` 的合法/非法寫法（含 `"0x0002"`、`65536`、`True`、`1.5`）、`resolve_target` 遇到看不懂的 cid 會退回廣播、`_validate_config()` 對 21 種壞設定的抓錯、⚠️ 與 ❌ 的分界、有 ❌ 時拒絕啟動 |
| §10 | **端到端（假射頻）**：真的跑 `radio_init()`/`boot_sync()`/`on_button_*`/`scan()`/`listen()`/`main()` —— 驗證送出的位元組、`_MODE` 狀態、逾時後備、單播切換、定向回覆與 `ADDR_FILTER`、按鈕去抖／短按／長按、以及**全預設（三個位址都 None）開機不丟例外** |

> §10 的假射頻是**因果式**的（收到我們的幀才回話）—— 這一點很重要：
> 事先把回覆塞進 inbox 會被 `request_mode_get()` 開頭的 `flush_rx()` 正確丟掉
> （那是「不要拿上一輪的遲到回覆當答案」的設計），測試要照真實時序才測得準。

真機驗證順序建議：
1. C6 REPL 看開機 log（`my_mac` / 頻道 / 開機同步結果）。
2. `listen(15)` → 確認射頻上有沒有人（只有 MAC）；再 `scan()` → 拿到 cid。
3. 把印出來的 `PEER_MAC`/`PEER_CID` 貼回檔頭（或先用廣播跑通再改單播）。
4. 按按鈕，看對方有沒有換模式；掉幀時看 `[GET] 第 n/3 次沒有回覆`。

> 對方那一側也可以用 `test/protocol/espnow_mon.py` 直接看**收到的是什麼位元組**，
> 用來分辨「我們送錯」還是「對方沒回」。

---

## 10. 參數審計：一個概念只有一個住址

> 目的：避免「同一件事要填兩個地方」—— 那種重複最後一定會有一邊過期，
> 而且過期的那一邊不會報錯，只會讓行為跟你想的不一樣。

### 10.1 已修掉的真重複

| 概念 | 之前有幾個住址 | 現在 | 為什麼 |
|---|---|---|---|
| 「要單播還是廣播」 | ~~`USE_UNICAST` ＋ `PEER_MAC`/`PEER_CID`~~ | **1 個：資料本身**（填了位址就單播、沒填就廣播） | 本樹 `doc/03_notes/19_remote_control_plan.md` §2.6 早就否決這個開關：「❌ 不需要廣播/單播設定開關（原設計方向錯誤）」 |
| 「什麼時候要對全部講」 | ~~`USE_UNICAST` ＋ `BOOT_SYNC_BROADCAST` 兩個 bool~~ | **1 個：`SEND_TARGET`**（`"first"`/`"peer"`/`"all"`） | 兩個 bool 描述同一件事，還有隱藏交互（後者只在前者為 True 時才有意義） |
| 「LED 的電氣型態」 | ~~`LED_KIND` ＋ `LED_ACTIVE_LOW`~~ | **1 個：`LED_TYPE`**（`"gpio"`/`"gpio_low"`/`"ws2812"`） | 舊版 `LED_KIND="ws2812"` ＋ `LED_ACTIVE_LOW=True` 是無效組合，而且靜默生效 |
| 「我們自己的 cid」 | ~~`MY_CID` ＋ `SCAN_REPLY_CID`~~ | **1 個：`MY_CID`**（掃描那邊只剩開關 `SCAN_REPLY_DIRECTED`） | 同一份 cid 填兩次，改一邊忘一邊 |

### 10.2 看似重複、其實不是（刻意保留，已在程式註解互相指路）

| 看起來像 | 其實 | 為什麼不合併 |
|---|---|---|
| `STEP_BASE_IF_UNKNOWN = MOVABLE_MODE` | 同一個**值**，不同的**問題**（「按鈕1 去哪」vs「沒同步成功時 +1 從哪算」） | 那是**預設值**不是第二份設定：`= MOVABLE_MODE` 是 by reference，改一個一起改 |
| `PAIR_AS_MASTER` vs `SCAN_REPLY_DIRECTED` | 都是「讓回信寄給我」，但機制與副作用不同 | 前者是持續的（`0x1016`，會寫對方 flash、可能被拒絕）；後者只影響那一次掃描的回信 |
| `SCAN_WINDOW_MS` | 一個值、兩個角色（協議的 `timeout_ms` ＋ 本地收件視窗） | 拆成兩個只會讓它們不同步；本地多等 600ms 是固定的收尾餘量 |
| `BTN_ACTIVE_LOW` | 兩顆按鈕共用一個極性 | 本檔的前提是兩顆都接 GND；真要一高一低才需要拆成 per-button |
| `PEER_MAC` vs `PEER_CID` | 兩層（射頻 MAC / 協議 cid），不可互推 | 見 §3.2 的註記 |

### 10.3 審計時順手抓到的真 bug

`scan()` 以前沒有強制廣播 → 一旦填好位址（進入單播狀態），**點名會變成只問那一台**：
其他節點因為幀頭 `addr` 不是自己而不理它，於是「掃描」再也掃不到新節點，
而且沒有任何錯誤訊息。`0x100D` 的語意是「問**全部**『你是誰』」→
現在固定 `force_broadcast=True`，並有測試釘住這個行為（§9 的表格最後一列）。

### 10.4 同一家族的第二批：**寬鬆輸入（tolerant parsing）**

§10.1–10.3 抓的是「一個概念兩個住址」；這一節抓的是另一種同源病：
**參數沒有被驗證，而是被「容忍地轉換」** —— 打錯型別不報錯，靜默變成別的意思。

| 輸入 | 舊行為（靜默） | 現在 |
|---|---|---|
| `PEER_CID = "0x0002"`（字串） | → `0xFFFF`，而且 `[CFG]` 印「0xFFFF」騙人 | ✅ 認得（＝0x0002），單播照常 |
| `PEER_CID = 65536` | → `0x0000`（環繞） | ❌ 開機報「超出 0..0xFFFF」 |
| `MY_CID = True` | → `0x0001` | ❌ 開機報「bool 不是 cid」 |
| `PEER_CID = 1.5` | → `0x0001`（截斷） | ❌ 開機報「型別不對（float）」 |
| `PEER_CID = "abc"` | → `0xFFFF`（當成未設定） | ❌ 開機報看不懂；行為退回廣播 |
| `BRIGHTNESS = 256` | 夾成 `255` ＝**意思變成「不改亮度」** | ❌ 開機報範圍（255 有特定語意，不能靠夾） |
| `DEBUG = "False"` | 字串是**真**的 → 一直噴 log | ❌ 開機報「必須是 True/False」 |
| `PAIR_AS_MASTER = "False"` | 是**真**的 → 開機真的去認主（有副作用！） | ❌ 開機擋掉 |
| `STEP = "1"` | 按鈕2 一按就 `TypeError` | ❌ 開機就報（不是等按下才爆） |
| `SEND_TARGET = "First"` | — | ❌ 開機報打錯字（不靜默退回） |
| 兩顆按鈕同一支腳 | 一顆鈕同時觸發兩件事 | ❌ 開機報 |

原則：**熱路徑不做轉換、開機一次講清楚。**
（舊版 `_cid_or_bcast()` 用 `int(v)` 硬吞，等於把錯誤延後到最不方便的時刻，
或更糟 —— 讓它永遠不出現。）

### 10.5 「CPython 可以、MicroPython 不行」——離線測試抓不到的那一類

真機抓到、離線全綠也看不到的 bug：

| 寫法 | CPython | MicroPython | 後果 |
|---|---|---|---|
| `del buf[:n]`（bytearray slice 刪除） | ✅ | ❌ `TypeError: 'bytearray' object doesn't support item deletion` | **收到任何一幀都炸** → 開機同步永遠逾時、掃描永遠掃不到（而且 log 只有一行） |

修法：照 `slave/lib/sys/proto.py`（已在真機驗證）的作法 —— **緩衝區只增不刪**，
用 `start`/`end` 兩個偏移表示有效範圍，`feed()` 時把未消費的尾巴搬到前面（slice 賦值）。

防復發（`test/protocol/test_c6_remote.py`）：

1. **portability lint**：FrameRx 內不允許出現以 `del ` 開頭的程式行（註解不算）。
2. **兩個最大幀一次餵入**：走緩衝區擴充 + 搬移路徑，證明不靠刪除也能解。

> 教訓：離線測試證明的是「邏輯對」，不是「MicroPython 跑得動」。
> 凡是碰到 `bytearray`/`memoryview`/切片/`del`，都要假設 CPython 與 MicroPython 不一樣。
