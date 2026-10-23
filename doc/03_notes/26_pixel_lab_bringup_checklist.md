# PixelLab 上板前查遺補缺（2026-10-22，2026-10-24 補實測）

> 目標：把模擬器定稿的 PixelLab UI／三動作／base 燈效，整理成可上板、可驗證、
> 可 commit 的完整清單。這份是「還缺什麼、要決定什麼」，不是另一份設計文件。

---

## 0. 一句話結論

**畫面與操作已對上模擬器**；還缺的是「task 角色與 base 策略、面板→執行端轉發、
4016 config/硬體、上板驗證、git」。目前板子沒連，最新的 build 修正也還沒上板。

---

## 1. 已完成（working tree，尚未 commit）

| 項目 | 狀態 | 位置 |
|---|---|---|
| 三頁 PixelLab（APA/RGB/PCA） | 完成 | `slave/ui/lvgl/page/pixel_lab_*` |
| PCA 第一層「選區域」 | 完成 | 目標清單／晶片／掃描；enter 進入、exit 回上層 |
| 區域內游標 vs enter commit | 完成 | 左右只動數字；enter 才寫 state |
| 掃描可直接選 | 完成 | 第一層就能選掃描，不必先目標→通道 |
| PCA ALL_LED | 完成 | 游標 -1 ↔ state 0xFF；task 一次寫 16 通道 |
| 晶片排版／腳位／中央 ALL | 完成 | 依模擬器定稿（104×138、2×8、腳位外露） |
| IO／pins 分兩行 | 完成 | task `_publish(pins=)`；頁面 `_pins_lb` |
| 頁面順序 7 頁 | 完成 | Pixel 控制 0／PCA 1／RGB 2／APA 3／遙控器 4／ESP-NOW 5／設定 6 |
| control_panel 移除 | 完成 | `page/__init__.py` 不再 import／register |
| icons gauge/lightbulb/zap | 完成 | `lv_icons.ICONS` 與 `icons_16.bin` 已有 |
| 字型 | 不需重做 | ALL/SDA/SCL 是 ASCII，`·` 已有；`zh_hant_16.bin` 138720 |
| mk_vbar 真機 build 修正 | 完成 | 不再掛 `fill._vbase`；`set_vbar(..., x=, y=)` |
| fake page/task 測試 | PASS | `/tmp/test_pixel_lab_pages_fake.py`、`/tmp/test_pixel_lab_task.py` |

### 1.1 這次修的關鍵 bug

- `pixel_lab_common.mk_vbar()` 原本把 `fill._vbase = (...)` 掛在 `lv.obj` 上。
- 真機 MicroPython binding 不允許自訂屬性 → `SimplePage.build()` / `PcaPage.build()`
  丟 `AttributeError` → `app.build_all` 印 `[app] build skip pixel_lab_*` →
  `_screens` 沒有三頁 → launcher 有卡片但 `app.go()` 直接 return。
- 修正：`set_vbar(..., x=, y=)` 明傳基準；`SimplePage._paint_bar` /
  `PcaPage._paint_squares` 各自傳 `b["x"]/b["y"]`、`s["x"]/s["y"]`。

---

## 2. 尚未完成／需要決策

### 2.1 「基礎燈泡」是哪一個？【已採 A】

> 2026-10-24 已改為 **A：全部燈 base + selected 最後覆蓋**（固定次序，不用 max）。
> 下面保留當初的 A/B/C 說明；實際程式已走 A。

目前 `pixel_lab_task` 有兩種 base：

- **APA/RGB**：`_tick_buffer()` 只把「本頁 type 的 range」填 base，選中 index 填
  steady/blink；其他 type 保持 neutral。
- **PCA**：`_tick_pca()` 把 16 通道填 base，選中 channel（或 ALL）填
  steady/blink。

與你之前說的「全部 4016 一起呼吸，選中的長亮/閃」不同：現在是 **per-page
type range**，不是全部 4016。請選：

- **A. 全部 4016 base**：整槽 base 呼吸，選中 LED 在当前 page 的 type；
  切頁時把上一個 page 的 selected 關掉，但 base 仍在。最接近你之前的方法。
- **B. 維持 per-page**：只有当前 page 的 type 呼吸，其他 type 0（目前程式）。
- **C. 只做 UI base bar**：燈效之後再說（目前頁面已有 base bar，但 task 沒跑）。

> 選 A 需要改 `_tick_buffer`：`view[:] = base 全槽`，再把 selected 寫进
> 当前 page 的 range；PCA 頁則維持 16 通道 base。

### 2.2 面板 vs 執行端【決策】

這塊 `ports/S3/ESP32-S3-Control_Panel_F` 的 README 原本寫「面板只發指令，不自己執行」，
但 2026-10-22 已把 port `Core_Manager.py` 更新為「面板 + 本地燈」：現在註冊
`stream` / `pixel` / `render` / `pixel_lab`（見上方 1. 已完成）。port config 目前
`WS2812/APA102/PCA9685` 都 enable，boot log 則是：

