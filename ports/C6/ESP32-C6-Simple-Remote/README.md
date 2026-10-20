# 兩鍵模式遙控器（最簡版 / NC4 / ESP-NOW）

> **用途**：兩顆按鈕、沒有螢幕的遙控器。
> **按鈕A** → `0x3105 MODE_SET 0x0200`（可動模式 = type 2 / id 0）
> **按鈕B** → 目前值 `+1`（本地推進：`0x0200 → 0x0201 → 0x0202 …`）
> **驗證**：對方有 `0x3103` handler 時，每次 SET 後自動用 `0x3104` 確認；沒有就不問。
> **不做**：不跑 slave 框架、不做 UI、不寫 flash、不依賴模式清單。
> **分類**：port（獨立固件，只上傳一個檔案）
> **實測**：ESP32-S3（`ESP32_GENERIC_S3`，MicroPython 1.29）；C6 亦可用，GPIO 編號不同。
> **最後更新**：2026-10

---

## 0. 行為（與舊版 C6 邏輯相同，但拿掉會失敗的部分）

```
開機
 ├─ （可選）SPOOF_MAC：把射頻 MAC 設成舊 master（才能收到它 unicast 的回覆）
 ├─ ESP-NOW：STA 重設 → PM_NONE → ESPNow active → add_peer(廣播)
 ├─ 設頻道（預設 6）
 ├─ 0x100D 廣播點名（reply_cid=0xFFFF, force=1）
 │     收 0x100E → MAC + cid（協議：cid 的唯一來源）
 ├─ 0x1101 node：讀對方的 master_cid／master_mac
 ├─ 0x1016{MASTER_CID}：把「方向」指回自己，再讀 node 確認
 ├─ 0x3103（MODE_GET）→ 有 0x3104 才啟用「SET 後確認」，並用回報值當 B 的起點
 ├─ 0x3101 單播問一次清單，印出模式（沒有回覆也不影響按鈕）
 └─ 主迴圈
      按鈕A → 0x3105(type=2, id=0, delay=0, bri=255)   ← 0x0200
      按鈕B → 0x3105(目前值 +1)                        ← 0x0201, 0x0202…
      （有 0x3103）→ 延遲 80ms → 0x3103 → 0x3104 比對
```

**舊版 C6 的三個問題與本版做法：**

| 舊版 | 問題 | 本版 |
|---|---|---|
| 開機用 `0x3103 MODE_GET` 同步 | **本樹原本沒有 `0x3103/0x3104` handler**（schema 有、registry 沒註冊）→ 每次逾時 | **已補上 handler**（`slave/action/pixel_actions.py`）；開機先探測，沒有就不問 |
| `MODE_SET` 後用 `0x3103` 確認 | 同上，必然逾時 → 印「確認逾時」＋紅燈 | 探測到 handler 才確認；確認不到只印一次警告，不影響按鈕 |
| 假設 `0x0200/0x0201` 存在 | 本樹預設 `pixel/modes/*.json` 的 id 是 1、2、3，沒有 `0x02xx` | **保留你要的 `0x0200` 邏輯**；若對方沒有這個 id，`gmode` 只會 warn 忽略（不會壞） |

> ★ 上一版（先查清單再挑兩個模式）已改掉：`MODE_LIST_QUERY` 在協議裡是
> **指定單播、必回覆**（`04_pixel_protocol.md` §4.7），而且清單不是按鈕的前置條件。
> 本版照協議順序「先 `0x100D` 發現、再單播查清單」，但**按鈕不等清單**。

---

## 1. 實機測試結果（2026-10）

