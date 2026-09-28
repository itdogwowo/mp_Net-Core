# ports/S3/ESP32-S3-Control_Panel — S3 控制面板裝置

## 板子

ESP32-S3 + ST7789 TFT（SPI，240×320，LVGL 橫屏顯示）+ 旋轉編碼器
（A/B = GPIO18/8、按壓 = GPIO17）+ 按鍵（GPIO42）+ microSD（slot0 / 4-bit）
+ UART1（GPIO39/41）+ ESP-NOW。

**沒有**本地燈效 / 電機 / I2S 音訊硬體（`WS2812` / `APA102` / `PCA9685` /
`uartMotor` / `I2S` 全部 `enable: 0` → boot 不會建出 `st_pixel`）。

## 角色：面板只發指令，不自己執行

```
使用者操作（encoder / 按鍵 / LVGL 頁面）
  → LVGL 頁面只寫狀態：bus.shared["_display_cmd"] / ["_pixel_cmd"]
  → ControlPanelTask      消費 _display_cmd → 廣播 0x1501 WTT_CTL
    PixelControlPanelTask 消費 _pixel_cmd   → 廣播 0x3105 MODE_SET / 0x3106 MODE_STOP
  → ESP-NOW（NowBus）→ 執行裝置自己去解碼、自己執行
回程：執行裝置 0x1502 WTT_STATUS → 本板 dispatch（waiting_to_trash_actions.on_status）
  → 寫 _display_* Global → LVGL 頁面顯示「已確認 / 倒數」
```

⚠️ **ESP-NOW 是面板的生命線**：`Network.ESP_now.enable` 必須為 1，且
`channel` 要與執行裝置一致（兩邊都是 6）。少了它，UI 有畫面但送不出任何指令。

## delta 檔案清單（相對 slave/ 覆蓋，一次過上傳）

| 檔案 | 內容 |
|---|---|
| `config.json` | 本板硬體 + Router 解碼介面（見下） |
| `Core_Manager.py` | 面板角色的任務集（見下） |

> 這個 port **沒有** `pixel/` 或 `schedule.json` delta：面板不當播放端，
> 排程用 base 的空範本即可。

## 任務集（`Core_Manager.py`，與 slave 的差異）

| 分區 | 任務 | affinity |
|---|---|---|
| 系統核心 | `network` / `circuit` / `bus_decode` / `now` | `(1,0)` core0 |
| 系統核心 | `fs_scan` / `hw_sample` | `(0,1)` **core1** |
| 系統核心 | `log`（故意排最後） | `(1,0)` core0 |
| 應用 | `web_ui` | `(0,0)` 任一核 |
| 應用 | `lvgl`（`bus.has_lcd()` 才註冊） | `(1,0)` core0 |
| 面板 | `cpanel` / `pixel_cpanel` / `schedule` | `(1,0)` core0 |

**刻意不註冊**（要復活就把 `Core_Manager.py` 裡的註解打開）：

| 任務 | 為什麼 |
|---|---|
| `pixel` / `render` | 本地燈效的計算核 + 播放核。面板沒有燈硬體 → `st_pixel=None` 自行停用。面板要看燈效是「廣播 MODE_SET 給執行裝置」，不是自己播 |
| `stream` | 0x30xx 串流播放（讀 `data.bin` 逐幀推燈）。面板不當播放端 |
| `dj` / `audio_player` | 音訊合成 + I2S 播放。面板沒有音訊硬體 |
| `motor` / `action` | 本板 UART1 沒掛電機，也不當動作執行端 |

三個容易踩的順序／歸屬約束（`Core_Manager.py` 內有完整說明）：

1. **`log` 放最後** —— `log_task_ready` 一設，其它 task 的 `print` 就改走環形
   緩衝；先起會把 boot 期的 driver 訊息吸走。
2. **`now` 與 `network` 都要留** —— 兩者都會建 `NowBus` 但互相 reuse（先查
   bus service），不會二次 `espnow.active(True)` 撞 `ESP_ERR_ESPNOW_EXIST`。
   誰先跑完不確定，缺一個在時序不利時就沒有 ESP-NOW。
