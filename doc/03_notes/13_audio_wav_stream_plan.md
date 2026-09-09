# 音訊（WAV）串流播放模組 — 定案計劃

> **用途**：MP3 模組的設計定案——PC 端預轉 WAV，slave 端以「合成/播放兩任務」串流：
> `dj_task`（合成端：playlist + 多檔混音）→ `audio_stream` hub → `audio_player_task`
> （播放端：I2S DMA），並與 GlobalMode（gmode）貫通燈效。
> **分類**：筆記（03_notes）
> **狀態**：定案（設計討論完成，實作進行中）
> **對象**：實作 dj_task / audio 協議 / gmode 綁定的人。實作前先讀 `Skills/mp-netcore` + `Skills/buffer-conventions`。

---

## 0. 一分鐘結論

- 音源 = PC 端預轉好的 **WAV（16-bit PCM / 44.1kHz / stereo）**，slave 不解碼 MP3。
- slave 端**兩個任務分工**（對稱 pixel 的 PixelTask/RenderTask）：`dj_task`（合成端：
  playlist 快取、多檔同時開啟、混音、狀態機 → `audio_stream` hub）+ `audio_player_task`
  （播放端：hub → I2S 輸出，唯一碰 `audio_dac` 的地方）。
- 播放列表 = **SD 上 `/sd/audio/*.wav` 的目錄索引**（`playlist.json`），掃一次快取重複讀；
  「沒有就掃、有就 pass」，指令可 RESCAN / REMOVE，**與檔案傳輸層（0x20xx）零耦合**。
- 燈效綁定音效：mode JSON 加 `audio` 段（只存檔名），gmode 解析後同步扇出給 pixel 與 dj_task。
- 混音（多音軌並播）**惰性啟用**：單軌 = 直通零混音成本；只有同時要播多段音軌才走 viper 混音。

---

## 1. 硬體層

### 1.1 PCM5102 接線（依交接文件，本板腳位以 config 為準）

| ESP32-S3 GPIO | PCM5102 腳 | 說明 |
|---|---|---|
| BCK | **BCK** | I2S 資料時鐘 |
| WS | **LCK** | 左右聲道時鐘 |
| DIN | **DIN** | I2S 資料 |
| XSMT | **XSMT** | 靜音控制，**軟體拉高**才出聲（歷史頭號踩坑）；硬體獨有 → **歸 driver** |
| — | SCK / FMT | 接 GND |

GPIO 定案：`BCK=9`、`LCK(WS)=10`、`DIN=11`、`XSMT=12`（config.json 預設；enable=0 惰性）。
（⚠️ 實測的 **ESP32-S3 Octal-SPIRAM** 板：**GPIO 33–37 被 PSRAM 佔用、`Pin()` 直接報
invalid**（33/34/35 全不可用）；19/20 USB、43/44 console、45/46 strapping 同樣要避。
每塊板最終接線 = 「可建立 I2S + 未被其他外設佔用」，boot 的 GPIO 衝突檢查會擋錯；
可用腳位清單與挑法見 `doc/02_guides/14_audio_bringup.md`。）

### 1.2 config.json 分兩層（照 PCA9685/APA102 模式：匯流排歸匯流排、模組歸模組）

**I2S 區塊 = 匯流排**（只放 I2S 外設自己的腳位與參數，註冊 `i2s_list`）：

```json
"I2S": {
  "enable": 1,
  "list": [
    {
      "GPIO": {"sck": 9, "ws": 33, "sd": 34},
      "config": {"mode": "tx", "bits": 16, "format": "stereo", "rate": 44100, "ibuf": 40000}
    }
  ]
}
```

**PCM5102 區塊 = 硬體模組**（`{"i2s": 0}` 引用 i2s_list 索引 + 自己獨有的 xsmt 腳，
同 `PCA9685 → {"i2c": 0}` / `APA102 → {"spi": 0}` 慣例）：

```json
"PCM5102": {
  "enable": 1,
  "list": [
    {"GPIO": {"i2s": 0, "xsmt": 35}}
  ]
}
```

