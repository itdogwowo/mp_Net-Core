# 音訊模組（WAV 串流播放 / dj_task）使用說明

> **用途**：MP3 模組的完整使用指南——PC 端預轉 WAV，slave 端經 `dj_task` 串流播放：
> 硬體接線、config 設定、檔案格式契約、播放列表（playlist.json）、0x32xx 指令、
> 多軌混音、燈效綁定音效（gmode 貫通）、測試方法。
> **分類**：使用教學（02_guides）
> **最後更新**：2026-09（M1~M5 程式完成）
> **設計定案**：`doc/03_notes/13_audio_wav_stream_plan.md`
> **協議細節**：`doc/01_protocol/02_command_index.md` §12（0x32xx）、`04_pixel_protocol.md`（mode_type=3）

---

## 1. 一分鐘結論

- 音源 = PC 端預轉 **WAV（16-bit PCM / 44.1kHz / 雙聲道）**，slave 不解碼 MP3。
- slave 端**兩個任務分工**（對稱 pixel）：`dj_task`（合成端：播放列表快取、多檔案同時開啟、
  混音、狀態機 → audio_stream hub）+ `audio_player_task`（播放端：hub → I2S 輸出）。
- 播放列表 = `/sd/audio/*.wav` 的目錄索引（`playlist.json`）——開機「沒有就掃、有就 pass」，
  指令可 `RESCAN` / `REMOVE`，與檔案傳輸層（0x20xx）**零耦合**。
- 多軌混音**惰性啟用**：1 軌直通零成本；同時播多段音軌才走 viper 混音 + 軟限幅。
- 燈效綁定音效：mode JSON 加 `audio` 段（只存檔名），`MODE_SET` 經 GlobalMode（gmode）
  同步起播燈效與音軌（同一個 `start_delay_ms`）。

---

## 2. 硬體與 config

### 2.1 接線（PCM5102）

| ESP32-S3 GPIO | PCM5102 腳 | 說明 |
|---|---|---|
| 9 | BCK | I2S 資料時鐘 |
| 10 | LCK (WS) | 左右聲道時鐘 |
| 11 | DIN | I2S 資料 |
| 12 | XSMT | 靜音控制——**driver 自動拉高解除靜音**（懸空=無聲，頭號踩坑） |
| — | SCK / FMT | 接 GND（強制內部 PLL）；與板子 **共地** |

> ⚠️ **腳位以板子為準**（config.json 預設即上述 9/10/11/12；enable=0 惰性）。
> 實測 **ESP32-S3 Octal-SPIRAM**：**GPIO 33–37 被 PSRAM 佔用，`Pin()` 直接 invalid**
> （33/34/35 皆不可用）；19/20 USB、43/44 console、45/46 strapping 也要避開。
> 挑腳原則：可建立 I2S + 未被其他外設佔用（boot 衝突檢查會擋錯）；
> 可用清單與自動挑腳工具見 `14_audio_bringup.md`（上板教學）。

### 2.2 config.json（兩層：匯流排 + 模組，照 PCA9685 慣例）

```json
"I2S": {
  "enable": 1,
  "list": [
    {"GPIO": {"sck": 9, "ws": 10, "sd": 11, "xsmt": 12},
     "config": {"mode": "tx", "bits": 16, "format": "stereo", "rate": 44100, "ibuf": 40000}}
  ]
},
"PCM5102": {
  "enable": 1,
  "list": [
    {"GPIO": {"i2s": 0, "xsmt": 12}}
  ]
},
"Audio": {
  "mode": "block"
}
```

- `I2S` = 匯流排（`driver/i2s_drv.py` → `i2s_list`）；`PCM5102` = 模組
  （`driver/pcm5102_drv.py` → `audio_dac` service：`write()` / `mute(on)` / `set_irq()` /
  `fmt` 契約）。
- `Audio.mode`：`"block"`（預設，播放端每圈阻塞 write = 硬體節拍）／`"irq"`（method 2
  非阻塞：I2S irq 驅動補格，註冊失敗自動退回 block；兩者節拍都由 DMA 決定）。
- `audio_dac.fmt = (rate, bits, channels)` 是**檔案契約的單一事實來源**；改格式就改這裡。
- 未啟用 → `audio_dac` 不存在 → dj_task 開機自行停用（不影響其他系統）。

