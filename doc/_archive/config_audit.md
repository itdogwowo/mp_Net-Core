# config 全面稽核與整合（2026-10）

> 🗄️ **歷史紀錄（2026-10-24 標記，不再更新）**：文中三支工具（`config_audit` / `config_dryrun` / `config_prune`）
> 已不在 repo；**§6 的 `-1` / `null` 用法仍是現行規則**，`ports/README.md` 仍引用該節。

> 起因：專案裡的 `config.json` 橫跨多個時期（8 月 rc/old 系列 → 9 月 ports 系列），
> 參數隨韌體演進失效，同一塊板子存在多份不同時期的 config。本文以 **韌體程式碼為
> 唯一真相**逐項驗證，列出失效項、現存錯誤，並給出整合方案。
>
> 最珍貴的資產是**「每個服務的 enable（啟動／關閉）＋ GPIO 腳位」**——那是板子的
> 硬體真相，其餘調校值（亮度、像素數、baudrate、排程）都可以之後再生成。

## 0. 工具（本次新增）

| 工具 | 用途 |
|---|---|
| [`tools/PC/config_audit.py`](../../tools/PC/config_audit.py) | 以程式碼為真相，列出每份 config 的 **[DEAD]** 失效鍵、**[MISSING]** 缺鍵、**[SHAPE]** 型別問題，並輸出 **GPIO 總表** |
| [`tools/PC/config_dryrun.py`](../../tools/PC/config_dryrun.py) | 在 PC 上模擬 `boot.py` Phase 1/2，抓「開機才會炸」的錯誤：**KEYERROR / CONFLICT / DANGLING / SHAPE / MISSING / ADVISORY** |

```bash
python -B tools/PC/config_audit.py --live          # 稽核現役 config
python -B tools/PC/config_audit.py --gpio-table    # 只印 GPIO 總表
python -B tools/PC/config_dryrun.py --live         # 啟動模擬（燒錄前先跑）
```

## 1. 現況盤點

### 1.1 檔案分層

| 層 | 路徑 | 份數 | 狀態 |
|---|---|---|---|
| 基礎框架 | [`slave/config.json`](../../slave/config.json) | 1 | 追蹤中，**同時被當成 S3 基礎板設定用** |
| 專案 delta | `ports/<板子>/config.json` | 13 個目錄 / 16 份檔案 | 追蹤中 |
| 舊時期 | `temp/{old,old2,rc,rc2}/config.json`、`temp/config.json` | 5 | **gitignored**（`.gitignore:230`）純本機暫存 |
| 舊時期 | `ports/P4/ESP32-P4-ETH/temp/config.json` + 140 檔完整舊韌體 | 1 | 同樣被 `temp/` 規則忽略 |
| 測試夾具 | `test/protocol/night_run/config.1401.{test,backup}.json` | 2 | 追蹤中，夜間測試用 |

`ports/` 底下 13 個專案目錄：

```
P4/ESP32-P4-ETH          4 份 config（config.json / config-1 / config-2 / config1）
P4/ESP32-P4-ETH_mp3      1 份（但含 159 檔完整韌體 fork）
P4/General               1 份
S3/ESP32-S3-1_18         1 份 + schedule.json + pixel/
S3/ESP32-S3-Control_Panel      1 份 + Core_Manager.py
S3/ESP32-S3-Control_Panel_V2   1 份 + Core_Manager.py
S3/ESP32-S3-Matrix       1 份
S3/ESP32-S3-N16R8-LCD-V1.0     1 份
S3/ESP32-S3-N16R8-V1.0   1 份
S3/ESP32-S3-Test_Peer    1 份 + Core_Manager.py
S3/ESP32-S3-Touch-AMOLED-1.91  1 份
S3/ESP32-S3-Touch-LCD-2.8      1 份
```

### 1.2 服務啟動／關閉矩陣（enable）

`1`=啟用、`0`=明確關閉、`-`=該 section 不存在。順序依 `boot.py` 的初始化順序。