- `driver/i2s_drv.py`（改）：支援 TX/RX 模式，產物 `i2s_list`（匯流排層，不碰模組腳位）。
- `driver/pcm5102_drv.py`（新）：取 `i2s_list[GPIO.i2s]` → **先拉高 XSMT 解除靜音** →
  包成 DAC 物件，註冊 `audio_dac` service 對外提供 `write()` / `mute(on)` 硬體介面。
  硬體模組獨有的東西全收在這裡，不進資料通道；dj_task 只經 service API 呼叫。
- `boot.py`：Phase 1 加 `("pcm5102", g_pcm5102)`（gpios 回報 xsmt 腳做衝突檢查）；
  Phase 2 `_init("i2s", init_i2s)` 取消註解 + 其後加 `_init("pcm5102", init_pcm5102)`
  （匯流排先於模組，順序同 I2C→PCA9685）。
- 對照交接文件實測：`I2S(0, sck, ws, sd, mode=I2S.TX, bits=16, format=I2S.STEREO, rate=44100, ibuf=40000)`。

---

## 2. 檔案格式契約（PC 端預轉，config 驅動、可修改）

- **契約由 config 驅動**：I2S 區塊的 `rate/bits/format` 是單一事實來源；
  `audio_dac` service 對外回報 `fmt=(rate, bits, channels)`，dj_task 逐檔比對
  WAV header，**不符 → 記警告並跳過該檔**（不做即時轉換，規範化留給 PC 端）。
- 預設 **16-bit PCM / 44.1kHz / 雙聲道**（CD 品質）：
  `ffmpeg -i in.mp3 -ar 44100 -ac 2 -sample_fmt s16 -acodec pcm_s16le out.wav`
- **這不是硬體上限**：PCM5102 可到 384kHz/32-bit、MicroPython I2S 支援 16/24/32-bit。
  但對 MP3 轉出的內容，44.1kHz/16-bit 已是**實用上限**（CD 品質；MP3 有損、母帶
  普遍 44.1kHz），再往上資料率/CPU/記憶體翻倍、聽感無增益。
- 日後要播 Hi-Res（24-bit/96kHz 等）→ 改 I2S 區塊 config + PC 端 ffmpeg 用同一組
  數字重轉即可，但 hub 槽數與 SD 吞吐需重估。
- 自寫 WAV header 解析（MicroPython 無 wav 模組）：掃 RIFF/`fmt`/`data` chunk，
  容忍 LIST/INFO 多餘 chunk；`fmt` 非 PCM 或參數與契約不符 → 記錯誤並跳過該檔。
- 統一目錄 `/sd/audio/*.wav`。

### 2.1 檔名自述契約（預測兼容 + 取參數，不用開檔）

檔名用 `_rate_bits_ch` 尾標自我標記格式參數（三個尾段皆數字才算 tag）：

```
battle_44100_16_2.wav     → 44.1kHz / 16-bit / 雙聲道
bgm_idle_48000_24_2.wav   → 48kHz / 24-bit / 雙聲道（一看即知不兼容）
```

- **解析極廉價**：stem 以 `_` 切開，取尾部數字段 + 合理性驗證
  （`rate ≥ 8000`、`bits ∈ {8,16,24,32}`、`ch ∈ {1,2}`）——驗證不過就當「無 tag」，
  避免 `track_1_2_3_4.wav` 這類檔名被誤判。
- **語意**：tag 是「自述標記」、header 是「真相」——
  1. tag 與契約不符 → 掃描時**預測不兼容**：不開檔、直接標 `compat=0`（省掃描時間），
     列表照收讓 Master 看得到，`duration_ms=0`（未解析）；
  2. tag 兼容 → 照常 parse header 求時長，並 cross-check（tag ≠ header → 以 header
     為準 + 記警告）；
  3. 無 tag（舊檔/他廠）→ 照常 parse header 驗證，兼容性以 header 為準。
- playlist.json 每筆加 `"compat": 0/1`；`AUDIO_LIST_RSP` entry 亦帶 compat（u8）——
  異系統 Master 不用下載檔案就能預判能不能播。
- PC 端轉檔時參數直接寫進檔名（一氣呵成）：
  `ffmpeg -i in.mp3 -ar 44100 -ac 2 -sample_fmt s16 -acodec pcm_s16le "battle_44100_16_2.wav"`