3. **encoder A/B 只能在 config 的 `ENC` 段**，不要放進 `PIN` 段 —— `ENC` 段給
   `enc_drv` 建 `machine.Encoder` 並註冊 `enc_list`，`HwSampleTask` 採樣進
   `_hw_inputs` 給 LVGL 消費。`PIN` 段再加 `encA`/`encB` 會讓 boot Phase 1
   報 GPIO 衝突並 `SystemExit`。

## 「哪些線進解碼鏈」＝ `Router`（本 port 不設 `CircuitDecode`）

`Router` 是唯一的解碼介面宣告處。`{"in": X, "out": []}` ＝ `V_DROP` ＝
明確不執行；`out: ["self"]` ＝ 本地執行；`out: ["now"]` ＝ 從 ESP-NOW 送出去。

| 介面 | 設定 | 理由 |
|---|---|---|
| `self` | `["now"]` | **本機發起（vBus 注入，例如 `schedule`）→ 從 ESP-NOW 送出去**。不含 `self` ＝ 不在本地再 dispatch 一次 |
| `now` | `["self"]` | ESP-NOW 進來的幀本地執行（0x1502 寫 `_display_*`），**不再轉發**（避免回音） |
| `net` / `udp` | `["self"]` / `[]` | master 的 WS / 發現通道；wifi+lan 都 0 時**不存在**，開機時會被標「暫不註冊」，通道上線才自動註冊（內容照 config） |
| `uart0` | `[]` | UART1(39/41) 未使用；**待決，見下** |

**實測（`Router.enable=1`，只有 `now` + `uart0` 上線時）**：

```
  生效的路由表:
    'self'  out=['now']     auto=False  verdict=V_FORWARD
    'now'   out=['self']    auto=False  verdict=V_EXECUTE
    'uart0' out=[]          auto=False  verdict=V_DROP

  gate: self → V_FORWARD（now.written=1，訊號有送出）
        now  → V_EXECUTE（沒有再噴回去）
        uart0→ V_DROP
```

> **`self` 為什麼不含 `self`**：`out` 裡的 `self` 是「本地執行」開關
> （`_verdict_of()` 判定），不是「送給自己」。面板沒有本地硬體（`st_pixel=None`），
> 本地執行是空轉，所以排程注入的幀**只外送、不在本地 dispatch**。
> 若哪天要「本機發起 → 本地也執行一份」，把它改成 `["self", "now"]` 即可。
>
> 副作用（可接受）：排程送出的指令**不會**更新面板自己的快取狀態；
> 面板顯示的仍是執行裝置回來的事實（`0x1502` / `0x3104`）—— 對控制器而言這是對的。

### `vbus` 不是可路由的介面

`vbus`（內部虛擬總線）**只負責發送**，它注入的幀一律以來源名 **`self`** 進 Router
（判別：`is_local_bus()`，即 `io is None`）。所以：

- `Router.routes` 裡**不該出現** `in: "vbus"` —— 那會是第二個名字指同一件事。
  真的寫了會被當成「不存在的通道」跳過。
- `out` 裡寫 `vbus` 會被**無視**（它不是出口，`CircuitBus(None).write()` 永遠回 `False`）。

要讓排程的指令往外走，寫的是 **`{ "in": "self", "out": [...] }`**（本檔就是這樣）。

### 介面名 `uartN` 的 N 是 **`UART.list` 的索引（0-based）**

本板 `UART.list` 只有一筆（`id: 1` = `machine.UART(1)`，GPIO39/41），它是**索引 0**
→ 介面名 **`uart0`**。**不是** `uart1`（那會是 `id`）、不是 `uart_0`。

```
UART.list[0]  →  label "CIRCUIT-UART0"  →  邏輯名 uart0
UART.list[1]  →  label "CIRCUIT-UART1"  →  邏輯名 uart1     （本板沒有）
```

`config` 的 `id` 是 `machine.UART(id)` 的硬體周邊號（1 起），**只給 `init_uart` 用**，
不出現在介面名裡 —— 所以換硬體 id 時不必改 Router。

> ⚠️ 這個規則是**改過的**（原本介面名取 `item["id"]`）。舊寫法在 `id` 與 list 位置不一致時
> 會讓 `uart1`/`uart2` 兩個名字同時都是騙人的。現在與 `uart_list[idx]`、
> `CircuitDecode.GPIO.uart`、`bus_speed.bus_id` 三層的索引語意對齊。
>
> **遷移對照**：`uart1`（舊，= id 1）→ **`uart0`**（新，= 索引 0）。有 2 條線的板子
> （`P4-ETH_mp3`）要一起改：舊 `uart1`→`uart0`、舊 `uart2`→`uart1`。