| 服務 | slave | P4-ETH | P4-mp3 | P4-Gen | S3-1_18 | CtlPanel | CtlPanelV2 | Matrix | N16R8-LCD | N16R8 | TestPeer | AMOLED | Touch2.8 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| SPI | 1 | 1 | 0 | 1 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 1 | 1 |
| PIN | 1 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 1 | 0 | 1 | 1 |
| I2C | 1 | 1 | - | 0 | 0 | 1 | 1 | 0 | 0 | 1 | 0 | 1 | 1 |
| UART | 1 | 0 | 1 | 0 | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| PWM | 0 | 0 | - | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| I2S | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| PCM5102 | 0 | - | 1 | - | - | - | - | - | - | - | - | - | - |
| SDcard | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 0 | 1 | 1 | 0 | 1 | 1 |
| TFT | 1 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 1 | 0 | 1 | 1 |
| I80 | - | - | - | - | - | - | - | - | - | 1 | - | - | - |
| ENC | 1 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| XL9555 | - | - | - | - | - | - | - | - | - | 1 | - | - | - |
| GT1151Q | - | - | - | - | - | - | - | - | - | 1 | - | - | - |
| WS2812 | 0 | 1 | - | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| APA102 | 0 | 1 | - | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| PCA9685 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| uartMotor | 0 | 0 | - | - | 1 | 0 | 0 | - | - | - | 0 | - | - |
| Router | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |

> `PWM` 13 份全部 `enable=0`，且 `boot.py` 的 `_init("pwm", ...)` 已被註解掉 —— 這個服務實質停用中。

## 2. 失效參數（[DEAD]：韌體從不讀取）

驗證方式：把 `slave/` 與 `ports/P4/ESP32-P4-ETH_mp3/`（另一份完整韌體）所有
`.get("x")` / `["x"]` / `.pop("x")` 的字面鍵抽出成集合，再與 config 的每個鍵比對；
未出現者再逐一手動複查（排除 `**cfg` 傳參等間接用法）。

### 2.1 全體通用的失效鍵（13 份 + slave 都有）

| 鍵 | 出現份數 | 證據 |
|---|---|---|
| `System.c_lum` | 14 | 全專案唯一命中是 `ConfigManager.py:798` 的**docstring 舉例**，非實際讀取 |
| `System.refresh_rate_ms` | 14 | 0 次引用（已被 `frame_interval_ms` 取代） |
| `System.buffer_frames` | 14 | 0 次引用 |
| `Buffer.rx_hub_buffers` | 14 | 0 次引用 |
| `Buffer.fb_mode` | 14 | 0 次引用（`"auto"` 這個值也沒有消費端） |

### 2.2 局部失效鍵

| 鍵 | 出現位置 | 證據 |
|---|---|---|
| `Buffer.size` | Control_Panel、Control_Panel_V2 | 唯一命中是 `proto.py:32` 的註解（說明「以前 app.py 用 Buffer.size*2，已收斂」） |
| `Buffer.pixel_stream_slots` | Control_Panel、Control_Panel_V2 | 0 次引用 |
| `SDcard.LDO` | Control_Panel、Control_Panel_V2 | 0 次引用（`{id:4, mv:3300}`） |
| `PWM.list[].dStay` | 5 份（P4-ETH、1_18、Test_Peer…） | `pwm_drv.py` 只讀 `GPIO`，`PWM(Pin(gpio), freq=1000, duty=0)` 寫死；`dStay` 的消費端只有 PixelController / uart_motor |
| `PWM.list[].type` | 同上 | 0 次引用 |
| `PCA9685.list[].GPIO.type` | 6 份（P4-ETH、P4-mp3、General、1_18、N16R8-LCD、Test_Peer） | `pca9685_drv.py` 只讀 `GPIO.i2c` |
| `I2S.list[].GPIO.xsmt` | 只有舊 `slave/config.json`（值 12） | `xsmt` 的兩個讀取點都在 **PCM5102** 區塊（`pcm5102_drv.py:93`、`:120`）；`i2s_drv.gpios()` 只回報 `sck/ws/sd` |