| 項目 | 結果 |
|---|---|
| 連線板子 | **ESP32-S3**（`Generic ESP32S3 module`），MAC `e4:b3:23:f9:32:00` |
| 接收端 | fastLED **Master**，MAC `a0:f2:62:ea:eb:04`，node cid `0xEB04`，channel **6** |
| 初始配對 | 舊 **Mon** `58:E6:C5:6D:8A:20`（**它的真 MAC，不是冒充**）→ `master_cid=0x0001`、`bound=true` |
| `0x3101`／`0x3103` 從 `e4:b3…`（未配對） | ❌ 被拒（Master 只接受已配對 Mon） |
| 舊 Mon 58E6 → `0x1016{0xFFFF}` 單播 `addr=0xEB04` | ✅ **解除**：`master_cid=0xFFFF`、`master_mac=""`、`bound=false` |
| 新 Mon `e4:b3…` → `0x1016{0x0101}` 單播 `addr=0xEB04` | ✅ **綁定**：`master_cid=0x0101`、`master_mac=E4B323F93200`、`bound=true` |
| 配對後 `e4:b3…` → `0x3105`／`0x3103`／`0x3101` | ✅ 執行、`0x3104` 回報、17 個模式（**不冒充**） |
| FF／廣播 `0x1016` | ❌ 程式明確拒絕：`if (broadcast || addr != nodeCid) return false;` |

**結論**：舊主板沒有問題，因為它的**原廠 MAC 就是 `58:E6:C5:6D:8A:20`**，它就是當初完成配對的 Mon；
Master 記住的是它的真 MAC，所以不需要任何模仿。
新的 `e4:b3…` 是另一個 Mon，Master 的 `MonBinding::apply()` 規則是「**先綁先贏、只有主人能解除**」，
所以未解除前一切控制與回覆都會被拒。FF 廣播不是配對路徑，必須**單播到 Master 節點 CID**。

### 1.1 取模式（`0x3103` / `0x3104`）

- **repo 的 base** 原本沒有 handler；2026-10 已補上（`slave/action/pixel_actions.py`）。
- **接收端自己的 firmware 本來就有**：配對後（真 MAC `e4:b3…`），`0x3104` 正常回
  （目前 `0x0200`、`running=0`、`15000ms`）。
- 之前「沒有回」**不是 handler 缺失**，而是 Master 拒絕未配對 Mon 的控制，
  回覆也只會給已配對 Mon。

> 註：`0x3102` 依請求端點有兩種格式（對方 `fastLED` 的 `nc4_panel_list.h`）：
> - `addr=0xEB04`（Master node CID）→ 每筆 **2 bytes** `(type<<8)|id`；本遙控器用這個。
> - `addr=0x0001`（已配對 Mon／UART 舊 endpoint）→ 每筆 **6 bytes** `type,id,total_ms`（舊面板格式）。
>
> 所以 `peer.cid` 要填 `0xEB04`，不要填 `0x0001`。
> `SPOOF_MAC` 現在是 `None`：靠正常配對（主人解除 → 新 Mon 綁定）即可，不需要冒充。

base 補上的實作：

```python
# slave/action/pixel_actions.py
def on_mode_get(ctx, args):        # 0x3103
    # 讀 gmode.state() 的 mode_id / started_at，與 bus.shared["mode_start_at"]
    # → 回 0x3104 {mode_type, mode_id, elapsed_ms, total_ms=0, running}
app.disp.on(0x3103, on_mode_get)
```

遙控器這邊：

- 開機先送一次 `0x3103`；**有 `0x3104` 才**把 `_HAS_MODE_GET = True`。
- 之後每次 A/B 送完 `0x3105`，延遲 80ms 再送 `0x3103`：
  - 回報相同 → `[SET] ✅ 對方回報 0x0200（確認成功）`
  - 回報不同 → `[SET] ⚠️ 對方回報 0xXXXX（不是 …）→ 以對方為準`
  - 沒回 → 只印一次「確認不了」，不影響按鈕（舊版就是卡在這裡）。

> 配對流程（對方 `mon_pairing.cpp`）：
> 1. 舊主人（58E6）**單播** `0x1016{0xFFFF}` 到 `addr=0xEB04` → 解除。
> 2. 新 Mon（e4b3）**單播** `0x1016{0x0001}` 到 `addr=0xEB04` → 綁定。
> 3. 讀 `0x1101 node` 確認 `master_mac=E4B323F93200`。
> **不能 FF 廣播**（`if (broadcast || addr != nodeCid) return false;`）。

### 1.2 Master ID（`0x1016 SET_MASTER`）