---

## 3. 檔案格式契約（PC 端預轉）

```bash
ffmpeg -i in.mp3 -ar 44100 -ac 2 -sample_fmt s16 -acodec pcm_s16le "battle_44100_16_2.wav"
```

- **檔名自述 tag**：`<name>_<rate>_<bits>_<ch>.wav`（例 `battle_44100_16_2.wav`）。
  解析含合理性驗證（rate≥8000、bits∈{8,16,24,32}、ch∈{1,2}），不合理 → 當無 tag。
- **tag 是自述、header 是真相**：tag 與契約不符 → 掃描時**不開檔**直接標 `compat=0`
  （預測不兼容、省掃描時間）；播放時再以 header 對 `audio_dac.fmt` 驗證。
- 不兼容檔播放：`AUDIO_SET` 回 `AUDIO_READY_ACK{ok=0}` —— Master 立刻知道要重轉，不靜默。
- 統一目錄 `/sd/audio/*.wav`。44.1kHz/16-bit 是 MP3 內容的實用上限（CD 品質）；
  要 Hi-Res 就改 config 的 I2S 區塊 + 重轉，程式不用動。

---

## 4. 播放列表（playlist.json）

```json
{
  "version": 1,
  "scanned_at": 0,
  "files": [
    {"name": "battle_44100_16_2.wav", "path": "/sd/audio/battle_44100_16_2.wav",
     "size": 1058400, "duration_ms": 12000, "rate": 44100, "bits": 16,
     "channels": 2, "compat": 1}
  ]
}
```

- **開機**：有 `playlist.json` → 載入 + pass（不掃 SD）；沒有 → dj_task 後台**分批掃描**
  （每圈 ≤50ms 預算，不阻塞 boot、不踩 WDT 8s）。
- **變更**只認 dj_task 自己：`0x320C RESCAN`（播放中收到會延後到回 IDLE）與
  `0x320D REMOVE`（`delete_file=0` 只移索引隱藏 / `=1` 連 SD 檔刪）。
- **查詢雙通道**：
  - 命令（異系統相容）：`0x320A AUDIO_LIST_QUERY` → `0x320B AUDIO_LIST_RSP`
    （每筆 = name + duration_ms + compat；8K 截斷時 `total>count`）。
  - 檔案（自家 Master 終極大技）：`0x2005 FILE_QUERY "/sd/audio/playlist.json"` 拿 sha256 →
    變了才 `0x2007 FILE_READ` 分段下載，sha 沒變直接用快取。

---

## 5. 指令總表（0x32xx）

### 5.1 播放控制

| CMD | 名稱 | Payload | 說明 |
|---|---|---|---|
| 0x3201 | AUDIO_SET | `file_name(str)` `play_mode(u8)` `volume(u8)` | 準備單檔（file_name = playlist 的 name；play_mode 0=播完停 1=循環；volume 0–100） |
| 0x3202 | AUDIO_PLAY | `start_ms(u32)` | 起播；>0 = 中途加入 |
| 0x3203 | AUDIO_STOP | — | 停止（XSMT 靜音 + 釋放檔案） |
| 0x3204 | AUDIO_PAUSE | `pause(u8)` | 暫停/恢復（即時靜音） |
| 0x3205 | AUDIO_SEEK | `target_ms(u32)` | 跳轉（對齊幀邊界） |
| 0x3206 | AUDIO_VOLUME | `volume(u8)` | 主音量 0–100（混音時折進每軌增益） |
| 0x3207 | AUDIO_READY_ACK | `ok(u8)` `duration_ms(u32)` | SET 驗證結果（ok=0 = 不存在/不兼容/header 不符） |
| 0x3209 | AUDIO_PROGRAM_SET | `tracks(bytes_rest)` | 多軌節目 JSON（見 §6.1） |

### 5.2 播放列表管理