> 後四個是**做 §6 範本時才發現的**：原本的稽核規格把 `PWM.dStay`/`type` 與
> `I2S.GPIO.xsmt` 誤記為有效（我照 driver 名稱與 config 慣例推測，沒實際追
> 消費端）。追下去才確認：`dStay` 只有 pixel 類（WS2812 / APA102 / PCA9685 /
> uartMotor）在用，`pwm_drv` 完全不碰；`xsmt` 只在 PCM5102 區塊讀。
> 已回頭修正工具規格、補進清理清單，範本也不再示範這些沒有消費端的旋鈕。
>
> 教訓：**config 的鍵長得像有效，不代表有人讀**。`_comment`、舊名殘留、
> 兩處重複宣告，都會讓「看起來該有」的鍵一直流傳下去。

### 2.3 舊時期才有的失效鍵（改名／移除的歷史痕跡）

| 舊鍵 | 出現位置 | 現在叫什麼 |
|---|---|---|
| `System.local_fps` | temp/old, old2, rc, rc2 | → `frame_interval_ms` |
| `System.num_leds` | temp/old, old2, rc, rc2 | → `num_pixels` |
| `PCA9685.GPIO.dArc`、`PWM.GPIO.dArc` | temp/old2 | → `dStay`（`motor_drv.py:7` 明寫「對齊舊專案 PWM 的 dArc 概念」） |
| `test_mode`（整個 section） | temp/old2 | 已移除（`enable/test_pattern/test_level/test_segment/test_speed`） |

### 2.4 反向：韌體會讀、但幾乎沒人給的鍵（[MISSING]）

| 鍵 | 讀取點 | 缺少份數 | 影響 |
|---|---|---|---|
| `System.ws_stale_ms` | `sys_actions.py:45` | 13/14（只有 Control_Panel ×2 有） | 吃 default 30000，功能正常但無法調 |
| `Buffer.now_rx_slots` | `now_bus.py:126` | 12/13（只有 P4-mp3 有） | 吃 default 2 |

另外 `System.cID` / `debug_level` / `watchdog` 在部分板子缺席 —— 這三個**是安全的**：
`cID` 未給時 `ConfigManager.ensure_cID()` 會從 MAC 自動生成，`debug_level` 預設 1，
`watchdog` 缺席時 `watchdog.py` 以「測試模式（無狗）」處理。

## 3. 服務啟動／關閉 ＋ GPIO 總表（最珍貴的資產）

只列 **enable=1**（開機真的會被 `boot.py` Phase 1 claim）的腳位。

| 板子 | 已啟用服務 → GPIO |
|---|---|
| `slave` | SDcard → 4,5,6,7,15,16 ｜ PIN → 10,11,12,13,17,42 ｜ ENC → 8,18 ｜ I2C → 38,40 ｜ SPI → 21 ｜ UART → 39,41 |
| `P4/ESP32-P4-ETH` | SDcard → 39,40,41,42,43,44 ｜ SPI → 7,8 ｜ I2C → 20,21 ｜ WS2812 → 15,16,17 |
| `P4/ESP32-P4-ETH_mp3` | SDcard → 39,40,41,42,43,44 ｜ UART → 2,3,4,5,49 ｜ I2S → 30,31,51 ｜ PCM5102 → 52 |
| `P4/General` | SDcard → 39,40,41,42,43,44 ｜ SPI → 8 |
| `S3/ESP32-S3-1_18` | UART → 8,9,10,11,12 |
| `S3/ESP32-S3-Control_Panel` | SDcard → 4,5,6,7,15,16 ｜ PIN → 10,11,12,13,17,42 ｜ ENC → 8,18 ｜ I2C → 38,40 ｜ SPI → 21 ｜ UART → 39,41 |
| `S3/ESP32-S3-Control_Panel_V2` | 同上，但 **PIN 的 btn 由 42 → 9** |
| `S3/ESP32-S3-Matrix` | WS2812 → 14 |
| `S3/ESP32-S3-N16R8-LCD-V1.0` | SDcard → 4,5,6,7,15,16（D0..D3 順序為 5,4,16,15，與其他板相反） |
| `S3/ESP32-S3-N16R8-V1.0` | I80 → 3,4,5,6,7,8,9,10,11,12,13,15,16,17,18,39,45,46 ｜ I2C → 1,2,40,41 ｜ SDcard → 21,47,48 ｜ PIN → 38,42（+ GT1151Q.INT label `touch_int`） |
| `S3/ESP32-S3-Test_Peer` | （無——純協定對端，零硬體依賴；所有服務 enable=0） |
| `S3/ESP32-S3-Touch-AMOLED-1.91` | PIN → 0,1,2,3,4,6,10,11,12,13,14,15,16,17,21,38,41,43,44,45,46 ｜ I2C → 39,40 ｜ SDcard → 8,9,42 ｜ SPI → 47 |
| `S3/ESP32-S3-Touch-LCD-2.8` | PIN → 0,2,4,5,6,7,8,9,12,13,15,18,39,41,42,43,44,46 ｜ I2C → 1,3,10,11 ｜ SDcard → 14,16,17 ｜ SPI → 40 |