- fastLED Master 的 **node cid = `0xEB04`**（`node` 的 `cid`）。
- 遙控器（**Mon**）的 CID = **`0x0001`**（`node` 的 `master_cid`）。
- `0x1016{0x0001}` 必須**單播到 `addr=0xEB04`**；Master 未綁定、或來源是 owner 才會套用。
- 舊 owner 58E6 未解除前，`e4:b3…` 送 `0x1016` 會被 `MonBinding::apply()` 拒絕
  （所以 `master_mac` 不變，不是 handler 缺失）。
- 解除後（owner 58E6 送 `0x1016{0xFFFF}`），`e4:b3…` 送 `0x1016{0x0001}`：
  `master_mac=E4B323F93200`、`bound=true`。

實機（配對成功後）：

```text
[NODE] cid=0xEB04  master_cid=0x0101  master_mac=E4B323F93200  bound=True
[BIND] 對方回報 master_cid=0x0101 master_mac=E4B323F93200 bound=True
```

> 舊 board `58:E6…` 不需要冒充；它的真 MAC 就是 58E6，是原本的 owner。
> 規則：**先綁先贏、只有 owner 能解除/改**；FF 廣播無效。

### 1.3 歷史：MAC 冒充（已不需要）

早期不知道 owner 解除流程，才把遙控器 MAC 設成 `58:E6…` 冒充舊主人，這樣 Master 就接受。
現在正確做法是 §1.4 的「owner 解除 → 新 Mon 綁定」，`SPOOF_MAC` 保持 `None`。

```text
⚠️ SPOOF_MAC 已套用 → 58:e6:c5:6d:8a:20
✅ ESP-NOW 已啟動  my_mac=58:e6:c5:6d:8a:20
[RX] cmd=0x100E ... src=a0:f2:62:ea:eb:04
[NODE] cid=0xEB04  master_cid=0x0001  master_mac=58E6C56D8A20  bound=True
[RX] cmd=0x3104 ... → 目前 0x0202 (type=2 id=2 running=1 7000ms)
[RX] cmd=0x3102 ... → 模式清單（共 17 個）
ℹ️ 模式清單（共 17 個）：0x0100 … 0x010C 0x0200 0x0201 0x0202 0x0203
[SET] A：0x0200 … → [RX] 0x3104 → [SET] ✅ 對方回報 0x0200（確認成功）
[SET] B：0x0201 … → [RX] 0x3104 → [SET] ✅ 對方回報 0x0201（確認成功）
```

**模式清單（17 個）**：

| 組別 | ID |
|---|---|
| LED（type 1） | `0x0100` ~ `0x010C`（13 個） |
| SERVO（type 2） | `0x0200` ~ `0x0203`（4 個） |

> ⚠️ **B 的 +1 範圍**：從 `0x0203` 再按 B 會送 `0x0204`（不在清單，對方忽略）。
> 若要在現有模式間循環，把 `next_mode_of()` 改成「依 `0x3102` 清單前進」即可。
>
> 另外，接收端在 SET 後會主動送 `0x3105`／`0x3106`（回音）與 `0x3113`（1 byte，
> 用途未明）給 master；不影響按鈕，但 `DEBUG=True` 時會看到。

### 1.4 配對規則（對方 fastLED `mon_pairing.cpp`）

Master 只接受**已配對 Mon**的控制；配對不是 FF，也不是隨便送 `0x1016`：

| 動作 | 條件 | 結果 |
|---|---|---|
| `0x1016{cid}` 綁定 | 尚未綁定（`bound=false`） | ✅ 記住來源 MAC |
| `0x1016{cid}` 換主人 | 已綁定，來源 ≠ owner | ❌ `apply()` 回 false |
| `0x1016{0xFFFF}` 解除 | 已綁定，來源 = owner | ✅ 清空 `master_mac` |
| `0x1016` 任何 | 電波 FF／`addr != nodeCid` | ❌ 直接 `return false` |

實測（Master `a0f2` / node cid `0xEB04`）：

