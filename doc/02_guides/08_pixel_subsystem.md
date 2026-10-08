# pixel 子系統 — 使用指南（效果 / 群組排列 / 模式配對 / 播放清單）

> **用途**：pixel 子系統架構與使用。子系統把「效果 / 群組排列 / 模式配對 / 播放清單」拆成四層，各自定義，由 `PixelTask`（`slave/tasks/pixel_task.py`）在開機時依序初始化並執行大隊列自動播放。
> **分類**：使用教學（02_guides）
> **最後更新**：2026-08-24
> **位置**：`slave/pixel/`（`effects/`、`map/`、`modes/`、`registry.json`）
> **測試結果與未來方向**：`03_notes/07_pixel_test_results.md`

---

## 1. 四層資料

| 層 | 內容 | 檔案 | bus key |
|---|---|---|---|
| 效果 | 波形生成器（怎麼動） | `pixel/effects/`（effects.json + effects.py） | `pixel_gens` |
| mapping | 群組排列（怎麼排） | `pixel/map/*.json`（每套一個檔，自帶 id/name） | `pixel_layout` |
| modes | 效果 × 群組配對 + 播放參數 | `pixel/modes/*.json` | `pixel_maps` |
| 播放清單 | 播什麼、開不開自動播放 | `pixel/registry.json` | `pixel_show` |

---

## 2. 資料模型

### 2.1 effects/ — 效果

`pixel/effects/effects.json`（JSON 形式，效果完整定義）+ `pixel/effects/effects.py`（PY 形式，只放畫波寫不出來的補充類別）。**json 是唯一真源：id / name / params（含 program 畫波）都在 json 手寫**；畫波效果（breathing/eyes/wave）不需要 py 類別，由內建 `Effect` 播放，只有畫波寫不出來的效果才寫 py 類別靠 name 配對。框架（`Effect` 基類 / 登記表 / 波表快取 / `check_conflicts()`）在 `slave/lib/sw/effect_core.py`。

- 數學核心在 `slave/lib/sw/PixelMathMethod.py`：**`@micropython.viper` 整數多項式逼近**（拋物線基底 + `922*(y²-y)>>12` 修正），**無查表、無浮點、值域固定 12-bit 0-4095**。
- 空間分布：`frame(t)` 把時間波攤到 pixel_n 顆 → `pattern_value_at(program, 相位)`，相位 = `(t // speed) * step + i * spacing + offset`。
- 吐 `array('H')`（0-4095），供 scatter 的 viper 用 ptr16 直接讀。

波形段 `type`：`keep` / `math_now` / `square_wave_now` / `pulse_wave` / `pulse` / `starter`。

| JSON 欄位 | 說明 |
|---|---|
| `id` | 效果識別碼（手寫，全 json 唯一） |
| `name` | 效果名稱（手寫；畫波自由命名，補充類別對應 py 類別名） |
| `program` | 波形段序列（畫波效果必填；補充類別可省） |
| `pixel_n` | 輸出位數 |
| `step` | 時間步進（舊 step） |
| `spacing` | pixel 間距（空間分布） |
| `offset` | 空間偏移 |
| `speed` | 倍速 |
| `reverse` | 反向 |

#### 寫效果（最高優化）

- **路 A 畫波類（首選，純 json）**：只在 effects.json 加一段（id/name + program + 空間分布），框架用內建 `Effect` 播放。範例：`wave` / `eyes` / `breathing`。
- **路 B 自訂/狀態機類（畫波寫不出來）**：`class xxx(Effect)` + override `frame(t)` + `register(xxx)`，json 補 id/params。保持整數、無浮點；輸出 `array('H')`、長度 `pixel_n`、值域 0-4095。

#### 色彩接口（bulk，暫時包裝）