| CMD | 名稱 | Payload | 說明 |
|---|---|---|---|
| 0x320A | AUDIO_LIST_QUERY | — | 命令通道查列表 |
| 0x320B | AUDIO_LIST_RSP | `total(u8)` `count(u8)` `entries(bytes_rest)` | 截斷時 total>count → 用檔案通道 |
| 0x320C | AUDIO_LIST_RESCAN | — | 重掃重建 playlist.json |
| 0x320D | AUDIO_LIST_REMOVE | `name(str)` `delete_file(u8)` | 索引移除 / 索引+檔刪 |
| 0x320E | AUDIO_LIST_READY | `ok(u8)` `count(u8)` | RESCAN/REMOVE 共用 ACK |

### 5.3 播放流程

```text
AUDIO_SET → READY_ACK{ok,duration_ms} → AUDIO_PLAY → 播放
播放中: SEEK / PAUSE / VOLUME / STOP
狀態: 0x1102 STATUS_RSP 內 providers —— audio_pos_ms / audio_duration_ms /
      audio_active / audio_volume / audio_underruns
```

---

## 6. 多軌混音與燈效綁定

### 6.1 多軌節目（0x3209）

```json
{"tracks": [
   {"file": "beat_44100_16_2.wav",   "loop": true,  "volume": 60, "start_ms": 0},
   {"file": "melody_44100_16_2.wav", "loop": false, "volume": 50, "start_ms": 500}
 ], "limit": 80}
```

- 上限 **4 軌**（超過截斷）；`start_ms` = 相對節目起播的延遲（音軌晚進場）；
  `limit` = 軟限幅門檻（% of full scale，預設 80，防削波）。
- 每軌獨立 loop；全部非 loop 軌播完且緩衝清空 → 節目自然結束。

### 6.2 燈效綁定音效（gmode 貫通）

mode JSON（`pixel/modes/*.json` 加 `audio` 段；純音效 mode 放 `audio/modes/*.json`）：

```json
{
  "id": 769,
  "name": "bgm_battle",
  "map": [],
  "audio": {
    "tracks": [
      {"file": "battle_44100_16_2.wav", "loop": true, "volume": 70, "start_ms": 0},
      {"file": "melody_44100_16_2.wav", "loop": false, "volume": 50, "start_ms": 2000}
    ],
    "limit": 80
  },
  "play_loop": -1, "play_count": 1, "play_interval": 0
}
```

- `id` 用 16-bit 識別碼：`(mode_type << 8) | mode_id`；**mode_type=3 = AUDIO 組（純音效）**。
- 模式型態：純燈效（只有 map）/ 純音效（只有 audio）/ 燈+音（兩者都有）。
- `0x3105 MODE_SET` → gmode 解析 → pixel 與 audio 用**同一個 start_delay_ms** 同步起播；
  純音效模式自動熄燈、純燈效模式自動停音 —— 模式是原子表演單元。
- `0x3101 MODE_LIST_QUERY(mode_type)` 支援 0=全部 / 1=LED / 2=SERVO / 3=AUDIO 過濾。
- UART 面板（ActionTask1）的 `_check_mode_audio` 雙路徑：`audio_dac` 在線 → 走 WAV
  （`_mode_audio_map` 值用**檔名字串**）；否則走舊 DFPlayer（int 曲目編號）。

---

## 7. 架構速覽（兩任務：合成 + 播放，對稱 pixel 的 PixelTask/RenderTask）

```
dj_task（合成端, Core0 主線程）               audio_player_task（播放端, Core1 thread）
  playlist 快取 + 分批掃描                      audio_stream hub ──→ audio_dac.write()
  狀態機(IDLE/READY/PLAYING/PAUSED/SEEKING)     (I2S DMA 阻塞 = 硬體節拍, 放 GIL
  每 voice: file handle + stage(8KB)             → 合成端在播放阻塞期間可繼續混音)
  讀 SD → viper 混音(1軌直通/N軌相加+軟限幅)
       │  commit 混好的 PCM slot
       ▼
  audio_stream hub（AtomicStreamHub 8KB×8 ≈370ms 前讀深度）
  控制旗標(bus.shared): audio_streaming / audio_paused（播放端讀來驅動 XSMT 靜音）
```

- **合成端**（`tasks/dj_task.py`）只碰檔案與 hub；**播放端**（`tasks/audio_player_task.py`）
  是唯一碰 `audio_dac` 的地方（write/mute），播放端每圈只寫一格就回。