| 步驟 | `node` |
|---|---|
| 初始（舊 Mon 58E6） | `master_cid=1, master_mac=58E6C56D8A20, bound=true` |
| 58E6 → `0x1016{0xFFFF}` 單播 `addr=0xEB04` | `master_cid=65535, master_mac="", bound=false` |
| e4b3 → `0x1016{0x0001}` 單播 `addr=0xEB04` | `master_cid=1, master_mac=E4B323F93200, bound=true` |
| e4b3 → `0x3105{0x0200}` | `0x3104` 回 `0x0200` ✅ |

- 舊主板不需要模仿：它的**真 MAC 就是 `58:E6:C5:6D:8A:20`**，是原本的 owner。
- 新遙控器 `e4:b3…` 被拒是「先綁先贏」；主人解除後就能綁。
- 不需要 `SPOOF_MAC`；USB `pairing clear` 是遙控器遺失時的救援。

> **完整合約（給 fastLED 實作）**：[doc/01_protocol/10_mon_master_pairing_contract.md](../../../doc/01_protocol/10_mon_master_pairing_contract.md)
> —— 以 Mon 規格為準：FF 可認領／解除、任何來源可解除、`pair_claimed` 只存記憶體、重啟可換手。

---

## 2. 接線

```
        ┌──────────── ESP32-S3 / C6 ─────────────┐
按鈕A   │  GPIO11 ──┤ ├── GND                     │  內部上拉：按下 = 低電位
按鈕B   │  GPIO12 ──┤ ├── GND                     │  去抖 30ms
        └──────────────────────────────────────────┘
```

- 兩顆都直接接 GND，不需要外部電阻。
- ⚠️ **GPIO12/13 是原生 USB 的 D-/D+**。按鈕接 12 會干擾那個 USB 埠；
  燒錄／看 log 請用另一個 USB（UART）埠，或把按鈕換到 GPIO2/3。
- ⚠️ GPIO8（板載 RGB）、GPIO9（BOOT）、GPIO15 是 strapping，不建議當按鈕。
- GPIO11 在 S3/C6-DevKitC 都是正常 GPIO。

---

## 3. 上傳（`main.py` ＋ `remote_config.json`）

```bash
# 先備份板上的 main.py
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX fs cp :main.py ./main_backup.py

# 上傳程式 + 設定檔（兩個都要）
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX \
    fs cp ports/C6/ESP32-C6-Simple-Remote/main.py :main.py
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX \
    fs cp ports/C6/ESP32-C6-Simple-Remote/remote_config.json :remote_config.json

python3 -B -m mpremote connect /dev/cu.usbmodemXXXX reset
```

> 只改設定時，**只需重傳 `remote_config.json`**，不用重傳 `main.py`。
> 開機 log 會印 `[CFG] 讀取 /remote_config.json`；讀不到就用內建預設。

想先並存測試（不覆蓋 `main.py`）：

```bash
python3 -B -m mpremote connect /dev/cu.usbmodemXXXX \
    fs cp ports/C6/ESP32-C6-Simple-Remote/main.py :simple.py

# REPL：import simple; simple.boot()   → 只做開機流程
#       simple.set_mode(0x0200, "A")  → 手動送一次
#       simple.main()                 → 進主迴圈（按鈕）
```

---

## 4. 設定檔 `/remote_config.json`

`main.py` 開機讀 `/remote_config.json`；改完上傳、重開機生效。讀不到 → 用預設（印警告）。

```json
{
  "buttons": { "a_pin": 11, "b_pin": 12 },
  "channel": 6,
  "my_cid": "0x0101",
  "address_mode": "own",
  "spoof_mac": "58e6c56d8a20",
  "peer": { "mac": "a0f262eaeb04", "cid": "0xEB04" },
  "mode": { "a": "0x0200", "step": 1 },
  "leds": [
    { "pin": 48, "brightness": 100 }
  ],
  "debug": true
}
```