### 2.2 播放不兼容檔的行為（不只靜默跳過）

- `AUDIO_SET` 時就驗證（playlist compat 標記 + header 實測）→ 不符 → 回
  `AUDIO_READY_ACK{ok=0}`，Master 立刻知道要重轉，不會「沒聲音卻查無原因」。
- 播放中發現的罕見狀況（header 與標記不符等）→ 該軌補靜音 + 記警告，不炸。
- 即時轉換（降採樣 / 單→雙聲道）列**未來優化**：只做 viper 整數比降採樣，
  非整數比不做（Python 端太貴，規範化留在 PC 端）。

---

## 3. 架構總覽 — 前中後以緩衝區（key）銜接；兩任務：合成 + 播放

### 3.0 職責分層（硬體獨有歸 driver，銜接全靠緩衝區）

```
前段（合成端 dj_task） ─▶ 中段（audio_stream hub = 唯一銜接 key） ─▶ 後段（播放端）
開檔/讀 SD/viper 混音      AtomicStreamHub 8KB×8                  audio_player_task
（寫 slot）               SPSC：前段寫 slot、後段讀 slot             （讀 slot → audio_dac）
```

- **driver（`driver/i2s_drv.py` 匯流排層 + `driver/pcm5102_drv.py` 模組層）**：
  硬體模組獨有的東西 —— I2S 初始化（匯流排）、XSMT 靜音 / DAC 封裝（模組）。
  對外只有 `audio_dac` service API。
- **dj_task（合成端, Core0）**：資料怎麼流 —— playlist/讀檔/混音、狀態機；
  不碰腳位、不碰 I2S 物件細節。
- **audio_player_task（播放端, Core1）**：唯一碰 `audio_dac` 的地方 ——
  從 hub 取 slot `write()`（I2S DMA = 硬體節拍）、依旗標 mute/unmute。
- **緩衝區（audio_stream hub）**：前中後**唯一**銜接方法 —— 前段 `get_write_view()+commit()`
  寫 slot，後段 `get_read_view()+release_read()` 讀 slot，兩邊不直接碰對方。
  控制旗標（bus.shared）：`audio_streaming` / `audio_paused`（合成端寫、播放端讀）。

```
┌─ dj_task（合成端, Core0 主線程）──────────────┐   ┌─ audio_player_task（播放端, Core1 thread）─┐
│ 狀態機: IDLE/READY/PLAYING/PAUSED/SEEKING      │   │ audio_stream hub → audio_dac.write()      │
│ + 掃描 phase（playlist 無→掃、有→pass）          │   │ (I2S.write 阻塞 ~46ms = 硬體節拍, 放 GIL   │
│ playlist 快取 → 檔名 O(1) 解析                   │   │  → 合成端在阻塞期間可繼續混音 = 真重疊)     │
│ 每 voice: file handle + stage(8KB)             │   │ 旗標驅動 XSMT 靜音; hub 空 → 補靜音+underrun│
│ 讀 SD → viper 混音 → commit audio_stream hub   │   └──────────────────────────────────────────┘
└──────────────────────────────────────────────┘
```

### 3.1 前讀後渲染的原理（兩任務並行 + 硬體 DMA）

- 「後渲染」由**播放端 + I2S 硬體 DMA + ibuf（~40KB ≈ 230ms）** 負責：播放端
  `write()` 把一格交給 DMA 後阻塞等空位；**阻塞放 GIL** → 合成端（Core0 主執行緒）
  在播放阻塞期間可繼續讀檔/混音 —— 這就是交接文件 `i2s_dualcore.py` 實證的
  「write 阻塞期間生產線程在跑」的真重疊。
- 合成端每圈產一格（hub 滿就讓出）；hub 8 槽 ≈ 370ms + ibuf 230ms ≈ **600ms 吸收
  餘量**，蓋過 SD 偶發 stall。
- 與 pixel 的 PixelTask（合成）→ RenderTask（播放）同一套模式與親和性安排。
- 緩衝層仍用既有機制：`AtomicStreamHub`（view 模式）+ `alloc_dma`。

### 3.2 記憶體估算