### 3.1 「非腳位」欄位（容易誤讀，特別標註）

以下 `GPIO` 子鍵的值是**匯流排 list 的索引**，不是腳位號：

| 子鍵 | 意義 |
|---|---|
| `i2c` | `I2C.list` 的索引（HUSB238 / XL9555 / GT1151Q / PCA9685） |
| `spi` | `SPI.list` 的索引（APA102） |
| `uart` | `UART.list` 的索引（uartMotor / CircuitDecode） |
| `i2s` | `I2S.list` 的索引（PCM5102） |

`XL9555.list[].IO` 是**擴充晶片自己的 P0–P15**（不是 MCU GPIO），
`I80.{data,wr,cs}` 才是 MCU 腳位。

### 3.2 服務的「啟動／關閉」控制腳（電源類）

除了各服務的 `enable` 開關，以下腳位是**主動拉高／拉低去開關外部電路**的，
整合時絕不可遺漏：

| 位置 | 腳位 | 作用 | 消費端 |
|---|---|---|---|
| `System.watchdog.btn_bypass_gpio` | 目前皆 `-1`（不設） | 開機按住 → 不建 WDT（逃生門） | `watchdog.py:130` |
| `UART.list[].GPIO.en` | Test_Peer `en=9` | RS485 DE 方向腳 | `uart_drv.py:115` |
| `UART.list[].en_settle_ms` | 未給（default 1） | DE 切換前後安定時間 | `uart_drv.py:121` |
| `TFT.pins.bl` | 各板不同 | 背光 = 面板電源（**必須先開才能收 init 命令**） | `tft_drv.py:89` |
| `PCM5102.GPIO.xsmt` | P4-mp3 `52` | DAC shutdown 解除 | `pcm5102_drv.py:93` |
| `I2S.GPIO.xsmt` | 各板不同 | 同上（I2S 側宣告） | `i2s_drv.py` |
| `TFT.xl9555.{rst,bl}` | N16R8 `LCD_RST`/`BL_CTR` | 走 XL9555 的 LCD 復位／背光 | `tft_drv.py:157`（**見 §4.1，型別有誤**） |

## 4. 稽核發現的真實錯誤

### 4.1`ports/S3/ESP32-S3-N16R8-V1.0`：顯示路徑開機必失敗（最嚴重）

> **狀態：依決策「先不動，只留報告記錄」。** 這塊板子目前開機時 TFT 會 FAIL
> （沒有畫面）；要修時照下面三個點處理即可。

這塊板子是 ST7796 + I80 並列 + XL9555 擴充 + GT1151Q 觸控，但：

1. **`boot.py` 只呼叫 `init_tft`（SPI 模式）**，而 `init_tft_i80` 在全專案
   **沒有任何呼叫端**（`grep init_tft_i80` 只找到定義處）。