### ⚠️ `_autofill()` 會**默默改寫你的 config.json**

這是最容易咬人的一條。`CircuitBus` 的 label 是 `CIRCUIT-UART{idx}`（`idx` = list 索引），
所以若 config 寫的名字對不上（例如舊寫法 `uart1`，或直覺的 `uart_0`）：

```
routes: [{ "in": "uart_0", "out": [] }]
  → ifaces 裡沒有 "uart_0" → 這條 route 永遠不生效（靜默，沒有警告）
  → 真正的 "uart0" 沒有 route
  → _autofill() 補上 { "in": "uart0", "out": ["self"] }
  → bus_decode.py 的 _persist_autofill() 呼叫 cfg_manager.save_from_bus("Router")
  → **寫回 config.json**
```

兩個後果：① 該線的 disable 意圖**反過來變成「本地執行」**；
② 你手動刪掉的 route，**下次開機會被補回來**（`_auto_done` 每個 session 清空）。

`_KNOWN_IFACES` 定義了合法介面名清單，但**全樹沒有任何地方讀它** ——
所以打錯名字不會有任何提示，只能靠這份文件。改 config 後請確認
`ROUTER_TABLE_GET`（或開機 log）顯示的介面名與你寫的一致。

### ⚠️ `out` 填 `[]` 不能改用「刪掉那條 route」

`SignalRouter._autofill()` 會為「實際存在、但 config 沒寫 route」的線路補上
`{"in": X, "out": ["self"]}` 並**寫回 config.json** —— 刪掉等於下次啟動被補成
「本地執行」，語意反了。要 disable 就得明寫 `[]`。

### ⚠️ 這條路的代價（相對舊 `CircuitDecode.enable: 0`）

| | `CircuitDecode.enable: 0` | `Router.enable: 1` + `out: []` |
|---|---|---|
| 該線不參與解碼 | ✅ | ✅ |
| UART 實體還會被輪詢嗎 | **不會**（沒進 `bus_sources` → `BusDecodeTask` 不碰） | **會** —— `CircuitTask` 是 `self._buses = all_buses`（`circuit.py:74`）且每輪全 poll（`circuit.py:200`），進 `rx_hub` 後才被 Router 丟掉 |
| 開機訊息 | 乾淨 | 每個 disable 的既有線路警告一次 `'out' 是空的 → 這條 route 沒有作用` |

**要真的零成本，正解是把 `UART.enable` 改 0。** 本 port 目前**保留
`UART.enable: 1`**（腳位凍結決定），所以上表第二列的成本還在。

> **更新**：UART **要拿來用**（收發）。但「用」需要四個環節都到位，
> 現在只到第 1 環 —— 見下方待決事項 ①。

## 待決事項

1. **UART 要「收發」還缺三個零件。** 接收路徑四段，現況：

   | # | 環節 | 現況 |
   |---|---|---|
   | 1 | `CircuitTask` poll UART → `rx_hub` | ✅ 一直在跑 |
   | 2 | 幀進 `bus_sources`（給 `BusDecodeTask` 解 NC4） | ❌ 需要 `CircuitDecode.enable=1` |
   | 3 | Router 放行 | ❌ 現在 `uart0` 是 `out: []` → `V_DROP` |
   | 4 | **有人讀 raw byte 並解析** | ❌ **不存在** |

   第 4 環是關鍵：`ControlPanelTask._poll_ex_ic()` 讀的 `_ex_ic_slot` **全 repo
   沒人寫**，而且它等的 cmd `0x1403` 早已被指派給 `SPEED_SET`（`schema/hw.json`）
   —— 那段是舊協議殘留。

   真正的 EX-IC 5-byte 格式在**執行裝置**端（`action_task_1.py`）：
   `[0xB4][mode][brightness 0-31][time][0xFF]`，它用 `CircuitBus.read_into()`
   直接讀 raw byte 自己解析，**不經過 NC4 解碼鏈**。

   → 待定：面板要「收」的是**誰發的、什麼格式**？
   - 若是同樣的 `0xB4` 5-byte 幀 → 在 `ControlPanelTask` 綁 `circuit_bus_uart0`
     讀 `read_into()` 並解析，**不需要**動 `CircuitDecode` 或 Router（第 2、3 環
     都不需要）。
   - 若是 NC4 幀 → 需要 `CircuitDecode.enable=1` + `uart0: ["self"]` + 一個
     action handler，而且要先挑一個沒被佔用的 cmd 碼。
   - ⚠️ 不要用 `ActionTask1` 來讀：它 `_uart = uart_list[0]` 與 `CircuitBus`
     包的是**同一個 UART 物件**，且在 `_try_bind_circuit_bus()` 失敗時會
     fallback 直接 `self._uart.read()` → 同一條線兩個讀者，幀被瓜分。