| 欄位 | 說明 |
|---|---|
| `buttons.a_pin` / `b_pin` | 兩顆按鈕 GPIO |
| `channel` | 對方頻道（`null` = 掃 1..13） |
| `my_cid` | **你的 Mon ID**（`0x0101`；`0x1016` payload） |
| `address_mode` | `own` = 原廠 MAC（預設）；`spoof` = 冒充 `spoof_mac`；`ff` = `FF:FF:FF:FF:FF:FF` / `addr=0xFFFF` |
| `spoof_mac` | 只有 `spoof` 模式用；舊 Mon 的真 MAC（`58e6…`） |
| `peer.mac` | fastLED Master 的 MAC（`a0f262eaeb04`） |
| `peer.cid` | Master 的 node CID（`0xEB04`）；**不是** `0x0101` |
| `mode.a` / `step` | 按鈕A 的模式；按鈕B 的增量 |
| `leds` | 清單：`pin`（`null` 跳過）、`brightness` 0..100、可選 `active_low` |
| `debug` | 印 TX/RX |

> `broadcast: true` 是舊欄位，等於 `address_mode: "ff"`（仍相容）。

### 4.0.1 三態 `address_mode`

| 模式 | 自己的 MAC | `SEND_MODE` | 用途 |
|---|---|---|---|
| `own` | 原廠 `e4:b3…` | `unicast` | **預設**；配對後正常控制（A/B + 確認） |
| `spoof` | `spoof_mac`（`58:E6…`） | `unicast` | 對方只認舊 Mon 時；舊面板必須關機 |
| `ff` | 原廠 | `broadcast` | 不需單播的控制；`0x3105` 會執行，但 `0x3103` 不回 → **FF 下沒有確認** |

REPL 可即時切換（會重啟 ESP-NOW 套用 MAC）：`m.set_address_mode("own"|"spoof"|"ff")`。

> `own` 的原廠 MAC 取自 **`machine.unique_id()`**，不是 `sta.config("mac")`。
> 這樣 soft reboot 才不會把上一次的 spoof MAC 誤當成原廠（曾發生 `mode=own` 但 `my_mac=58:e6…`）。


### 4.1 誰是幾號（實測）

```json
// 0x1101 node 的回報（配對成功後）
{"node":{"cid":60164,"master_cid":257,"master_mac":"E4B323F93200","bound":true}}
```

| 值 | 是誰 | 設定欄位 |
|---|---|---|
| `cid=60164` = `0xEB04` | fastLED **Master** 的 node CID（被控端） | `peer.cid` |
| `master_cid=257` = `0x0101` | **Mon**（遙控器）的 CID | `my_cid` |
| `master_mac=E4B3…` | Mon 的**真 MAC**（配對後） | 不用設 |

> fastLED 那邊把 `a0f2…` 叫 Master、把遙控器叫 Mon；NC4 的 `master_cid` 指的是 Mon。
> 名稱相反，看數值最準。

- **MAC 不用設**：遙控器每一幀都帶自己的真 MAC。
- **配對**（第一次或換 Mon）：
  1. 舊 owner 單播 `0x1016{0xFFFF}` 到 `addr=0xEB04`（解除）。
  2. 新 Mon 單播 `0x1016{my_cid}` 到 `addr=0xEB04`（綁定）。
  3. 讀 `node` 確認 `master_mac` 是自己的 MAC。
  - `0x1016` **不能 FF 廣播**；`peer.cid=0xFFFF`／`broadcast=true` 只能用在控制，不能用來配對。
- 舊板 58E6 不需要冒充：它的真 MAC 就是 `58:E6:C5:6D:8A:20`，是原本的 owner。
  現在 owner 已換成 `e4:b3:23:f9:32:00`。

### 4.2 LED

```json
"leds": [
  { "pin": 48, "brightness": 100 },
  { "pin": null, "brightness": 50 }
]
```

- 開機／匯入中：全暗；**準備好**：依 `brightness` 亮；失敗：快閃 6 下。
- `pin: null` = 跳過；`brightness` 0..100；`active_low` 可選（低電位亮 → `true`）。
- REPL 可調：`import main as m; m.pwm_led(0, 50)`（第 0 個燈 50%）。

按鈕B 的推進（本地）：

| 目前值 | 按下 B 送出 |
|---|---|
| `0x0200` | `0x0201` |
| `0x0201` | `0x0202` |
| `0x02FF` | `0x0300`（會跨組；舊版行為） |
| `0x0000` | `0x0201`（用 `STEP_BASE_IF_UNKNOWN` 當基準） |
| `0xFFFF` | `0x0000` |