2. `init_tft` 要求 `TFT.pins.cs` 與 `TFT.pins.rst`，且是 **raw subscript**
   （`tft_drv.py:79-80` 的 `pins["cs"]` / `pins["rst"]`）；這份 config 只給了
   `{"dcx": "tft_dcx"}` → **KeyError**，被 boot 的 try/except 接住 →
   `[BOOT] FAIL: tft (...)`，**板子開機沒有畫面**。
3. `TFT.xl9555 = {"rst": "LCD_RST", "bl": "BL_CTR"}` 給的是**標籤字串**，
   但 `tft_drv.py:158-159` 用 `xl.pin[rst_pin]` 當**整數索引**存取
   （`XL9555.pin` 是 `[_Pin(self, i) for i in range(16)]` 的 **list**）
   → 就算接上 `init_tft_i80` 也會 **TypeError**。正確值應是 `IO` 編號
   （`LCD_RST`=1、`BL_CTR`=3）。

`config_dryrun.py` 已能自動抓到這 5 個問題。

### 4.2 全部 13 份 config 都帶著 P4 的 LAN 腳位（未爆彈）— **已處理**

`Network.lan.GPIO = {mdc:31, mdio:52, ref_clk:50}` 被複製到**每一份** config
（連 `slave/config.json` 也有）。但：

* ESP32-S3 的 GPIO 只到 **48**，`mdio=52` 與 `ref_clk=50` **不存在**；
  `31–37` 還是 octal-PSRAM 保留區。
* 這是 ESP32-**P4** 的腳位，只有 `ports/P4/ESP32-P4-ETH`（`lan.enable=1`）用得到。
* `network_drv.py` **沒有 `gpios()`**，所以這些腳位不進 `boot.py` Phase 1 的
  claim/validate —— 打錯不會在開機被擋，要等 `_init_lan()` 才炸。

**處置（依決策）：** 除 `ports/P4/ESP32-P4-ETH/config.json`（唯一啟用、
且是真實 P4 腳位，**保留不動**）外，其餘 11 份的
`Network.lan.GPIO.{mdc,mdio,ref_clk}` 全部改為 **`-1`**（＝未設定）。
`spi/cs/rst/int` 原本就已是 `-1`；`S3/ESP32-S3-Matrix` 本來就沒有 `GPIO` 子樹。

### 4.3 `ports/P4/ESP32-P4-ETH`：同一塊板子 4 份 config — **已處理**

| 檔案 | `num_pixels` | WS2812 | APA102 | APA102 `Q` | SPI baudrate | `watchdog.enable` | `Router` |
|---|---|---|---|---|---|---|---|
| `config.json`（**保留**） | 676 | **1** | 1 | 290 | 8000000 | 0 | 有 |
| ~~`config-1.json`~~ | 1216 | 0 | 1 | 1216 | 5000000 | 1 | 無 |
| ~~`config-2.json`~~ | 1072 | 0 | 1 | 1072 | 5000000 | 1 | 無 |
| ~~`config1.json`~~ | 648 | 0 | 1 | 648 | 8000000 | 1 | 無 |

四份的 **GPIO 腳位完全相同**（`config.json` 多一組已啟用的 WS2812 15/16/17），
只差在像素數／baudrate／APA102 `Q`／watchdog／有無 `Router` —— 都是**調校實驗**。

**處置（依決策）：** 只留 `config.json`，其餘 3 份**刪除**。
下表是刪除前的調校值，若日後要重現實驗，直接照這張表改 `config.json` 即可：

| 想要的效果 | `System.num_pixels` | `APA102.list[0].Q` | `SPI.list[0].baudrate` | `WS2812.enable` |
|---|---|---|---|---|
| 648 像素 | 648 | 648 | 8000000 | 0 |
| 1072 像素 | 1072 | 1072 | 5000000 | 0 |
| 1216 像素 | 1216 | 1216 | 5000000 | 0 |
| 現行（含 WS2812） | 676 | 290 | 8000000 | 1 |

> `WS2812.list[].Q` 四份都是 310，不隨像素數變動。

