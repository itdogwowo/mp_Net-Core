# ports/ — 專案 delta 層(文件管理架構)

```
mp_Net-Core/
├── slave/                    ← 乾淨基礎框架(不綁任何實際項目)
│     • 通用韌體程式、lib、schedule/vBus 框架、基礎 config.json
│     • config.json 是「範本」: 20 個服務全 enable=0、腳位全未設定
│       (改寫原因見 config.json 檔頭的 `_comment`；`-1` 與 `null` 不可互換，見文末「GPIO 未設定的兩種寫法」)
│     • pixel 只留基礎效果 + 一支馬達正弦波效果範本（uart_motor_sine / motor_sine）
│     • registry.json: auto_play=false（開機不自動播放，純框架）
│
└── ports/                    ← 每個實際項目/板子的「delta 層」
      └── <板子或專案名>/
            ├── config.json          ← 該板 config（覆蓋 slave/config.json）
            └── ... 任何「相對 slave/ 同名同路徑」的檔案 = delta
                 例：
                   pixel/modes/xxx.json   ← 該項目自訂模式
                   schedule.json          ← 該項目開機排程（schedule 任務讀 /schedule.json）
                   pixel/effects/xxx.py   ← 項目專用效果
```

## 規則

1. **slave/ 永遠保持可單獨使用的基礎**: 不加項目專用內容。
   「可單獨使用」= 燒進去能正常開機、不會因為缺設定而報錯；
   **不是**「開機就有功能」。範本全服務關閉，要功能就得套用一個 port delta。
2. **實際項目要跑什麼**,一律以 delta 形式放 `ports/<板子|專案名>/`,檔案的
   相對路徑 = 燒錄到裝置上的路徑(slave 根 = 裝置根)。
3. 燒錄 = 先上傳完整 `slave/`,再以 ports 內的 delta 檔**覆蓋**同名路徑
   (最後覆蓋者勝)。`ports/<板子>/config.json` 會**整份取代** `slave/config.json`。
4. 不同項目各自一個資料夾,互不干擾;基礎升級 = 改 slave,項目 = 只改自己的
   ports delta。
5. **一個板子只留一份 `config.json`**。調校變體(不同像素數 / baudrate 等)不要
   留成 `config-1.json` 這種檔案,那會隨時間腐化;要留就記在該 port 的 `README.md`。

## 改完 config 一定要驗

```bash
python -B tools/PC/config_dryrun.py --live   # 模擬 boot.py: 0 個 KEYERROR / CONFLICT
python -B tools/PC/config_audit.py --live    # 0 個失效鍵
```

## 範例(S3 電機 bench 板)

`ports/S3/ESP32-S3-1_18/`:
- `config.json` — uartMotor(addr 18, uart list[1] = id2 @9600 GPIO12)、
  UART/RS485、Schedule 等該板設定。
- `README.md` — 該板說明與 delta 清單。

## 快速燒錄(以 mpremote 為例)

```bash
# 1) 上傳基礎（裝置已接 COM28）
python -m mpremote connect COM28 fs cp -r slave :/        # 依 mpremote 版本支援 -r；不支援就逐檔上傳
# 2) 套用 delta（覆蓋）
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/config.json :/config.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/xxx.json :/pixel/modes/xxx.json
# 3) RESET
```

> 之後可加一支小工具 `tools/PC/deploy_port.py <COM> <port_name>` 自動化
> 「全量 slave + delta 覆蓋」，避免手動逐檔。

---

## GPIO「未設定」的兩種寫法（不可混用）

`slave/config.json` 是**不綁板子的範本**（20 個服務全 `enable: 0`、腳位全未設定）。
driver 對「未設定」的判斷方式不一致，所以 **`-1` 和 `null` 不能互換**，寫錯會在 enable 該服務時才爆：

| 判斷方式 | 意義 | 哪些欄位 | 寫錯的後果 |
|---|---|---|---|
| `is not None` | **沒接這條線** → 必須用 `null` | `SPI.GPIO.{sck,mosi,miso}`、`I2C.GPIO.{scl,sda}`、`UART.GPIO.{tx,rx}`、`PCM5102.GPIO.xsmt`、`SDcard.GPIO.{sck,cmd}`、`ENC.GPIO.{a,b}`、`PWM`/`WS2812`/`PIN` 的純量 `GPIO`、`I2S.GPIO.{sck,ws,sd}` | 用 `-1` → `Pin(-1)` 或 claim 到 GPIO -1 |
| `>= 0` / `> 0` | **未設定／不啟用** → `-1` 安全 | `UART.GPIO.en`、`SDcard.GPIO.data[]`、`APA102.GPIO.spi`、`PCA9685.GPIO.i2c`、`uartMotor.GPIO.uart`、`HUSB238`/`XL9555`/`GT1151Q.GPIO.i2c`、`CircuitDecode.GPIO.uart`、`Network.lan.GPIO.*`、`System.watchdog.btn_bypass_gpio` | 用 `null` 多數能跑（有 default），但 `-1` 才是慣例 |

證據（節錄）：

```python
# i2c_drv.py —— 用 is not None，所以 -1 會被當成腳位
scl=Pin(gpio["scl"]) if gpio.get("scl") is not None else None,
# uart_drv.py —— 用 >= 0，所以 -1 是安全的「不啟用 RS485 DE」
en = gpio.get("en", -1)
if en is not None and int(en) >= 0:
```

> ⚠️ 上方「改完 config 一定要驗」提到的 `tools/PC/config_dryrun.py` / `config_audit.py`
> 目前**不在 repo**（只存在於 git 歷史）；要驗證請改成人工對照本表 + 開機 log 的 `[BOOT]` 摘要。