| 項目 | 大小 |
|---|---|
| audio_stream hub（8KB × 8 槽，bytearray） | 64KB（共享一個） |
| 每 voice stage（8KB × ≤4 軌） | 32KB 內 |
| I2S ibuf（內部 DMA） | ~40KB（handoff 實測值） |
| 資料率 | 44.1kHz × 2ch × 2B = **176.4 KB/s**（SD 12.8MB/s 綽綽有餘） |

### 3.3 混音（惰性啟用，在合成端）

- **1 軌 = 直通**：讀 stage → （viper 增益）→ 混進 hub slot，零混音計算。
- **N 軌並播**：合成端把各 voice stage 以 viper 定點增益相加 + 軟限幅
  （`limit` 門檻，防削波，交接文件 `i2s_clip_test.py` 實證手法）→ 混進一個 slot。
- 實證預算（交接文件 `i2s_bench.py`）：viper 16 軌混音 1s ≈ 281ms CPU，40 軌內實時。
- 某軌資料沒跟上 → 該軌補靜音繼續混，不爆音；播放端 hub 空才記 underrun。
- 波形合成（wavetable）**不在本計劃範圍**；「合成」僅指多段預錄音軌的混音疊加。

### 3.4 核心放置

- taskmanager 模式：`dj`（合成端）affinity `(1, 0)` Core0 + `audio_player`（播放端）
  affinity `(0, 1)` Core1（同 pixel/render 的安排）。
- worker_engine 模式：dj_task 加入 core0 主迴圈任務清單；audio_player_task 開獨立
  `_thread`（ESP32 上落在 Core 1）。

### 3.5 WDT 分批約束（任何任務不得 >8s 卡住核心）

- **決定（已實作）：lazy-arm —— 全部 task 的 on_start 運行完才建狗**。
  看門狗只管「運行中途」的問題；第一輪（各 task on_start 的 boot 級聯）跑不完不歸 WDT 管。
  - `task_manager.py`：`runner_loop(0)` 在 `_boot_done` 首次為 True（= 全部 on_start 完成）
    那一圈才呼叫 `init_watchdog()`（service 已存在則跳過），之後每圈餵狗 + `poll_rearm()`。
  - `Core_Manager.py`：launcher 不建狗（交由 runner 於 boot 完成後建）。
  - `Core0.py`（worker_engine）：全部 task `on_start` 完成後建狗 + 每圈餵狗 +
    `poll_rearm()`（該模式原本沒有 WDT，一併補上）。
  - `btn_bypass_gpio`：**-1 = 不設定**（不 claim GPIO、不做 bypass 檢查）；None/<=0 同義。
- dj_task 遵守（狗上線後任何任務不得 >8s 卡住核心）：
  1. 掃描 phase **分批**：每圈最多掃 N 檔或時間預算 ≤50ms 就回（同 FsScanTask 一格一格
     的模式），一圈只做「掃幾個檔 + 解析 + 更新快取」就返回，絕不在 loop 內一口氣掃完整卡。
  2. 播放 loop 每圈只處理一個 slot（8KB）就回：`I2S.write()` / SD `readblocks` 是 C 層
     阻塞、會放 GIL，阻塞期間 core0 照常餵狗；viper 混音以 slot 為單位（8KB ≈ 數 ms 級），
     遠低於 8s。
  3. 掃描中持續讓出（`sleep_ms(0)` / 交還控制權）。

### 3.6 播放餵法：block（預設）／irq 非阻塞（method 2）

節拍永遠由硬體決定（BCK 位元時鐘 + DMA），兩種模式只差 **Python 何時知道要補下一格**：

| 模式 | config `Audio.mode` | 下一次 write 的觸發 |
|---|---|---|
| **block**（預設） | `"block"` | `write()` 阻塞至 DMA 有空位才回 → 回傳後補下一格（被消耗牽著走） |
| **irq**（method 2） | `"irq"` | DMA 吃完一格緩衝 → 硬體中斷 → `irq` 回調 =「緩衝空了來補下一格」→ handler 從 hub pop 下一格（非阻塞 write 立即回實際寫入位元組，ring 滿 → partial 續寫） |

- `driver/pcm5102_drv.py` `Pcm5102Dac.set_irq(handler)`：依序嘗試 bare → positional →
  keyword 簽名；全失敗回 None → 播放端自動退回 block。