### 4.4 `ports/P4/ESP32-P4-ETH_mp3`：違反 ports 的 delta 契約

`ports/README.md` 規定 ports 底下只放「相對 slave/ 同名同路徑的 delta」，
但這個目錄是 **159 檔的完整舊韌體 fork**：

* 缺 `slave/lib/sys/` 後來才加的 4 個模組：`peer_registry.py`、`signal_router.py`、
  `soft_reboot.py`、`timer.py` → 它是**較舊的一整套**，不是 delta。
* 混入**裝置產生的檔案**：`secrets.db`、`manifest.json`（FS 索引快取）、
  `schedule_trace.log`。

### 4.5 `handoff/`：8/18 的過期快照

`handoff/schema/{pixel,sys}.json` 與 `slave/schema/` 的現行版本**已經不同**
（`ota.json` 相同）。這是 8 月交接時的快照，留著會與現行 schema 混淆。

## 5. 整合方案

### 5.1 原則

1. **GPIO／enable 一個都不能少、不能改** —— 那是硬體真相。
2. 失效鍵直接刪（可證明零行為變更：韌體從不讀）。
3. 舊時期目錄不進 git（`temp/` 已被 ignore），不需要「整合」，只需**確認無人依賴**後清掉。
4. 同一塊板子只留**一份** config；調校變體不留在 ports（那會隨時間再腐化）。
5. 每份 config 都應能通過 `config_dryrun.py`。

### 5.2 執行狀態

| Phase | 內容 | 狀態 |
|---|---|---|
| **A** | 清理 16 份現役 config 的失效鍵 | ✅ **已完成**（共刪 98 鍵，見下） |
| **B** | 修 `N16R8-V1.0` 顯示路徑 | ⏸ **決策：先不動**，僅本報告記錄（§4.1） |
| **C** | P4 四份 → 一份 | ✅ **已完成**（刪 3 份變體，調校值留存於 §4.3） |
| **D** | 修正 LAN 腳位 | ✅ **已完成**（11 份改 `-1`；P4-ETH 保留真實腳位） |
| **E** | 退役舊時期（temp/、handoff/、裝置產生物） | ⏸ **決策：不動**，僅本報告記錄（§1.1、§4.4、§4.5） |
| **F** | 補齊 `ws_stale_ms` / `now_rx_slots` | ⏸ 未做（目前吃 default，行為正確） |

**Phase A 明細**：`slave/config.json` + 12 份 port config + 3 份 P4 變體，
共刪 **98** 個失效鍵（第一批 86 + §2.2 後三個新發現的 12）。
已逐檔機器驗證「**只刪鍵、零數值變更**」（腳本以允許清單比對每個葉節點的
新增／刪除／改值），且清理前後 `config_dryrun.py` 的結果完全一致（14 項）
→ 可證明零行為變更。

| 檔案 | 刪除鍵數 |
|---|---|
| `slave/config.json`、10 份 port config | 5（§2.1 的通用失效鍵） |
| `S3/ESP32-S3-Control_Panel`、`_V2` | 9（＋`Buffer.size`/`pixel_stream_slots`/`SDcard.LDO`） |
| `P4/ESP32-P4-ETH/config.json`、`config-1`、`config-2`、`config1` | 各 5 |
| `P4/ESP32-P4-ETH_mp3`、`P4/General`、`S3/N16R8-*`、`S3/Touch-*`、`S3/Matrix`、`S3/1_18`、`S3/Test_Peer` | 各 5 |

### 5.3 建議的最終結構

```
slave/config.json          ← 基礎框架範本（不綁板子：20 個服務全 enable=0、腳位全未設定）
ports/<板子>/config.json   ← 該板唯一的設定（enable + GPIO 為核心）
```

每份 config 都應該通過：

```bash
python -B tools/PC/config_dryrun.py --live   # 0 個 KEYERROR / CONFLICT
python -B tools/PC/config_audit.py --live    # 0 個 DEAD（temp/ 除外）
```

目前狀態：**14 份 config、5 個啟動期問題**（全部集中在 `N16R8-V1.0`，§4.1，
依決策暫不處理）、**失效鍵 0**（`temp/` 的暫存副本除外）。