`slave/lib/sw/PixelMathMethod.py` 提供 HSV↔RGB（全整數、無浮點、viper bulk 批次，一次處理整條 buffer）：
- 8-bit（0-255）：`hsv_to_rgb8_buf` / `rgb_to_hsv8_buf`（RGB 為 bytearray 3B/px）
- 12-bit（0-4095）：`hsv_to_rgb12_buf` / `rgb_to_hsv12_buf`（RGB 為 array('H') 3 值/px）
- 單值便利函式：`hsv_to_rgb8` / `rgb_to_hsv8` / `hsv_to_rgb12` / `rgb_to_hsv12`

已修掉舊專案的 bug（RGB 順序、飽和度、色相 offset）。**本輪只提供接口，未接 scatter/effect/controller**——未來彩色 effect 再接。

### 2.2 map/ — mapping（群組排列）

每套 mapping 一個檔，自帶 id/name；**不寫硬體 order/counts**（硬體真值一律從播放器 `PixelStreamer.controllers` 推導）。

```json
{
  "id": 1,
  "name": "gundam",
  "groups": [
    { "id": 1, "name": "gundam_body", "sel": [
        {"type": "pwm",    "sel": "10:15"},
        {"type": "ws2812", "sel": "40:200"},
        {"type": "ws2812", "sel": ":10"},
        {"type": "pwm",    "sel": "15:10:-1"}
    ]},
    { "id": 2, "name": "motors", "sel": [{"type": "uartMotor1", "sel": ":"}] }
  ]
}
```

- `sel` 是「有序的段列表」，段依書寫次序拼接 = 像素次序，可交叉型別、反序、重疊。
- 選擇器：`7`（單顆）、`"0:14"`（範圍，end 不含）、`":"`（全選）、`"15:10:-1"`（反序）。
- **群組 id/name 在同 mapping 內必須唯一**，重複 → 該 mapping 載入失敗（warn + 跳過）。
- 引用硬體不存在的型別（如 pwm / uartMotor1 未接播放器）→ 該段為空（載入時 warn），不報錯。

**統一 key（硬體型別 → registry 型別名）**，在 `pixel_task.py` 的 `TYPE_MAP`：

| 播放器 `pixel_type` | registry key |
|---|---|
| `WS2812` | `ws2812` |
| `APA102` | `apa102` |
| `i2c_pixel` | `pca9685` |

### 2.3 modes/ — 模式（效果 × 群組配對 + 播放參數）

```json
{
  "id": 1,
  "name": "demo1",
  "index": 1,            // 大隊列排序備援：先比 index 再比 id，越少越前（list 順序為主）
  "play_loop": -1,       // 總共出現幾次循環：0=不播; N=最多 N 次; -1=常駐每輪（預設 -1）
  "play_count": 1,       // 同一個 loop 中連播幾次：0/缺省→1; -1=無限連播
  "play_interval": 0,    // 相隔多少個循環播一次：0=每個循環都播; 1=隔 1 循環（=每 2 循環一次）
  "maxF": 0,             // 每次播放最大幀數；0=不限制
  "mapping": "gundam",   // 選用：預設 mapping（id 或 name）；可省略
  "map": [
    { "group": "1.1", "effect": 1, "write": "rgb" },
    { "group": "motors", "effect": "breathing", "write": "w" }
  ]
}
```

> ⚠️ **本節曾寫錯欄位名與語意**（舊版寫成 `play_count` = 出現輪數、`play_interval` = 每 N 輪）。
> 2026-10 已依 `slave/tasks/pixel_task.py:296-302, 478-492` 更正。
> **「出現幾輪」是 `play_loop`，不是 `play_count`。** 現有 mode 檔（`demo_eyes.json`、`motor_sine.json`）都用 `play_loop` + `maxF`。

- **group 複合引用**：`mapping.group`，兩邊各可用 id 或 name（`gundam.motors` / `1.motors` / `gundam.1` / `1.1`）；無點號時以頂層 `mapping` 為預設。
- effect 同用 id 或 name 引用。
- **同 mode 內 group 引用不得重複**，重複 → warn + 只保留第一項。
- 播放參數單位全用 **frame**（不用 ms，節奏由播放端控制）。

`write` 白名單：`r` / `g` / `b` / `w` / `ww` / `rgb` / `rgbw` / `wwww`。

### 2.4 registry.json — 播放清單 + 自動播放

