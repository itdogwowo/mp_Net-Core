# ports/S3/ESP32-S3-1_18 — S3 電機 bench 板(五推順序伸→停→縮)

## 板子
ESP32-S3 (UID 90DA724962C0 / cID 62C0,COM28):UART 電機(UART-412 推桿)bench。
**五台推桿掛在同一條 9600 UART**（config UART `list[1]` = GPIO tx12/rx8），
firmware `#define ADDR` 分別燒成 **1/2/3/4/5** → 一個 `uartMotor` 條目帶 5 個 address，
driver 建**一個** `UartMotor` 實例、`show_all()` 一次過串發 5 個單台 frame（`FF addr value FE` ×5）。

開機流程(50fps,`frame_interval_ms=20` → 1 幀 = 20ms):
1. **auto**:`motor_home` 歸位一次(五台同值:A收 全速 2s → 停) [registry]
2. **schedule(vBus)**:+8s `MODE_SET id5` → `motor_seq` **五推各自獨立伸→停→縮**(循環 300 幀 = 6s,見下)
3. **+21s `MODE_STOP`** → 全停,保持中性值 2048(0x80,電機死區停,不再動)

## 五推各自獨立伸→停→縮(`motor_seq`,mode id5)

**五台是五個獨立的 generator,不是同一條波平移。** 一台推 = 一個 pixel,每一台配**自己的一支效果**
(自己的 `program` = 自己的延遲 / 伸 / 停 / 縮長度),靠 mode map 的 **`range`** 把群組 `matrix.motors`
拆成 5 段(`0:1`…`4:5`)各配一支:

```json
"map": [
  {"group": "matrix.motors", "effect": "motor_seq1", "write": "w", "range": "0:1"},
  ... 共 5 條,一條一台 ...
]
```

真碼驗證:`PixelTask._parse_mode` 每條 entry 各自 `sub_offsets()` 得到 `[0]`,`[1]`…;
`_make_player` 每條 entry 各建一個 generator;`_tick_player` 每幀對 5 條各 `scatter_offs` 一次
→ 五台同一幀裡帶著**各自的**raw byte 出門(一張 UART 幀同時更新五台)。
「第幾顆」= config address 排序的第幾台(硬體真值),效果裡不重複宣告 address。

預設組合(在 `pixel/effects/effects.json` id10-14,一台一支;改數字即可):

| 台 | 效果 id | 延遲 | 伸(升+降) | 停留 | 縮(降+升) | 尾停 | 總長 |
|---|---|---|---|---|---|---|---|
| 1 | 10 `motor_seq1` | 20 | 20+20 | 30 | 20+20 | 170 | 300 |
| 2 | 11 `motor_seq2` | 40 | 25+25 | 40 | 25+25 | 120 | 300 |
| 3 | 12 `motor_seq3` | 60 | 30+30 | 50 | 30+30 | 70 | 300 |
| 4 | 13 `motor_seq4` | 80 | 15+15 | 20 | 15+15 | 140 | 300 |
| 5 | 14 `motor_seq5` | 100 | 35+35 | 50 | 35+35 | 10 | 300 |

一支效果的 `program` 就是這 7 段(值 = 12-bit,`end_Time` **累加**):

| 段 | 寫法 | 意思 |
|---|---|---|
| 延遲 | `keep l_max 2048` | 還沒輪到我 → 0x80 死區停 |
| 伸(升) | `math_now l_lim 2048 l_max 4080 phi 3071` | 由停加速到全速伸 |
| 伸(降) | 同上但 `phi 1023` | 由全速伸減速回停(到位) |
| 停留 | `keep l_max 2048` | 0x80 死區斷力(推桿停在原地) |
| 縮(降) | `math_now l_max 2048 l_lim 0 phi 1023` | 由停加速到全速收 |
| 縮(升) | 同上但 `phi 3071` | 由全速收減速回停 |
| 尾停 | `keep l_max 2048` | 做完休息,順便把總長拉到 300 |

實測時間軸(50fps;真碼 `_tick_player` → `scatter_offs` → `UartMotor` 逐幀解回 raw):

```
 addr | 起跑 | 伸完 | 開始縮 | 縮完 | 動作長
   1  |  22  |  58  |   90   | 129  |  107 幀 (2.1s)
   2  |  42  |  88  |  130   | 179  |  137 幀 (2.7s)
   3  |  62  | 118  |  170   | 229  |  167 幀 (3.3s)
   4  |  82  | 118  |  150   | 189  |   80 幀 (1.6s)  ← 晚動、最短促
   5  | 103  | 168  |  220   | 289  |  186 幀 (3.7s)
```

改數字的三個動作:
1. **延遲**(晚點動)→ 改第一段 `keep` 的 `end_Time`,**後面所有 `end_Time` 一起加同樣的量**。
2. **伸/縮長度**(動久一點)→ 改對應那兩段 `math_now` 的 `end_Time`(升/降各一半 = 對稱的 bell)。
3. **循環長度** → 改最後一段 `keep` 的 `end_Time`;**五支要改成同一個值**,整組才會整齊重複
   (本檔 300 幀 = 6s;要每台跑不同週期也可以,那就把總長設不一樣)。