```
[INFO] WS2812: 1 channel(s)
[ERROR] APA102: spi index 1 not found
[INFO] APA102: 0 channel(s)
[WARN] PCA9685: no physical device found — skip 0x70 broadcast
[INFO] PCA9685: 0 stream controller(s), 0 control target(s)
```

請確認：

- **A. 純面板**：PixelLab pages 只做 UI；`pixel_cpanel` 要把 `pixel_lab` 狀態
  轉成命令送給 4016 執行端；`pixel_lab_task` 只跑在執行端。
- **B. 本地 4016 執行端**：這塊板要跑 `pixel_lab_task`，port `Core_Manager`
  要加回 `pixel`/`render`/`pixel_lab`，port config 要是 4016（目前 num_pixels=336）。

> 目前沒有把 `pixel_lab` 狀態從面板送到執行端的協議；`pixel_cpanel` 只認
> `_pixel_cmd` 的 `mode/stop/brightness`。這是最大的功能缺口。

### 2.3 Task 位置與註冊【決策】

- port `Core_Manager.py` 已註冊 `pixel_lab`，task 會在本板跑。
- 但 `st_pixel` 受 port config/硬體影響：boot log 只有 WS2812 2000，APA102/PCA
  找不到；task 只會控制實際存在的 range。PCA 頁 direct 也需要 PCA 實體。
- 若要走「面板只發指令」的舊角色，把 port CM 的四行 register 註解掉即可；
  本版預設是「面板 + 本地燈」。

### 2.4 4016 config / 硬體【缺口】

- port config：`WS2812` 1×2000、`APA102` 1×2000、`PCA9685` 1×0x70；`num_pixels`
  仍是 **336**，不是 4016。
- boot log：APA102 `spi index 1 not found`；PCA9685 沒有實體（0 channel）。
- 若 4016 是目標，需確認：
  - APA102 的 SPI index 1 是否存在／pin 是否正確；
  - PCA9685 0x70 是否在 I2C bus 0；
  - `System.num_pixels` 是否要改 4016（給 stream/live；PixelLab 用 st_pixel 的
    total_bytes，不受這個值影響，但 live/stream 會受影響）；
  - port config 的 `WS2812/APA102/PCA9685` 是否要保留 enable。
- 板子 boot 目前是 WS2812 2000；APA/PCA 0，等於只有 2000 顆可用。

### 2.5 正常上傳 / port delta【缺口】

你說的「正常上傳」是 port delta：

- `ports/S3/ESP32-S3-Control_Panel_F/Core_Manager.py`
- `ports/S3/ESP32-S3-Control_Panel_F/config.json`
- README 不上板。

但 **port delta 不會自動帶 `slave/ui/lvgl/page` 的新頁**。要確認正常上傳有包含：

```
slave/ui/lvgl/page/__init__.py
slave/ui/lvgl/page/pixel_lab_common.py
slave/ui/lvgl/page/pixel_lab_apa.py
slave/ui/lvgl/page/pixel_lab_rgb.py
slave/ui/lvgl/page/pixel_lab_pca.py
slave/ui/lvgl/page/pixel_controller.py
slave/ui/lvgl/page/remote.py
slave/ui/lvgl/page/now_setting.py
slave/ui/lvgl/page/settings.py
slave/tasks/pixel_lab_task.py
```

目前板子 `/dev/cu.usbmodem101` 不在，無法直接驗證板上檔案；接上後要：

1. 看 boot log 是否還有 `[app] build skip pixel_lab_*`；
2. 進 launcher 是否出現 `[nav] -> pixel_lab_*`；
3. 若還有 skip，把 traceback 貼出來。

### 2.6 Git / 文件【缺口】

- 上一個 commit 是 `5414e06`；本次 wave tool／direct zero-copy／固定次序／
  Core 1 平衡／FPS metrics 已一併 commit（見 `git log`）。
- GitHub push 之前卡在憑證；需要你自己 push 或設定 PAT。

### 2.7 測試/驗證【缺口】

- fake page/task PASS。
- 模擬器在 `http://127.0.0.1:8610/?project=slave`（ego browser 找不到 Chrome，
  無法截圖；你可直接開）。
- 實機：尚未驗證 build 修正後的三頁進入、區域選擇、掃描、ALL、pins。
- 實機：尚未驗證 task 的 base/selected（因為 task 在面板上沒註冊）。

---

## 3. 上板檔案清單（最小）

### 3.1 只修 UI build / 操作（目前板子）

```
/ui/lvgl/page/pixel_lab_common.py
/ui/lvgl/page/pixel_lab_pca.py
```

### 3.2 連同模擬器定稿的頁面/順序

```
/ui/lvgl/page/__init__.py
/ui/lvgl/page/pixel_lab_common.py
/ui/lvgl/page/pixel_lab_apa.py
/ui/lvgl/page/pixel_lab_rgb.py
/ui/lvgl/page/pixel_lab_pca.py
/ui/lvgl/page/pixel_controller.py
/ui/lvgl/page/remote.py
/ui/lvgl/page/now_setting.py
/ui/lvgl/page/settings.py
/tasks/pixel_lab_task.py
```