- 合成端每圈產一格（hub 滿就讓出）；兩邊都不踩 WDT 8s（分批約束見計劃書 §3.5）。
- 緩衝層沿用既有機制（`AtomicStreamHub`），不重複發明 —— 見
  `Skills/buffer-conventions` + `doc/03_notes/02_buffer_architecture.md`。
- 檔案層（0x20xx）零耦合：上傳不自動進索引，要進列表自己發 RESCAN。

---

## 8. 測試

### 8.1 PC 單元測試（不需硬體）

```bash
python -B -m unittest discover -s test/audio -p "test_*.py"   # 42 項: WAV 解析/tag/
        # 分批掃描/播放流程/REMOVE/RESCAN/混音器/gmode 扇出/命令順序
python -B -m unittest discover -s test/sys -p "test_watchdog.py"  # 18 項 WDT 回歸
```

### 8.2 上板測試

- 出聲回歸：`mpremote run test/audio/pcm5102_tone_test.py`（預設 9/10/11/12；
  **腳位無效會自動挑一組可用腳**並印出）—— 預期嗶嗶 → 掃頻 → A4
- **irq 探針**：`mpremote run test/audio/i2s_irq_probe.py`（固定腳位）或
  `i2s_irq_probe2.py`（自動挑腳）—— 印 `RESULT: irq 觸發 N 次` 代表固件支援
  method 2（可把 `Audio.mode` 設 `"irq"`）；N=0 則維持 `"block"`。
  實測：ESP32-P4 與 **ESP32-S3 v1.29.0-preview** irq 皆正常（bare 簽名）；
  交接文件的「irq 不觸發」只適用那顆 S3 舊固件（v1.26.1）。
- 樣本檔：`python -B test/audio/make_samples.py` 生成 `test/audio/samples/`，
  完整步驟與測試指令見 `test/audio/samples/README.md`

---

## 9. 已知限制與踩坑

| 項目 | 說明 |
|---|---|
| raw-mode SD（alloc.json） | `AUDIO_LIST_REMOVE(delete_file=1)` 的 `os.remove` 對 raw 檔會失敗（已知限制；該模式刪檔走 0x2009 FILE_DELETE） |
| viper 混音路徑 | PC 測試只覆蓋同語意純 Python 版；viper 版待上板實測 |
| 音訊/燈效長時漂移 | 各自真實時鐘，超長同步表演可能有累積漂移（未處理） |
| XSMT | 必須由 driver 拉高；接線懸空 = 完全無聲 |
| WDT | 任何任務不得 >8s 卡住核心；dj 掃描分批、播放每圈一 slot（見計劃書 §3.5） |
| 格式不符 | 掃描標 compat=0、SET 回 ok=0；不即時轉換（規範化留 PC 端） |
| **I2S 寫入不可放 core1** | ⚠️ `audio_player`（I2S write）必須在 **core0（主線程）**，`dj` 合成端放 core1——放反會 DMA 餵資料失步 → **播放超快/拆聲**（與 LCD/SPI 同為 core0-only 週邊）。預設已在 `Core_Manager.py` 設對：`dj(0,1)` / `audio_player(1,0)` |
| **irq 補格粒度 vs SLOT** | `Audio.mode="irq"` 時，handler 由 I2S 內部 DMA 緩衝（`ibuf=40000`≈227ms）驅動，但每次只 pop 一格 **8192B(46ms)**——節拍對不上 → hub 被過快掏空 → underrun → 卡頓/拆聲。**單檔純音效用 `"block"` 最穩**（write 被 DMA 牽著走，消耗=真實速率）；irq 若要上，需讓 SLOT 對齊 ibuf 粒度（待解） |


## 相關文件

- `doc/03_notes/13_audio_wav_stream_plan.md` — 設計定案（里程碑/決策記錄）
- `doc/01_protocol/02_command_index.md` §12 — 0x32xx 指令索引
- `doc/01_protocol/04_pixel_protocol.md` — 0x31xx 合約（mode_type=3 / gmode 起播約定）
- `doc/Datasheet/PCM5102_ESP32S3_交接文档.md` — 硬體實測數據（viper/雙核/混音性能）
- `Skills/mp-netcore` / `Skills/buffer-conventions` — 開發流程與緩衝層規範