- `tasks/audio_player_task.py`：`on_start` 依 `Audio.mode` 選擇；irq 模式 handler
  `_on_feed` 補格（partial 用 `_pend=(view, off)` 續寫，槽保持 READING 至寫完才釋放），
  loop 負責起播首填與 mute/旗標管理（pending 未完不插隊）。
- 實測（`test/audio/i2s_irq_probe.py` / `i2s_irq_probe2.py` 自動挑腳版）：
  **ESP32-P4** 與 **ESP32-S3 v1.29.0-preview**（Octal-SPIRAM）兩顆的固件 irq 皆正常觸發
  （counter=2），且都只接受 **bare `irq(handler)`** 簽名；交接文件的「irq 不觸發」
  只適用舊 S3 固件（v1.26.1）。
- 意義：irq 模式下 Python 完全不阻塞，合成端/指令線路更自由；block 已因放 GIL 而
  對系統其餘部分非阻塞 —— 兩者皆可，聽感/抖動差別待上板 A/B。

---

## 4. playlist.json — WAV 目錄索引快取（指令域獨有，零耦合）

### 4.1 語意

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

- **建立**：掃 `/sd/audio/*.wav`，順手解析 WAV header 記後設資料（掃一次全拿到）。
- **開機**：dj_task `on_start` 檢查 —— `playlist.json` 存在 → 載入 RAM + **pass（不掃 SD）**；
  不存在 → 進入掃描 phase（分塊執行，不阻塞 boot 與指令線路，照 `FsScanTask` 模式）。
- **變更**：只認 dj_task 自己（RESCAN / REMOVE）。**0x20xx 檔案傳輸保持自己的行為，
  不特登處理 playlist**；上傳新檔後要不要進列表 = Master 自己發 RESCAN。
- 播放指令（`AUDIO_SET`/mode JSON `audio.tracks[].file`）只給**檔名**，dj_task 經快取
  解析路徑（O(1)）；解析不到 → 記警告並跳過該軌。

### 4.2 查詢雙通道（並存，供異系統相容）

- **命令通道**（異系統用）：`AUDIO_LIST_QUERY/RSP`。
- **檔案通道**（自家 Master 用，終極大技）：既有 `0x2005 FILE_QUERY`（拿 sha256+size）
  → sha 變了才 `0x2007 FILE_READ` 分段下載 `playlist.json`，PC 端直接解析。
  `file_actions` 對未進 manifest 的檔案已有 realtime 路徑（`os.stat` + 直接 open），無需改動。

---

## 5. 指令（0x32xx 音訊域）

### 5.1 播放控制

| CMD | 名稱 | 方向 | Payload | 說明 |
|---|---|---|---|---|
| 0x3201 | AUDIO_SET | M→S | `file_name(str)` `play_mode(u8: 0=播完停 1=循環)` `volume(u8 0–100)` | 準備單檔（檔名 = playlist 的 name） |
| 0x3202 | AUDIO_PLAY | M→S | `start_ms(u32)` | 起播；>0 = 中途加入 |
| 0x3203 | AUDIO_STOP | M→S | (空) | 停止 |
| 0x3204 | AUDIO_PAUSE | M→S | `pause(u8)` | 暫停/恢復 |
| 0x3205 | AUDIO_SEEK | M→S | `target_ms(u32)` | 跳轉 |
| 0x3206 | AUDIO_VOLUME | M→S | `volume(u8)` | 主音量 |
| 0x3207 | AUDIO_READY_ACK | S→M | `ok(u8)` `duration_ms(u32)` | SET 驗證結果（`ok=0` = 檔不存在 / 不兼容 / header 不符），Master 據此重轉 |
| 0x3209 | AUDIO_PROGRAM_SET | M→S | `tracks(bytes_rest)` | 獨立多軌節目（JSON，與 mode JSON `audio` 段同構） |

### 5.2 播放列表管理