段型語義(`lib/sw/PixelMathMethod.py` 真碼):`keep` 恆定 = **`l_max`**;`math_now` 正弦在
`l_lim..l_max` 之間,`phi 3071` = 由谷底上升、`phi 1023` = 由峰值下降,`F=5` = 該段走半個週期
(`F=10` = 一整個週期)。
⚠️ 別用 `starter`(恆 0 = **全速收**,不是「靜止」)。

> 五支都是內建 `Effect`(永不 StopIteration)→ 會一直循環播到 `MODE_STOP`
> (`render.clear_all()` 填中性值 0x80);想只跑一輪就設 mode `maxF`(= 循環長度或其倍數)。
> 另:五台雖然同一幀出門,但同一幀內第 1 台與第 5 台差 16.7ms(見下面「緩衝設計」)。

## delta 檔案清單(相對 slave/ 覆蓋,一次過上傳)
| 檔案 | 內容 |
|---|---|
| `config.json` | uartMotor addr **1-5** @UART list[1](9600 GPIO12)、RS485 id1 115200 |
| `pixel/effects/effects.py` | slave 基礎 + `uart_motor_sine` + bench 行程類別(motor_home / motor_test_cycle)—— **motor_seq 不需要動它** |
| `pixel/effects/effects.json` | effects id:1-6 基礎、7 uart_motor_sine、8 motor_home、9 motor_test_cycle、**10-14 motor_seq1-5(一台一支,純 json 畫波)** |
| `pixel/modes/motor_home.json` | mode id3(開機 auto 歸位,五台同收) |
| `pixel/modes/motor_test_cycle.json` | mode id4(五台一起:伸10s/收12s 一次) |
| `pixel/modes/motor_seq.json` | **mode id5(五條 map entry × range 0:1..4:5,各配一台自己的效果,`play_loop:-1` 常駐)** |
| `pixel/registry.json` | `auto_play:true, list:["motor_home"]` |
| `schedule.json` | 開機 +8s 經 vBus 發 `MODE_SET 00 05 00 00 FF`(id5)、+21s `MODE_STOP` |
| `README.md` | 本檔 |

> `motor_sine`(mode id2,單台正弦循環)在 slave 基礎內,不需放此。
> `matrix.motors` 群組(base `slave/pixel/map/matrix.json`)= 全部 `uartMotor1` pixel
> = `num_devices` = **最大 address = 5 顆**,所以效果 pixel_n 對得上、一台對一顆。

## MODE_SET payload 速查(type=0 本地 id)
| id | mode | payload | 動作 |
|---|---|---|---|
| 2 | motor_sine(循環) | `00 02 00 00 FF` | 五台一起:正弦伸/縮無限循環 |
| 3 | motor_home(歸位一次) | `00 03 00 00 FF` | 五台一起:全速收 2s → 停 |
| 4 | motor_test_cycle(伸縮一次) | `00 04 00 00 FF` | 五台一起:伸10s/收12s |
| 5 | **motor_seq(五推各自獨立)** | `00 05 00 00 FF` | 五台各自延遲/伸/停/縮,同時循環 |
| MODE_STOP | — | `00`… `0x3106 payload 01` | 全停+填中性值 |

## 上傳
```bash
# 1) 先上傳 slave/ 基礎(全量)
# 2) delta 覆蓋(依序):
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/config.json :/config.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/effects/effects.py :/pixel/effects/effects.py
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/effects/effects.json :/pixel/effects/effects.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/motor_home.json :/pixel/modes/motor_home.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/motor_test_cycle.json :/pixel/modes/motor_test_cycle.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/modes/motor_seq.json :/pixel/modes/motor_seq.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/pixel/registry.json :/pixel/registry.json
python -m mpremote connect COM28 fs cp ports/S3/ESP32-S3-1_18/schedule.json :/schedule.json
# 3) RESET（開機五台會自動全收，之後 schedule 由 id5 觸發五推順序）
```

## 緩衝設計(三層佇列;數字全部用真碼實測,不是文件推的)

| 層 | 深度 | 換算 | 行為 |
|---|---|---|---|
| 計算核→播放核 hub(`AtomicStreamHub`) | **3 slots** × 20 B(`num_buffers` 預設 3,不吃 config `buffer_frames`) | 60 ms | 滿了 `get_write_view()` 回 None → 同一幀下輪重試(不丟幀,慢一拍) |
| ESP32 UART TX ring(`uart_drv` `txbuf`) | **16384 B** | **819 幀 = 16.4 s** | 進 1000 B/s、出 960 B/s → **淨增 40 B/s** |
| ATtiny412 單台 frame | 4 B | 4.17 ms/台 | 同一幀內第 1 台 → 第 5 台差 **16.7 ms** |

