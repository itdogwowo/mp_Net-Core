# 音訊模組上板教學（bring-up 操作手冊）

> **用途**：把 PCM5102 + WAV 播放模組從「裸板」跑到「聽得到聲音 / 能 A/B 兩種播放模式」的
> 逐步操作手冊。所有步驟都在實際板子驗證過（ESP32-S3 v1.29.0-preview Octal-SPIRAM、ESP32-P4）。
> **分類**：使用教學（02_guides）
> **前置**：PC 端已裝 mpremote（`python -m mpremote version`）；板子經 USB 連接
> （`python -B -c "import serial.tools.list_ports as lp; [print(p.device, p.hwid) for p in lp.comports()]"`
> 找 VID `303A` 的 COM 埠；S3 內置 USB Serial/JTAG 是 `303A:4001`）。

---

## 0. 三分鐘流程總覽

```text
1. 確認板子 + 固件          → mpremote run 一個 print（os.uname 看版本）
2. 確認 I2S 可用腳位        → mpremote run test/audio/i2s_irq_probe2.py（自動挑腳）
3. 接線 PCM5102             → 選 3+1 支可用腳，SCK/FMT 接地、共地
4. 出聲回歸（block）        → mpremote run test/audio/pcm5102_tone_test.py（改預設腳位）
5. irq 探針（method 2 前題） → i2s_irq_probe.py / probe2（看 irq 觸發數）
6. 上專案固件跑 dj_task     → 燒錄 + config（I2S/PCM5102/Audio.mode）→ 指令播 WAV
7. A/B 聽感                 → Audio.mode 切 "block"/"irq" 各播一段比較
```

---

## 1. 確認板子與固件

```bash
python -B -m mpremote connect COM27 exec "import os; print(os.uname())"
```

實測參考：

| 板子 | VID:PID | 固件 | I2S irq |
|---|---|---|---|
| ESP32-P4 | `303A:1001` | v1.29.0-preview 系列 | ✅ 觸發（bare） |
| **ESP32-S3（Octal-SPIRAM）** | `303A:4001` | v1.29.0-preview.420 | ✅ 觸發（bare） |

> ⚠️ mpremote 連線會先 Ctrl+C 打斷正在跑的程式：若板子跑著本專案固件且 WDT 開啟，
> 會觸發 `auto_disable_on_interrupt`（存 enable=0 + 軟重啟一次，系統自己的行為）；
> 要恢復狗：REPL 執行 `watchdog_set_enable(True)`。

## 2. 挑 I2S 可用腳位（S3 Octal-SPIRAM 重點！）

**實測：GPIO 33–37 在這顆 S3 上被 Octal PSRAM 佔用，`Pin(33)` 直接報 `ValueError: invalid pin`。**
所以交接文件的 33/34/35 組合**不能用**；33–37、19/20(USB)、43/44(console)、45/46(strapping) 全避開。

自動掃描工具（印可用清單 + 挑一組可建立 I2S 的三角）：

```bash
python -B -m mpremote connect COM27 run test/audio/i2s_irq_probe2.py
```

實測輸出：`valid pins: [1,2,4,…,18,21,38,…,42]`、`I2S ok with pins (1,2,4)`。

**挑腳三原則**：
1. 在可用清單內（工具會驗證）
2. 沒被其他外設佔用（SD/TFT/I2C/encoder…；上專案固件時 boot 的 GPIO 衝突檢查會擋）
3. 先想好日後 config 的一致性（config 的 `I2S.GPIO.sck/ws/sd` 就是這三支）

## 3. 接線

| 選定 GPIO | PCM5102 腳 | 備註 |
|---|---|---|
| 例 9 | BCK | |
| 例 10 | LCK (WS) | |
| 例 11 | DIN | |
| 例 12 | XSMT | **軟體拉高才出聲**（懸空 = 靜音，歷史頭號無聲主因）；也可直駁 3.3V 但就失去軟體 mute |
| — | SCK | **接 GND**（強制內部 PLL 從 BCK 恢復主時鐘） |
| — | FMT | 接 GND（I2S 格式） |
| — | GND | **與板子共地** |

無聲排查順序（交接文件三條命脈）：XSMT 高電位 → SCK 接地 → 共地 → `i2s_irq_probe2` 確認資料有在送。

## 4. 出聲回歸（blocking 播放路徑）

