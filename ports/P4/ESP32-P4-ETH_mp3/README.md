# ports/P4/ESP32-P4-ETH_mp3 — ESP32-P4 乙太網 + MP3 音效板

## 用途
用 I2S + PCM5102 播放音效：dj_task 從 SD 讀檔 + viper 混音 → audio_player 經 I2S DMA
播出（block 模式 = 精準 1×）。本目錄是**完整可上傳的 firmware**（與 `slave/` 同結構：
`boot.py` / `main.py` / `Core0.py` / `Core_Manager.py` + `driver/` + `tasks/` + `lib/` +
`config.json`），並含 **P4 i2s 16-bit 帶號修正**（`tasks/dj_task.py` 混音器改以 `ptr8`
組回帶號 s16，詳見 `doc/03_notes/14_audio_p4_ptr16_bug.md`）。

## 硬體接線（PCM5102，I2S）
| GPIO | 腳位 | 說明 |
|---|---|---|
| 15 | BCK | I2S 資料時鐘 |
| 17 | LCK (WS) | 左右聲道時鐘 |
| 16 | DIN | I2S 資料 |
| 18 | XSMT | 靜音控制（driver 自動拉高解除靜音） |

> SCK / FMT 接 GND（強制內部 PLL），並與板子共地。無聲先查：XSMT 高電位 → SCK 接地 → 共地。
> 註：`config.json` 內 I2S/PCM5102 腳位以板上實際 config 為準（與本表可能不同）。

## 主要檔案
| 檔案 | 內容 |
|---|---|
| `config.json` | 板端 config：SD 4-bit、I2S + PCM5102、`Audio.mode="block"`、ESP-NOW(ch11)、WS2812 停用 |
| `pixel/modes/sfx_play.json` | mode id **3**（純音效）：播 `ME_44100_16_2.wav`（map 留空＝無燈效、只出聲） |
| `schedule.json` | 開機 +1s 經 vBus 發 `MODE_SET 00 03 00 00 FF`（= mode 3） |
| `tasks/dj_task.py` | 混音器 `_mix1`~`_mix4`：P4/RISC-V 下 viper `ptr16` 為無號語意 → 改用 `ptr8` 重建帶號 s16（修 16/8-bit 正負號 bug） |
| `sd/audio/` | MP3 母檔 `E.mpeg` + `playlist.json`；44.1k/16bit/stereo WAV 需自行轉檔放入 SD |

## MODE_SET payload 速查
| id | mode | payload |
|---|---|---|
| 3 | sfx_play（純音效） | `00 03 00 00 FF` |

- 延遲：schedule 的 `ms` 欄位控制「開機後第幾 ms 觸發」；音效本身延遲 = mode json 的
  `delay_ms` + MODE_SET 的 `start_delay_ms`（可疊加）。

## 上傳（裝置已接 COM）
```bash
# 本目錄 = 完整 firmware：把整目錄內容複製到板子根目錄（等同 slave/ 全量上傳流程）。
# 音源：44.1k/16bit/stereo WAV 放 SD 卡 /sd/audio/（轉檔 SOP 見
#   doc/03_notes/14_audio_p4_ptr16_bug.md §5；改檔後 mpremote cp 上傳 + 重開機才會載入新碼）
```

開機 log 預期：`I2S: 1 device(s)` → `PCM5102: 1 device(s)` → `🎧 [Dj] online` →
`🔊 [Aplay] online` → `[Schedule] armed` → +1s `MODE_SET` 觸發播放。
