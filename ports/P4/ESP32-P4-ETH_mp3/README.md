# ports/P4/ESP32-P4-ETH_mp3 — ESP32-P4 乙太網 + MP3 音效板

## 用途
用 I2S + PCM5102 播放音效：PC 端先把 MP3 預轉 WAV，slave 端經 dj_task 串流播放。

## 硬體接線（PCM5102，I2S）
| GPIO | 腳位 | 說明 |
|---|---|---|
| 15 | BCK | I2S 資料時鐘 |
| 17 | LCK (WS) | 左右聲道時鐘 |
| 16 | DIN | I2S 資料 |
| 18 | XSMT | 靜音控制（driver 自動拉高解除靜音） |

> SCK / FMT 接 GND（強制內部 PLL），並與板子共地。無聲先查：XSMT 高電位 → SCK 接地 → 共地。

## delta 檔案清單（相對 slave/ 覆蓋，一次過上傳）
| 檔案 | 內容 |
|---|---|
| `config.json` | I2S(sck15/ws17/sd16) + PCM5102(xsmt18) + Audio(block)；WS2812 停用（讓出 15/16/17） |
| `audio/modes/sfx_play.json` | mode id **770**（純音效）播 `WhatsApp_Audio_12-38-55_44100_16_2.wav` |
| `schedule.json` | 開機 +1s 經 vBus 發 `MODE_SET 03 02 00 00 FF`（= mode 770） |
| `README.md` | 本檔 |

## MODE_SET payload 速查
| id | mode | payload |
|---|---|---|
| 770 | sfx_play（純音效） | `03 02 00 00 FF` |

- 延遲：schedule 的 `ms` 欄位控制「開機後第幾 ms 觸發」；音效本身延遲改
  `audio/modes/sfx_play.json` 的 `delay_ms`（或 MODE_SET 的 `start_delay_ms`）。

## 上傳（裝置已接 COM）
```bash
# 1) 先上傳 slave/ 基礎（全量）
# 2) delta 覆蓋（依序）:
python -m mpremote connect COM fs cp ports/P4/ESP32-P4-ETH_mp3/config.json :/config.json
python -m mpremote connect COM fs cp ports/P4/ESP32-P4-ETH_mp3/audio/modes/sfx_play.json :/audio/modes/sfx_play.json
python -m mpremote connect COM fs cp ports/P4/ESP32-P4-ETH_mp3/schedule.json :/schedule.json
# 3) WAV 音源放 SD 卡 /sd/audio/WhatsApp_Audio_12-38-55_44100_16_2.wav
# 4) RESET
```

開機 log 預期：`I2S: 1 device(s)` → `PCM5102: 1 device(s)` → `🎧 [Dj] online` →
`🔊 [Aplay] online` → `[Schedule] armed` → +1s `MODE_SET` 觸發播放。