```json
{
  "version": 1,
  "auto_play": true,
  "list": ["demo1", "demo2"]
}
```

- `list`：mode 名稱（或 id）依序播放 = 大隊列；順序即播放順序，播完一輪 → 再從頭循環。
- `auto_play=false`（或檔案不存在）→ 不自動播放，等指令層下 `pixel_play`。

---

## 3. 播放模型（大隊列 show）

- show = registry.list 的 mode 序列，循環播放；每播完一輪 `pass+1`。
- mode 每次播放 = 用 effects.json params **重建 generator**（fresh gen），播到耗盡（StopIteration）。想播久一點 → 在效果內延長（如 `end_Time`）。
- 例外：**下一個要播的 mode 與剛播完的是同一個**（如播放清單連續放同一 mode）→ 重用現有
  generator（`restart()` + 重置 done），不釋放不重建，避免重複播放時「剷除 → 重建」造成卡頓。
  若 generator 不支援 `restart()`（原生 generator 物件）→ 自動回退剷除重建。
- 每輪依播放參數決定該 mode 是否出現（`_should_play`，`pixel_task.py:478-492`）：
  - `play_loop==0` → 永遠跳過
  - `(pass-1) % (play_interval+1) != 0` → 這輪跳過（週期性）
  - `play_loop>0` 且已出現次數 ≥ `play_loop` → 不再出現
  - 其餘（含 `play_loop=-1` 常駐）→ 播放
- `play_count` 是**同一次 loop 內連播幾次**，不影響「哪幾輪出現」。
- 例：`[intro(play_loop=1), A(-1), B(-1)]` → 第 1 輪 intro+A+B，第 2 輪起 A+B 循環。
- 例：`ticker(play_loop=1, play_interval=4)` → 第 1、6、11…輪才出現。

---

## 4. 整合流程（一幀怎麼跑）

```
gen（效果生成器，fresh）──▶ array('H') 緩衝（0-4095）
        │ 依 mode 配對（mapping.group + write）
PixelLayout.scatter（亂序選擇 → 整齊表落點，viper）
        │
        ▼
big_buffer（RGBW 幀，bytearray）──▶ st_pixel.show_all()（一次推硬體）
```

- 幀格式：每顆控制單元 4 bytes（R,G,B,W），拼接順序 = 播放器 controllers 順序。
- `r/g/b/w`：每顆 1 值，只寫對應通道，其餘「不修改」（可累加組合）。
- `ww`：12-bit 完整（byte2 低 8 + byte3 高 4）；`wwww`：1 值代表整顆 pixel。
- `rgb`：3 值/顆；`rgbw`：4 值/顆；全部 >>4。
- 保底：值流不足 → 取模循環；過長 → 多餘丟棄；空 → 全寫 0。

---

## 4.1 Pixel Render 架構簡介（雙核 + hub + controller）

pixel 渲染由「計算核 + 播放核 + 中間 hub」三件套組成，寫效果的人完全不碰這些：

```
core1（計算核）PixelTask                  core0（播放核）RenderTask
┌─────────────────────────────┐          ┌─────────────────────────────┐
│ 效果計算 → scatter 進 hub    │  ──幀──▶  │ 固定 fps 節奏取幀 → show_all │
│ (pixel_stream hub)          │          │ 推硬體（WS2812/APA102/UART…）│
└─────────────────────────────┘          └─────────────────────────────┘
        ▲ 計算多快就多快                        ▲ 節奏固定，不受計算影響
        │ hub 滿就 drop 幀（不阻塞）            │ 沒幀就跳過（不重播）
```

| 角色 | 檔案 | 職責 |
|---|---|---|
| 計算核 | `tasks/pixel_task.py` | 初始化四層資料 + 大隊列播放 + `_tick_player` 全力算幀 scatter 進 hub |
| 播放核 | `tasks/render.py` | 依 `System.frame_interval_ms`（預設 20ms）從 hub `read_into` → `show_all()` |
| 中間 hub | `lib/sys/buffer_hub.py` | SPSC 無鎖環形緩衝（3 slot），計算滿就 drop、播放空就跳過 |
| 聚合播放器 | `lib/sw/PixelController.py` `PixelStreamer` | 把多個 controller（WS2812/APA102/PCA9685/UartMotor）合成一個 big_buffer |