`UartMotor.show_all()` 實測 = 20 bytes/幀;9600 8N1 = 1 byte 1.042 ms。

- **落後量 = 0.04 × 演出秒數**:13s → 0.52 s;21s → **0.84 s**;60s → 2.4 s;400s → 16 s。
- **TX ring 填滿時間 = 16384 / 40 ≈ 410 s(6.8 分)**;填滿之後 `write()` 阻塞
  (repo 自己量過這個行為:`doc/03_notes/08_night_test_results.md` §14.3「txbuf 偏小 → write 阻塞行為」),
  RenderTask 的節奏會被連結速度綁住。

設計準則(不改代碼就能用的):
1. **單場長度 ≤ 60 s**(落後 2.4 s、TX ring 只用 15%);要連續跑就 ≤ 400 s 並重新規劃。
2. **停機後馬達不會立刻停**:佇列裡還有「落後量」那麼多的舊指令要送完
   → 馬達還會動 ≈ 落後量(21s 場 ≈ 0.84 s)才真的停。
   **斷電 / RESET / 拆線前等 ≥ 落後量 + 0.3 s(21s 場 → 至少 1.2 s)**。
   (實測:任何時刻停下,佇列裡至少含一段正在跑的動作 —— 這條波的全域靜止窗只有 5 幀(0.1s),
   永遠小於佇列深度 32 幀,所以「挑安靜時刻停」救不了,只能靠等待。)
3. **收尾狀態是有保證的**:佇列排空後馬達一定停「最後推出去的那一幀」。
   `MODE_STOP` 會推一幀全中性(實測 TX = `FF 01 80 FE … FF 05 80 FE`);
   mode 設 `maxF = 波長 × N`(275×N)則最後一幀本來就是全靜止
   (實測 maxF=275 → 剛好 275 幀、最後 TX 五台全 0x80)→ 兩種收尾都停在死區,差別只在殘餘動作長短。
4. **起始沒有延遲**:開機 auto 的 `motor_home`(125 幀 = 2.5 s)早就播完、ring 已排空
   → `MODE_SET` 後第一幀立即生效。
5. **想要零落後**:唯一不改代碼的方法是把流量壓到 ≤ 960 B/s —— `System.frame_interval_ms` 改 25
   (40fps → 800 B/s、83%,零積累)。代價:1 幀變 25ms,波形段數與 motor_home 秒數都要重算。
   (改代碼的路:廣播 frame 8 B/幀 = 400 B/s,或 firmware `PERIOD 51` 19200。)

## 注意(UART-412 語義)
- W 通道 raw byte:`0x00` = **A 全速收**、`0x80` = **死區停**(中性值)、`0xFF` = **B 全速伸**;
  效果輸出 12-bit,scatter 走 `write:"w"` 時 `>>4` 進 W 通道(所以停 = 輸出 2048)。
  (注意 `11_developing_effects.md` 寫「輸出 0 會被歸零保護映射成死區」是**舊資訊**:
  `UartMotor.st_load_and_convert` 是原樣收下 0x00 = 全速收,程式碼註解也這樣寫。)
- **匯流排負載(5 台)**:每幀 **20 bytes** @50fps = **1000 B/s**,9600 只有 **960 B/s(104%)**
  → 詳見上面「緩衝設計」(落後量、停機等待、零落後選項)。
- **開機第一幀**:`init_pixel` 的 `show_all()` 送的是全零 big_buffer → 實測 TX =
  `FF 01 00 FE … FF 05 00 FE` → 五台 **0x00 = A 全速收**(無斜坡),直到第一個效果幀進來。
  正好等於「先全部回收」,但若你的機構「收」不是安全方向,要先處理這一行。
- `MODE_STOP` / 暫停 / 熄燈走 `clear_all()`/`stop_motors()` 填中性值 0x80
  (實測 TX = `FF 01 80 FE … FF 05 80 FE`;不是 0,0 會全速暴走)。
- 位址改動(例如燒成 18-22):只改 `config.json` 的 address —— 群組 `matrix.motors` 的顆數
  由 controller(`num_devices = 最大 address`)推導,效果 `pixel_n` 跟著改即可;
  effect 端不需要知道 address(第幾顆 = 第幾台)。
- **mode id 會被 audio mode 蓋掉**:`GlobalMode.mode_pool()` = pixel 池 `pool.update(audio 池)`,
  所以 `/audio/modes/5.json`(id 5)會蓋掉本 port 的 `motor_seq` → `set_mode(5)` 解析成純音效模式
  → PixelTask 判定無 entries → 熄燈停。本 port 沒有 audio 檔(`slave/audio/` 不存在)所以沒事,
  但之後加音效模式時 id 不要用 2/3/4/5。

## PC 驗證(免硬體)
```bash
PYTHONPATH=slave python -B ports/S3/ESP32-S3-1_18/pixel/effects/effects.py   # 效果自檢
```