> `STEP_BASE_IF_UNKNOWN = None` → 純 16-bit +1（`0x0000 → 0x0001`）。

---

## 5. 開機 log（實機，最終版；不冒充）

```text
=== 兩鍵模式遙控器（A=0x0200 / B=+1）===
[CFG] 讀取 /remote_config.json
✅ ESP-NOW 已啟動  my_mac=e4:b3:23:f9:32:00  mode=own
⚠️ 按鈕B 接 12 → GPIO12 = USB D-（C6/S3 的原生 USB 埠）
[TX] 0x100D dst=ffffffffffff addr=0xFFFF len=18 ok
[RX] cmd=0x100E addr=0xFFFF src=a0:f2:62:ea:eb:04 len=18
[SCAN] 頻道 6｜回應 0x100D 的節點：
       MAC=a0:f2:62:ea:eb:04  cid=0xEB04  slave_id=A0F262EAEB04
[TARGET] 固定 PEER_MAC=a0:f2:62:ea:eb:04  cid=0xEB04
[CFG] SEND_MODE=unicast（單播 dst=PEER / addr=cid）
[RX] cmd=0x1102 addr=0x0001 src=a0:f2:62:ea:eb:04 len=80
[NODE] cid=0xEB04  master_cid=0x0101  master_mac=E4B323F93200  bound=True
[BIND] 0x1016 SET_MASTER{master_cid=0x0101}
[TX] 0x1016 dst=a0f262eaeb04 addr=0xEB04 len=15 ok
[BIND] 對方回報 master_cid=0x0101 master_mac=E4B323F93200 bound=True
[TX] 0x3103 dst=a0f262eaeb04 addr=0xEB04 len=13 ok
[RX] cmd=0x3104 addr=0x0001 src=a0:f2:62:ea:eb:04 len=11
ℹ️ MODE_GET 可用：目前 0x0108 (type=1 id=8 running=1 83000ms)
[RX] cmd=0x3102 addr=0x0001 src=a0:f2:62:ea:eb:04 len=36
ℹ️ 模式清單（共 17 個）：0x0100 … 0x010C 0x0200 0x0201 0x0202 0x0203
✅ 按鈕A（GPIO11）→ 0x0200（固定）
✅ 按鈕B（GPIO12）→ 目前值 +1（本地推進，起始 0x0108）
[RUN] 待機中：A=GPIO11 / B=GPIO12
```

按 A / B（已配對）：

```text
[TX] 0x3105 dst=a0f262eaeb04 addr=0xEB04 len=18 ok
[SET] A：0x0200 (type=2 id=0) 已送出
[TX] 0x3103 dst=a0f262eaeb04 addr=0xEB04 len=13 ok
[RX] cmd=0x3104 addr=0x0001 src=a0:f2:62:ea:eb:04 len=11
[SET] ✅ 對方回報 0x0200（確認成功）
```

## 5.1 REPL 指令（Ctrl-C 後）

| 指令 | 作用 |
|---|---|
| `m.pair()` | `0x1016{my_cid}` 綁定，再讀 `node` 確認 |
| `m.unpair()` | `0x1016{0xFFFF}` 解除（目前 owner 可） |
| `m.spoof_unbind(True)` | **以防萬一**：冒充 `spoof_mac` 解綁 → 還原 own → 重新綁定 |
| `m.spoof_unbind(False)` | 只解綁，還原 own、不重新綁定 |
| `m.modes()` | 印模式清單 |
| `m.get_node()` / `m.get_mode()` | 讀 `node` / 目前模式 |
| `m.set_send_mode("broadcast")` | 切 FF（控制用；不套 MAC） |
| `m.set_address_mode("own"\|"spoof"\|"ff")` | 切三態位址模式（會重啟 ESP-NOW） |

完整配對合約（給 fastLED）：[doc/01_protocol/10_mon_master_pairing_contract.md](../../../doc/01_protocol/10_mon_master_pairing_contract.md)。

---

## 6. 疑難排解

