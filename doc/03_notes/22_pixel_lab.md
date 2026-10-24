# PixelLab P1 — 三頁測試源

> 2026-10；UI 定案 v9；只是一個低優先測試工具，不是正式播放來源。

## 1) 三頁

| 頁 | mode | 輸出 | 目標 |
|---|---|---|---|
| PixelLab APA | `apa` | 4016 buffer 的 APA range（0..1999） | 該 type 的 index |
| PixelLab RGB | `rgb` | 4016 buffer 的 WS range（2000..3999） | 該 type 的 index |
| PixelLab PCA | `pca` | `pca9685_ctl` 選中的板（0x70/0x40/0x41） | 16 channel |

- 三頁互斥；返回/離開填 0。
- APA/RGB：base = 本 range 全部呼吸；selected = 選中 index 2 Hz 方波。
- PCA：base = 16 通道全部呼吸；selected = 選中 channel 2 Hz 方波。
- `SEL_MODE = "blink"`；未來改 `steady` 即可，UI 不提供選擇。

## 2) 低優先仲裁

`bus.shared["pixel_owner"]`：`None` / `"test"` / `"stream"` / `"pixel"`。

- PixelTask `_start` → `"pixel"`；`_stop` 若 owner 是自己 → `None`。
- StreamTask `_begin_load`/`_start_playing` → `"stream"`；`_reset` 若 owner 是自己 → `None`。
- PixelLabTask：owner 不是 `"test"` → 停止、不寫、不清；owner 回到 `None` 時
  若頁面仍 active 就自動恢復（測試頁等待，不是備份）。
- 頁面 `active=0` 且 owner=test → task 填 0 / 清 PCA，然後 owner=None。
- **沒有** LIFO、stack、flags 備份、上一個來源恢復。

## 3) 資料路徑

- APA/RGB：`view[:] = neutral_base` → `scatter_offs(range, [v], "wwww")`
  → `scatter_offs(selected, [4095/0], "wwww")` → `hub.commit()`。
- PCA：`pca.buf[0..15] = v`；`pca.buf[ch] = 4095/0`；`pca.show()`。
- PCA 頁 active 時 RenderTask `is_streaming=False, is_ready=True`（hold），
  避免 0x70 buffer 廣播與直接寫入互蓋。

## 4) UI

- APA/RGB：IO、選取 index、呼吸/選中兩個垂直方塊（50%/100%）。
- PCA：固定高度目標清單（可捲動）、**清單外**掃描鈕、直放晶片 2×8、
  中央 CH/百分比；地址只在清單。
- 沒有全熄/停止；back/leave = 停止+填 0。

## 5) 檔案/測試

- `slave/tasks/pixel_lab_task.py`
- `slave/ui/lvgl/page/pixel_lab_common.py` / `pixel_lab_apa.py` / `pixel_lab_rgb.py` / `pixel_lab_pca.py`
- `slave/tasks/pixel_task.py` / `stream_task.py`（owner gate）
- `slave/Core_Manager.py`（register `pixel_lab`）
- `temp/gen_font.py --write`（新增字型）
- 離線：task fake + page fake build PASS；未上板。
