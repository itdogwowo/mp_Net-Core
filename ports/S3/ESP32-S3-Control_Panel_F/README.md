# ports/S3/ESP32-S3-Control_Panel_F — S3 控制面板裝置

## 板子

ESP32-S3 + ST7789 TFT（SPI，240×320，LVGL 橫屏顯示）+ 旋轉編碼器
（A/B = GPIO41/42、按壓 PUSH = GPIO2）+ 按鍵 KO（GPIO1）+ microSD（slot0 / 4-bit）
+ ESP-NOW。**UART1 已關閉**（原 GPIO39/41；41 已改作 encoder A）。

**沒有**本地燈效 / 電機 / I2S 音訊硬體（`WS2812` / `APA102` / `PCA9685` /
`uartMotor` / `I2S` 全部 `enable: 0` → boot 不會建出 `st_pixel`）。

### 接線（新板）

| 模組 | 訊號 | GPIO | 備註 |
|---|---|---|---|
| ST7789 | SCL | 21 | SPI SCK |
| ST7789 | SDA | 14 | SPI MOSI（`SPI.data_pins[0]`）|
| ST7789 | RES | 13 | `tft_rst` |
| ST7789 | DC | 12 | `tft_dc` |
| ST7789 | CS | 11 | `tft_cs` |
| ST7789 | BLK | 10 | `tft_bl` 背光 |
| 遙控器 | A | 41 | `ENC.a` |
| 遙控器 | B | 42 | `ENC.b` |
| 遙控器 | PUSH | 2 | `encC`（編碼器按壓）|
| 遙控器 | KO | 1 | `btn`（獨立鍵）|
| microSD | CLK / CMD / D0–D3 | 7 / 6 / 15,16,4,5 | slot0 4-bit |
| — | UART1 | — | **關閉**（原 39/41，41 已給 encoder A）|

> ST7789 的 GND / 3.3V 照模組標示接。`SPI.GPIO.mosi` 留 `null`，
> 由 `data_pins: [14]` 在 lcd_bus 與 `machine.SPI` fallback 兩條路都對到 MOSI。
>
> 按鍵語意目前採用 `encC`（PUSH）＝確認、`btn`（KO）＝離開
> （`ui/lvgl/board.py` 的 confirm / exit 預設 label）。若實機相反，交換
> config `PIN.list` 裡這兩個 label 的 GPIO 即可，程式不用改。

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
| `motor` / `action` | 本板 UART1 已關閉（GPIO41 改作 encoder A），也沒掛電機，不當動作執行端 |

三個容易踩的順序／歸屬約束（`Core_Manager.py` 內有完整說明）：

1. **`log` 放最後** —— `log_task_ready` 一設，其它 task 的 `print` 就改走環形
   緩衝；先起會把 boot 期的 driver 訊息吸走。
2. **`now` 與 `network` 都要留** —— 兩者都會建 `NowBus` 但互相 reuse（先查
   bus service），不會二次 `espnow.active(True)` 撞 `ESP_ERR_ESPNOW_EXIST`。
   誰先跑完不確定，缺一個在時序不利時就沒有 ESP-NOW。
3. **encoder A/B 只能在 config 的 `ENC` 段**，不要放進 `PIN` 段 —— `ENC` 段給
   `enc_drv` 建 `machine.Encoder` 並註冊 `enc_list`，`HwSampleTask` 採樣進
   `_hw_inputs` 給 LVGL 消費。本板 `ENC = {a:41, b:42}`；`PIN` 另放
   `encC=2`（PUSH）、`btn=1`（KO）。`PIN` 段再加 `encA`/`encB` 會讓 boot Phase 1
   報 GPIO 衝突並 `SystemExit`。

## 「哪些線進解碼鏈」＝ `Router`（本 port 不設 `CircuitDecode`）

`Router` 是唯一的解碼介面宣告處。`{"in": X, "out": []}` ＝ `V_DROP` ＝
明確不執行；`out: ["self"]` ＝ 本地執行；`out: ["now"]` ＝ 從 ESP-NOW 送出去。

| 介面 | 設定 | 理由 |
|---|---|---|
| `self` | `["now"]` | **本機發起（vBus 注入，例如 `schedule`）→ 從 ESP-NOW 送出去**。不含 `self` ＝ 不在本地再 dispatch 一次 |
| `now` | `["self"]` | ESP-NOW 進來的幀本地執行（0x1502 寫 `_display_*`），**不再轉發**（避免回音） |
| `net` / `udp` | `["self"]` / `[]` | master 的 WS / 發現通道；wifi+lan 都 0 時**不存在**，開機時會被標「暫不註冊」，通道上線才自動註冊（內容照 config） |
| ~~`uart0`~~ | — | **已移除**：GPIO41 改作 encoder A → `UART.enable=0`，根本沒有 `uart0` 介面 |