`test/audio/pcm5102_tone_test.py` 預設腳位 9/10/11/12；**腳位無效會自動挑一組可用腳**
並印出 `pins: BCK=9 LCK=10 DIN=11`。改預設就編輯檔頂 `PREF_SCK/PREF_WS/PREF_SD/PREF_XSMT`。

```bash
python -B -m mpremote connect COM27 run test/audio/pcm5102_tone_test.py
```

預期聽到：**嗶嗶(880Hz×2) → 200→2000Hz 上揚掃頻 → 440Hz 長音**（約 3.6s）。

- 聽得到 → I2S + DAC 硬體 OK，blocking write 路徑 OK
- 聽不到但腳位正確 → 照 §3 無聲排查；資料側可用 `i2s_diag2`（交接文件）驗 DMA

## 5. irq 探針（決定能不能用 method 2 非阻塞播放）

```bash
python -B -m mpremote connect COM27 run test/audio/i2s_irq_probe.py    # 固定腳位版（先改成你的腳位）
python -B -m mpremote connect COM27 run test/audio/i2s_irq_probe2.py   # 自動挑腳版
```

判讀：

| 印出 | 意思 | 做法 |
|---|---|---|
| `irq registered (bare)` + `final counter ≥ 1` | irq 會觸發 | `Audio.mode` 可設 `"irq"` |
| `RESULT: irq 未觸發` | 該固件 irq 壞 | 維持 `"block"`，或換新固件再測 |
| `RESULT: 無法註冊 irq` | 連註冊都不行 | 維持 `"block"` |

實測：P4 與 S3 v1.29.0-preview 都只吃 **bare `irq(handler)`**（不吃 keyword 參數）；
driver 的 `set_irq()` 已依序嘗試三種簽名，全失敗自動退回 block。

## 6. 上專案固件跑 dj_task

1. 燒錄含音訊模組的專案（boot 會依 config 起 `i2s_list` → `audio_dac` → dj/audio_player）
2. config.json 三段：

```json
"I2S":      {"enable": 1, "list": [{"GPIO": {"sck": 9, "ws": 10, "sd": 11, "xsmt": 12},
                                    "config": {"mode": "tx", "bits": 16, "format": "stereo",
                                               "rate": 44100, "ibuf": 40000}}]},
"PCM5102":  {"enable": 1, "list": [{"GPIO": {"i2s": 0, "xsmt": 12}}]},
"Audio":    {"mode": "block"}
```

3. 開機 log 預期：`I2S: 1 device(s)` → `PCM5102: 1 device(s)` → `🎧 [Dj] online（合成端）`
   → `🔊 [Aplay] online（播放端, …）`
4. 樣本 WAV 上卡（`test/audio/samples/`，`make_samples.py` 生成）→ 掃描/播放指令流程
   見 `test/audio/samples/README.md`
5. 沒接 SD 也行：把 WAV 放 flash（`mpremote cp x.wav :/sd/audio/` 需 SD；無 SD 則
   先用 `pcm5102_tone_test.py` 驗硬體，dj 全鏈需 SD/檔案系統）

## 7. A/B 聽感（block vs irq）

```text
Audio.mode = "block"  → 重開機 → 播一首（聽：有無卡頓/爆音）
Audio.mode = "irq"    → 重開機 → 播同一首（聽：抖動/底噪差異）
0x1102 STATUS_RSP 看 audio_underruns / audio_irq_fires 數值佐證
```

比較重點：underrun 數、切歌/seek 瞬斷感、多軌混音時的穩定度。

---

## 已知板子差異速查

| 項目 | S3 Octal-SPIRAM（實測） | P4（實測） |
|---|---|---|
| USB PID | 303A:4001 | 303A:1001 |
| 33–37 | **不可用（PSRAM）** | — |
| irq 簽名 | bare | bare |
| irq 觸發 | ✅ | ✅ |

## 相關文件

- `13_audio_wav_module.md` — 音訊模組使用指南（指令/契約/架構）
- `doc/03_notes/13_audio_wav_stream_plan.md` — 設計定案與決策記錄
- `doc/Datasheet/PCM5102_ESP32S3_交接文档.md` — 硬體踩坑與性能實測（irq 結論僅適用舊固件）
- `test/audio/samples/README.md` — 樣本 WAV 與播放測試指令