### 3.3 面板/執行端（依 2.2 決策）

- 本版 port `Core_Manager.py` 已含 `stream/pixel/render/pixel_lab`；
  只需上傳 port delta（Core_Manager + config）。
- 本地 4016：port `config.json` 改 4016；確認 APA102 SPI1、PCA 0x70 實體；
  `num_pixels` 336 → 4016（live/stream；PixelLab 用 st_pixel 不受此值影響）。
- 純面板（舊角色）：把 port CM 的四行 register 註解掉，另補執行端協議。

---

## 4. 建議實作順序

1. **接上板子**：上傳 3.1 兩檔，確認 `[app] build skip` 消失、`[nav] ->` 出現。
2. **決定 2.1**：A/B/C；若 A，改 `_tick_buffer` 全槽 base。
3. **決定 2.2**：純面板或本地 4016；決定 task 位置。
4. **補協議**（若純面板）：`pixel_cpanel` 將 `pixel_lab` 狀態送執行端；
   執行端 task 接收 target/ch/index/ALL。
5. **config/硬體**：4016、APA102 SPI1、PCA 0x70、num_pixels。
6. **實機驗證**：三頁、區域、掃描、ALL、pins、base/selected、owner/被接手。
7. **commit/push**：把 2.6 的 modified 檔一起 commit；push 用你的憑證。

---

## 5. 需要你回答（只有這幾個）

1. 「基礎燈泡」是 **A 全部 4016 base**、**B per-page base**、還是 **C 只做畫面**？
2. 這塊板是 **A 純面板** 還是 **B 本地 4016 執行端**？
3. 4016 的 APA102 SPI index / PCA 0x70 接法是否已接好？（boot log 目前找不到）
4. 正常上傳是否有包含 `slave/ui/lvgl/page/*`，還是只傳 port delta？

---

## 6. 2026-10-24 實測：Core 0 / Core 1 與 50fps

### 6.1 測試條件

- Port `Core_Manager.py`：`pixel` / `render` / `pixel_lab` 都在 Core 1，
  `lvgl` / `network` / `now` / `cpanel` / `pixel_cpanel` 在 Core 0。
- 用 `/bench` 旗標自動啟動 P1 RGB（index 0、全部燈 base），量測後已移除
  bench 鉤子；`/bench` 檔本身留在板上也不再生效。
- FPS log 由各 task 每 1 秒輸出一次（`[FPS] lvgl/lab/pixel/render`）。

### 6.2 量測結果

| 項目 | P1 在 Core 0（調整前） | P1 在 Core 1（調整後） |
|---|---|---|
| LVGL | **10.6 fps**，step avg 0.60ms | **42.7 fps**，step avg 0.51ms |
| P1 `pixel_lab` | **10.6 fps**，tick avg **85.3ms** | **10.5 fps**，tick avg **85.3ms** |
| 主 `pixel` | 0（沒播 mode） | 0（沒播 mode） |
| 主 `render` | 0（idle） | 0（idle） |
| 現象 | 畫面被 P1 的 85ms show_all 拖到 10fps | 畫面回到 42.7fps；P1 仍 10.5fps |

### 6.3 結論

1. **畫面不是吃 CPU 的元凶**：LVGL `app.step()` avg 只有 0.51～0.80ms；
   之前 10.6fps 是因為 P1 在 Core 0，`show_all()` 一次 85ms，把 Core 0
   runner 卡住。
2. **把 `pixel` / `pixel_lab` 移到 Core 1 是有效的平衡**：Core 0 留給 UI，
   LVGL 由 10.6 → 42.7fps；Core 1 由 P1 使用（P1 85ms/幀）。
3. **50fps 不是排程問題，是 WS2812 的物理上限**：
   WS2812 2000 顆 @800kHz ≈ 2000×24×1.25µs ≈ **60ms**，加轉換/填值約
   **85ms/幀 → 約 11.7fps**。`FRAME_MS=20`（50fps）只是 gate，做不到。
   4016 顆 WS2812 會更慢（約 170ms/幀 → 5.8fps）。
4. **要真的 50fps**：用 APA102/SPI（4016×32bit / 20MHz ≈ 6.4ms）或減少
   WS2812 顆數；PCA9685 只有 16 通道，不是瓶頸。本板目前 APA102/PCA
   都是 0，只有 WS2812 2000，所以 P1 的上限就是 ~11fps。
5. 主 `pixel`/`render` 這次沒有播 mode（`show list=0`），所以 `[FPS] pixel`
   是 0；它們的 50fps 同樣受 `show_all()` 的 85ms 限制，不是 20ms gate。

### 6.4 目前留著的量測程式

- `slave/tasks/lvgl_task.py`、`pixel_task.py`、`render.py`、`pixel_lab_task.py`
  各有 1 秒一次的 `[FPS]` log；`pixel_lab_task.py` 的 `/bench` 鉤子已移除。
- 不想要 log 時，把各 task 的 `_fps_log()` 呼叫註解掉即可；不影響燈效。