**實測（`Router.enable=1`，只有 `now` 上線時）**：

```
  生效的路由表:
    'self'  out=['now']     auto=False  verdict=V_FORWARD
    'now'   out=['self']    auto=False  verdict=V_EXECUTE

  gate: self → V_FORWARD（now.written=1，訊號有送出）
        now  → V_EXECUTE（沒有再噴回去）
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

### UART1 為什麼關閉（GPIO41 衝突）

新板把遙控器 encoder A 放在 GPIO41，與原 `UART1 RX=41` 衝突；boot Phase 1 的
`gpio_validate()` 會直接 `SystemExit`。新接線沒有 UART，所以本 port：

- `UART.enable: 0`、`list: []` → 沒有 `uart0` 介面，也不佔用任何 UART 腳位。
- `Router.routes` 不寫 `uart0`（介面不存在，寫了也會被當成不存在的通道跳過）。
- `ControlPanelTask._poll_ex_ic()` 的 UART 接收路徑一併失效（它本來就是舊協議
  殘留，見下方待決事項 ①）。

要恢復 UART：先挑未佔用的 TX/RX（**不要用 41**）→ `UART.enable: 1` + `list`
一筆 → Router 加 `{ "in": "uart0", "out": [...] }`。

> **介面名 `uartN` 的 N = `UART.list` 的索引（0-based），不是 `id`。**
> `UART.list[0]`（無論 `id` 是 1 還是 2）→ 邏輯名 `uart0`；`UART.list[1]` → `uart1`。
> `config` 的 `id` 只給 `machine.UART(id)` 用，不出現在介面名裡。
> （舊寫法取 `item["id"]`，已改掉；舊名 `uart1` → 新名 `uart0`。）

### ⚠️ `_autofill()` 會**默默改寫你的 config.json**

以下用 UART 當例子（本板目前沒有 UART，但規則對所有 `list` 型介面都一樣）。
`CircuitBus` 的 UART label 是 `CIRCUIT-UART{idx}`（`idx` = list 索引），所以若
config 寫的名字對不上（例如假設中的 `uart_0`，或舊寫法 `uart1`）：

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
「本地執行」，語意反了。要 disable 就得明寫 `[]`。本板目前只有 `udp` 用 `[]`
（wifi/lan 都關時該介面不存在，route 不會生效）。

### 成本說明

UART 關閉後，本板沒有「Router 判 V_DROP 但實體仍被輪詢」的成本
（`CircuitTask` 只輪詢實際存在的通道）。`udp` 的 `out: []` 若通道上線，
Router 會在開機警告一次 `'out' 是空的 → 這條 route 沒有作用`，那是正常運作。

## 待決事項

1. ~~UART 要「收發」~~ **已關閉（2026-10 接線變更）**：GPIO41 改作 encoder A，
   UART1 沒有可用腳位 → `UART.enable: 0`。舊的 EX-IC 5-byte /
   `ControlPanelTask._poll_ex_ic()` 接收路徑一併失效（它本來就是舊協議殘留）。
   若日後要恢復，照上方「UART1 為什麼關閉」的三步做；**不要**把 RX 直接寫回
   41（會和 `ENC.a` 衝突，Phase 1 `SystemExit`）。
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
- **`UART.enable: 0` 是刻意的**：GPIO41 已給 encoder A。不要把 UART 開回來而不改
  RX 腳位，否則 boot Phase 1 會 `SystemExit`。

## 上傳

```bash
# 1) 先上傳 slave/ 基礎（全量）
# 2) delta 覆蓋：
python3 -B -m mpremote connect <COM> fs cp ports/S3/ESP32-S3-Control_Panel_F/config.json :/config.json
python3 -B -m mpremote connect <COM> fs cp ports/S3/ESP32-S3-Control_Panel_F/Core_Manager.py :/Core_Manager.py
# 3) RESET
```

> 開機後看 `[BOOT] ok / FAIL` 摘要與 `[CoreManager]` 的任務清單，
> 確認 ESP-NOW 有起來（`✅ [NOW-Bus] ESP-NOW active, channel=6`）。