2. **UI 的 ESP-NOW 硬體直發還沒收**：`ui/lvgl/page/pixel_controller.py` 的
   `_set_mode()` / `_apply_movable()` 仍直接抓底層 `espnow` 物件 `esp.send()`
   （`doc/03_notes/16_pixel_panel_temp_hacks.md` §2.1）。因此
   `PixelControlPanelTask` 目前**只實際收到 brightness**（`_adj_bright` 寫
   `_pixel_cmd`），mode 路徑 `_broadcast_mode_set()` 還沒被走。正規做法是 UI
   只寫 `bus.shared["_pixel_cmd"] = {"mode": id}` 由 task 轉發。
3. **`ControlPanelTask` 的 `_poll_ex_ic()`（0x1403）是全樹死碼** ——
   `_ex_ic_slot` / `_ex_ic_pending` 兩個 key 全 repo 只有它自己讀，沒有任何地方
   寫。0x1403 這個 cmd 碼現在是 `SPEED_SET`（`hw_actions.py`）。那段是舊協議殘留。
   > ⚠️ 面板收狀態**不要**改推 `0x1502` → `waiting_to_trash_actions.on_status` ——
   > 那是 `waiting_to_trash` 系列（命名即待廢）。面板的 pixel 狀態走 `0x3104
   > MODE_GET_RSP`，見 `doc/03_notes/18_pixel_panel_control_path.md`。

4. **面板的 pixel 控制路徑（模式／計時／亮度）盤點與設計草案** →
   `doc/03_notes/18_pixel_panel_control_path.md`（含三個斷點的證據、協議現成指令、
   亮度統一 `0–31`、以及三個未解技術問題）。

## 設定備註

- **`System.watchdog.auto_rearm_ms` 必須是 0。** `enable: 0` 且 `auto_rearm_ms > 0`
  時，`init_watchdog()` 會啟動倒數：開機後連續 N ms 沒收到任何有效指令封包
  → 自動存 `enable=1` + `machine.reset()`。**面板是沒人對它發指令的裝置**
  （它只往外廣播），silence 是常態 → 留 60000 會讓面板開機約 1 分鐘後自己重啟並
  把 WDT 打開。要 WDT 保護就直接 `enable: 1`。
- `System.frame_interval_ms` / `num_pixels` 在本板沒有實際作用（沒有本地 pixel
  播放端），保留是為了與其他 port 一致、日後要開 `stream` 時不必再補。
- `TFT.rotation` **必須維持 0**：`ui/lvgl/lvgl_init.py` 自己送 MADCTL(0x60) 轉橫屏，
  改 1 會 double-rotate。
- `System.cID`：本 port 指派 `"0001"`。未指派時 `bus.cid = 0xFFFF` ＝ 廣播，
  每一站都會重複執行廣播幀（`todo/03_signal_router.md` 的待決項）。

## 上傳

```bash
# 1) 先上傳 slave/ 基礎（全量）
# 2) delta 覆蓋：
python -m mpremote connect <COM> fs cp ports/S3/ESP32-S3-Control_Panel/config.json :/config.json
python -m mpremote connect <COM> fs cp ports/S3/ESP32-S3-Control_Panel/Core_Manager.py :/Core_Manager.py
# 3) RESET
```

> 開機後看 `[BOOT] ok / FAIL` 摘要與 `[CoreManager]` 的任務清單，
> 確認 ESP-NOW 有起來（`✅ [NOW-Bus] ESP-NOW active, channel=6`）。