| 症狀 | 原因／處理 |
|---|---|
| `0x100D` 沒節點回應 | 頻道不對（先確認對方 `Network.ESP_now.channel`）；或對方 ESP-NOW 沒 active |
| 有 `0x100E`，但 `0x3101/0x3103` 的回覆看不到 | Master 只接受**已配對 Mon**；先讀 `node.master_mac`，不是自己的 MAC 就依 §1.4 換 Mon |
| `[SET]` 後沒有 `✅ 對方回報` | 同上：未配對／owner 不是這顆遙控器 |
| 按 A/B 有 `[TX] ... ok`，但燈沒變 | `(type,id)` 不在對方的模式池（`gmode` 只 warn 忽略）；用 `m.modes()` 看清單 |
| 按 A/B 完全沒有 `[TX]` | 按鈕接線／GPIO 不對 |
| 第一次單播 `FAIL（沒 ACK）`，第二次 ok | 對方還沒認識你的 MAC；第二次已進 peer 表。屬正常 |
| 自動掃頻道停在錯誤的頻道 | `sta.config(channel=...)` 有切換延遲；本版固定 `CHANNEL=6`，並在換頻道後等 120ms |
| USB 一直重連／燒不進去 | 按鈕接在 GPIO12/13（USB D-/D+）→ 換腳或用 UART 埠 |
| `mode=own` 但 `my_mac=58:e6…` | soft reboot 保留了 spoof MAC；本版改用 `machine.unique_id()` 還原，重傳 `main.py` 即可 |
| 舊控制面板上線後行為怪異 | 它已不是 owner；Master 只認目前配對的 Mon。要換回去：目前 owner 解除 → 舊板重新綁 |

### 換 Mon（不用冒充、不用改 firmware）

1. **舊 owner** 單播 `0x1016{0xFFFF}` 到 Master 的 node CID（`0xEB04`）。
2. **新 Mon** 單播 `0x1016{my_cid}` 到 `0xEB04`。
3. 讀 `0x1101 node`，確認 `master_mac` = 新 Mon 的 MAC。

- `0x1016` **不能 FF 廣播**；必須射頻單播、`addr = Master node CID`。
- 遙控器遺失：接 Master USB，跑 `pairing clear`。

---

## 7. 離線驗證

| 項目 | 方法 | 結果 |
|---|---|---|
| `0x3105` 幀 | 與 `slave/lib/sys/proto.py` 的 `Proto.pack` 逐位元組比對 | 相同 |
| A = `0x0200` | `set_mode(0x0200)` | payload `02 00 00 00 FF` |
| B = +1 | `next_mode_of` ×3 | `0x0201`、`0x0202` |
| `0x0000` 基準 | `next_mode_of(0)` | `0x0201` |
| 清單缺失 | 假射頻不回 `0x3102` | `boot()` 仍成功，按鈕可用 |
| `0x3103` 缺失 | 假射頻不回 `0x3104` | 自動跳過確認，不逾時、不影響按鈕 |
| `0x3103` 可用 | 假射頻回 `0x3104` | SET 後顯示 `✅ 對方回報 0x0200`；回報不同時以對方為準 |
| `node` / `0x1016` | 舊 owner 58E6 單播 `0x1016{0xFFFF}` → e4b3 單播 `0x1016{0x0001}` | ✅ 解除後綁定：`master_mac=E4B323F93200`、`bound=true`（見 §1.4） |
| **實機・清單** | 真 MAC、`addr=0xEB04`，送 `0x3101{0}` | ✅ `0x3102`：**17 個模式**（0x0100–0x010C、0x0200–0x0203） |
| **實機・取模式** | 真 MAC、`addr=0xEB04`，送 `0x3103` | ✅ `0x3104`：`0x0200`、`running=0`、`15000ms` |
| **實機・A/B 確認** | 真 MAC、`addr=0xEB04`，送 `0x3105` → `0x3103` | ✅ `對方回報 0x0200` / `0x0201`（確認成功） |
| 實機・FF | `0x1016` 用 `FF:FF:FF:FF:FF:FF` | ❌ 對方 `if (broadcast) return false`（配對必須單播） |