**controller 介面**（每個硬體型別實作同一套，`PixelStreamer.show_all()` 依序呼叫）：

| 方法 | 說明 |
|---|---|
| `frame_size` | big_buffer 佔用（每顆 RGBW 4 bytes） |
| `st_load_and_convert(buf, offset)` | 從 big_buffer 轉換到硬體 buffer |
| `st_show()` | 推硬體 |
| `st_init()` | 初始化 |
| `neutral_value` | 停止/熄燈時填回的中性值（config `dStay`，12-bit >>4） |

**停止/熄燈 = 填中性值（`PixelStreamer.clear_all()`）**：
- 對齊舊專案 mp_LEDController 的 `dArc` 概念（reset 回到中性值）。
- config 每台設備可設 `dStay`（default Stay）：燈 `0`（熄滅）、motor `2048`（= 0x80 死區停）。
- 不能全清 0 —— UART-412 馬達的 `0` = 全速正轉（危險！）。
- 三處停止流程統一用 `clear_all()`：`render.py`（is_streaming 熄燈）、`pixel_task._stop()`、`Core_Manager` 退出。
- **暫停 = 電機也停（`PixelStreamer.stop_motors()`）**：`render.py` 的 is_paused 分支只把電機（`pixel_type="uartMotor1"`）填中性值歸位、燈保持最後一幀，且停止/暫停的中性幀只在狀態轉換時推一次（避免每 loop 推幀造成電機 UART 洪水）。

**motor 接入（UART-412）**：
- `UartMotor` 實作 controller 介面（`pixel_type="uartMotor1"`），從 big_buffer **W 通道**讀速度 byte（8-bit），`st_show()` 一次過發射單台 frame 串接（`ff addr value fe` × N）。
- UART-412 廣播模式受 `MAX_DEVICE=32` 限制，address > 32 時只能用單台串接。
- 歸零保護：W=0 會是「全速正轉」，故映射成中性值（死區 0x80）。
- 初始化：`driver/motor_drv.py` 讀 config `uartMotor` → 建 `UartMotor` → `boot.py` 註冊 → `pixel_drv.py` 聚合進 pixel_list。

---

## 5. PixelTask 初始化順序

```
on_start
├─ 1. 硬體：確保 st_pixel（無 → driver.pixel_drv.init_pixel()；config 全 disable → 空播放器）
│    聚合順序：apa102 + ws2812 + pca9685 + motor（boot.py 各 driver 先 init）
├─ 2. effects：py register + 載 effects.json → bus.shared["pixel_gens"]
├─ 3. mapping：從播放器推導 order/counts + 載 map/*.json → bus.shared["pixel_layout"]
├─ 4. modes：載 modes/*.json（解析複合 group 引用）→ bus.shared["pixel_maps"]
└─ 5. registry：載 registry.json → bus.shared["pixel_show"]；auto_play → 啟動 show
```

指令介面（bus.shared，指令層寫入）：
- `pixel_play` → 開始/重啟 show
- `pixel_stop` → 停止（熄燈）
- `pixel_pause` → 暫停 / 恢復

---

## 6. 自檢

```bash
# 於 slave/ 目錄執行
python3 lib/sw/pixel_layout.py   # 多 mapping + 複合引用 + 重複檢查 + scatter 保底
python3 pixel/effects/effects.py # 效果登記 + 生成器輸出（array('H')）
```

viper 速度只能在裝置上測（PC 沒有 micropython）。

## 相關文件

- `03_notes/07_pixel_test_results.md` — pixel 測試結果與未來方向
- `01_protocol/04_pixel_protocol.md` — Pixel 0x31xx 協議（MODE_LIST / MODE_SET 等）
- `03_notes/02_buffer_architecture.md` — 多級緩衝架構（pixel_stream hub，L4）