| CMD | 名稱 | 方向 | Payload | 說明 |
|---|---|---|---|---|
| 0x320A | AUDIO_LIST_QUERY | M→S | (空) | 命令通道查列表 |
| 0x320B | AUDIO_LIST_RSP | S→M | `total(u8)` `count(u8)` `entries(bytes_rest)` | 每筆 = `name(str_u16len)` + `duration_ms(u32)` + `compat(u8)`；`total>count` = 8K 截斷 → 用檔案通道拉全量 |
| 0x320C | AUDIO_LIST_RESCAN | M→S | (空) | 重掃 SD 重建 playlist.json |
| 0x320D | AUDIO_LIST_REMOVE | M→S | `name(str)` `delete_file(u8)` | `0`=只移索引（隱藏）；`1`=索引+SD 檔案一起刪 |
| 0x320E | AUDIO_LIST_READY | S→M | `ok(u8)` `count(u8)` | RESCAN/REMOVE 共用 ACK |

- 流程對齊 stream 模組：`SET → READY_ACK → PLAY`。
- 播放進度/狀態走 provider（`audio_pos_ms` / `audio_active` / `audio_voices`）併入既有 0x1102 status push。

---

## 6. GlobalMode 貫通層（gmode）

### 6.1 目標

單一事實來源 `bus.shared["gmode"]`：所有模式入口（`0x3105 MODE_SET`、`0x3106 MODE_STOP`、
UART 面板切換）先收斂到 gmode，解析後扇出給各模組；pixel 與音訊用同一個
`start_delay_ms` 同步起播。

```
0x3105 MODE_SET ──▶ ┌ GlobalMode ("gmode") ─┐ ──pixel 段──▶ PixelTask（單模式執行器）
0x3106 MODE_STOP ─▶ │ 狀態機 + 模式解析         │
0x32xx AUDIO_* ───▶ │ {mode_id, state, pixel,  │ ──audio 段──▶ dj_task（音訊引擎）
UART 面板模式 ────▶ │  audio, started_at}      │
                    └─────────────────────────┘
```

### 6.2 mode JSON 加 `audio` 段（三型態）

| 型態 | mode JSON | 扇出 |
|---|---|---|
| 純燈效 | 只有 `map` | 只扇出 pixel |
| 純音效 | `map: []` + `audio` | 只扇出 dj_task |
| 燈效+音效 | `map` + `audio` | 兩邊同步扇出 |

```json
{
  "id": 769,
  "name": "bgm_battle",
  "map": [],
  "audio": {
    "tracks": [
      {"file": "battle.wav", "loop": true, "volume": 70, "start_ms": 0},
      {"file": "melody.wav", "loop": false, "volume": 50, "start_ms": 2000}
    ],
    "limit": 80
  },
  "play_loop": -1, "play_count": 1, "play_interval": 0
}
```

- `id` 用 **mode_type=3（AUDIO 組）**：`(3 << 8) | 1 = 0x0301`，與 LED(1)/SERVO(2) 同一 16-bit
  識別碼空間；`MODE_LIST_QUERY(mode_type=3)` 可過濾純音效。合約文件 `04_pixel_protocol.md` 同步更新。
- mode 目錄兩池掃描：`pixel/modes/`（有 map）+ `audio/modes/`（純音效），gmode 合併成單一 mode pool。
- `audio.tracks[].file` **只存檔名**，dj_task 經 playlist 快取解析 —— 兩邊只靠檔名鬆耦合。
- 排程語義（play_loop/play_count/play_interval）留在 mode JSON 內；節目單行走（依序啟用多個
  mode）屬 gmode 後續階段，PixelTask 內部 show 邏輯過渡期保留。

---

## 7. 里程碑

- **M1** ✅ I2S TX 硬體回歸：config/driver 改造 + 出聲測試腳本 `test/audio/pcm5102_tone_test.py`
  （WDT lazy-arm 決策 B 一併落地）。⏳ 待上板接線驗證（GPIO 9/33/34/35）。
- **M2** ✅（程式完成）dj_task 單軌骨架：playlist 載入/掃描 phase + WAV header 解析 +
  讀→hub→I2S 直通 + 0x32xx 播放控制（`slave/tasks/dj_task.py`、`schema/audio.json`、
  `action/audio_actions.py`；PC 單元測試 19 項過）。⏳ 待上板聯調。