---

## 6. `slave/config.json` 改為基礎範本

依 `ports/README.md` 的契約（「slave/ 永遠保持可單獨使用的基礎，不加項目專用內容」），
`slave/config.json` 已改寫成**不綁任何板子的範本**：

* **20 個服務全部 `enable: 0`**；開機時 `boot.py` 的 Phase 1 不會 claim 任何 GPIO。
* 所有腳位改為「未設定」。
* 補上 §2.4 兩個原本缺的鍵：`System.ws_stale_ms`（30000）、`Buffer.now_rx_slots`（2）。
* `System.num_pixels` 改為 **0** —— `stream_task.py:81` 對 0 的處理是
  `frame_bytes = total_bytes`（由 pixel layout 決定），比寫死 336 更中立。
* `System.dual_core_mode` 由字串 `"0"` 改為整數 **`0`**（`main.py:13` 兩者都吃，
  統一為 int）。
* 移除 §2.2 後四個失效鍵（`PWM.dStay`/`type`、`PCA9685.GPIO.type`、
  `I2S.GPIO.xsmt`），範本不再示範已經沒有消費端的旋鈕。
* 保留有價值的 `_comment`（S3 octal-PSRAM 33-37 不可用、SDcard 腳位對應、
  PCA9685 address 的 `0xFF`/`0x70` 魔法值）。

### 6.1 ⚠️ 「未設定」有兩種寫法，不能混用

這是改寫時最重要的發現：**driver 對「未設定」的判斷方式不一致**，
所以 `-1` 和 `null` 不能互換。寫錯會在 enable 該服務時才爆。

| 判斷方式 | 意義 | 哪些欄位 | 寫錯的後果 |
|---|---|---|---|
| `is not None` | **沒接這條線** → 必須用 `null` | `SPI.GPIO.{sck,mosi,miso}`、`I2C.GPIO.{scl,sda}`、`UART.GPIO.{tx,rx}`、`PCM5102.GPIO.xsmt`、`SDcard.GPIO.{sck,cmd}`、`ENC.GPIO.{a,b}`、`PWM/WS2812/PIN` 的純量 `GPIO`、`I2S.GPIO.{sck,ws,sd}` | 用 `-1` → `Pin(-1)` 或 claim 到 GPIO -1 |
| `>= 0` / `> 0` | **未設定／不啟用** → `-1` 安全 | `UART.GPIO.en`、`SDcard.GPIO.data[]`、`APA102.GPIO.spi`、`PCA9685.GPIO.i2c`、`uartMotor.GPIO.uart`、`HUSB238/XL9555/GT1151Q.GPIO.i2c`、`CircuitDecode.GPIO.uart`、`Network.lan.GPIO.*`、`System.watchdog.btn_bypass_gpio` | 用 `null` 也能跑（多數有 default），但 `-1` 才是慣例 |

證據（節錄）：

```python
# i2c_drv.py:24-25 —— 用 is not None，所以 -1 會被當成腳位
scl=Pin(gpio["scl"]) if gpio.get("scl") is not None else None,
# uart_drv.py:115 —— 用 >= 0，所以 -1 是安全的「不啟用 RS485 DE」
en = gpio.get("en", -1)
if en is not None and int(en) >= 0:
```

範本已依此原則填寫，並在檔頭 `_gpio_convention` 寫明規則，
避免下一個人再製造同樣的坑。

### 6.2 已驗證

* `config_audit.py`：**0 個失效鍵**、無 SHAPE/MISSING 異常。
* `config_dryrun.py`：**0 個啟動期問題**。
* 語意模擬（照範本填真實腳位後啟用 SPI/I2C/UART/PIN/SDcard）：
  claim 到的腳位全部是合法正整數，**無 `-1`/`None` 洩漏**；
  範本原樣（全 enable=0）claim 0 條腳位；
  服務 enable=1 但腳位留 `null` 時 driver 正確跳過，不會 `Pin(-1)`。
