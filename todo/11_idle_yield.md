# `sleep_ms(0)` → `machine.idle()`（熱迴圈讓步優化）

> **用途**：追蹤把高頻「讓步點」從 `time.sleep_ms(0)` 換成 `machine.idle()` 的效益、範圍與風險。
> **最後更新**：2026-10-24
> **相關文件**：`test/boot/README.md` §7、`test/boot/bench_yield.py`

## 已完成（實測通過）

- [x] 每次呼叫成本：`machine.idle()` **21.0 µs** vs `time.sleep_ms(0)` **46.4 µs**（已扣空迴圈 1.8 µs）
- [x] 讓步公平性：陪跑執行緒 236k/s（idle）vs 216k/s（sleep_ms0）→ **等價**
- [x] `sleep_ms(1)` × 1000 = 1001 ms → tick 粒度 1 ms，是真的在等
- [x] 現況盤點：`sleep_ms(0)` 共 **11 處**
      （`task_manager` ×4、`fs_manager` ×2、`uart_drv`、`net_bus`、`circuit_bus`、`bus_speed`、`lvgl_task` 僅註解）

## 已完成（loopback/單元自測，非實測）

- [ ] （無）

## 待跟進（實測）

- [ ] 只改「每秒 > 5000 次」的熱迴圈；每輪/每檔一次的讓步**維持不動**
- [ ] 改完用 `bench_yield.py` 對照前後：幀率、CPU 佔用、`[TM]` 每輪耗時
- [ ] 若同時用到 `micropython.schedule()` 的迴圈，**不可**換成 `idle()`（它不跑 `mp_handle_pending`）

## 已知問題 / 待決

- [ ] 現有差異量級太小：158 檔 × 25 µs ≈ **4 ms/次開機**，單獨做沒有感覺，應與其他開機優化一起做
- [ ] 是否統一封裝（例如 `lib/sys/yield_.py::once()`）以免 11 處各自為政

## 筆記

- 裝置：ESP32-S3 Octal-SPIRAM、MicroPython `v1.29.0-preview`、240 MHz
- 量測方式：`test/boot/bench_yield.py` + `test/boot/dev_run.py --pull`
- esp32 port 原始碼：`machine.idle()` = `MP_THREAD_GIL_EXIT(); taskYIELD(); MP_THREAD_GIL_ENTER();`
  `time.sleep_ms(0)` = `mp_handle_pending()` + socket event poll + 同樣的 yield