- **M3** ✅（程式完成）playlist 管理指令：LIST_QUERY/RESCAN/REMOVE/LIST_READY +
  `audio_playlist` service 快取（`action/audio_actions.py`、dj_task 消費路徑；
  PC 單元測試 24 項過）。⏳ 檔案通道下載（0x2005/0x2007 讀 playlist.json）待上板聯調。
- **M4** ✅（程式完成）多音軌混音：每 voice 一個 hub（上限 4 軌）、viper 定點增益 +
  軟限幅（`_mix1~4`，PC 純 Python 版同語意）、`start_ms` 依序進場、0x3209
  AUDIO_PROGRAM_SET（tracks JSON + limit）、主音量折進每軌增益（PC 單元測試 32 項過）。
  ⏳ viper 路徑待上板實測（PC 測試只覆蓋純 Python 版同語意）。
- **M5** ✅（程式完成）gmode 貫通：`lib/sys/global_mode.py`（模式池合併 pixel_maps +
  `/audio/modes/`、set_mode/stop_mode 扇出、audio 每軌 start_ms 平移 = 與燈效同
  start_delay_ms 同步起播、純燈/純音自動停另一邊）+ MODE_SET/STOP 經 gmode 路由 +
  mode JSON `audio` 段由 PixelTask 原樣攜帶 + dj 命令消費順序修正（program 先於 play）
  + `_check_mode_audio`（DFPlayer）雙路徑遷移 + 範例 `slave/audio/modes/bgm_battle.json`
  （PC 單元測試 42 項過）。⏳ 待上板聯調（含 viper 路徑實測）。
- **M6** ✅（程式完成）兩任務拆分（合成/播放對稱 pixel）：dj_task 改為**合成端**
  （Core0，每 voice file handle + stage → viper 混音 → commit 共享 `audio_stream` hub，
  不再碰 audio_dac）+ 新建 `audio_player_task`（播放端，Core1：hub → `audio_dac.write()`，
  旗標 `audio_streaming`/`audio_paused` 驅動 XSMT 靜音）；taskmanager 註冊
  dj(1,0)+audio_player(0,1)、worker_engine dj 入主迴圈 + 播放端 thread
  （PC 單元測試 46 項過）。⏳ 待上板聯調（真重疊 + viper 路徑實測）。
- **M7** ✅（程式完成）播放端 method 2（irq 非阻塞）：`Pcm5102Dac.set_irq()`（bare/pos/kw
  簽名依序嘗試）+ `audio_player_task` `Audio.mode`="irq" 路徑（handler 補格、partial
  `_pend` 續寫、loop 首填退路、失敗自動退回 block）；`test/audio/i2s_irq_probe.py` 於
  **ESP32-P4 與 ESP32-S3 v1.29.0-preview 兩顆實測 irq 皆觸發正常**（counter=2，與
  交接文件 S3 舊固件結論不同；S3 Octal-SPIRAM 33–37 不可用，探針自動挑腳驗證）
  （PC 單元測試 50 項過）。⏳ DAC 實際接線後 A/B 聽感聯調。

## 8. 開放事項

- [x] **GPIO 定案**（BCK=9 / LCK=10 / DIN=11 / XSMT=12，config 預設；S3-OCT 33–37 不可用）
- [x] 單軌音量增益（viper）—— 已隨混音層實作（增益 + 軟限幅）
- [ ] 音訊與燈效長時播放的漂移（各自真實時鐘）處理策略

## 相關文件

- `doc/Datasheet/PCM5102_ESP32S3_交接文档.md` — 硬體接線 + 實測腳本（viper/雙核/混音性能數據）
- `doc/03_notes/02_buffer_architecture.md` + `Skills/buffer-conventions` — 緩衝層規範
- `Skills/mp-netcore` — slave 新增功能模組流程（schema/action/task/config）
- `doc/01_protocol/02_command_index.md` — 指令索引（0x32xx 為新域）
- `doc/01_protocol/04_pixel_protocol.md` — 0x31xx 合約（mode_type=3 追加時同步更新）
- `slave/tasks/stream_task.py` / `slave/tasks/render.py` — 生產/消費狀態機範本
- `slave/tasks/fs_scan_task.py` — 後台分塊掃描範本（dj_task 掃描 phase 照此模式）
