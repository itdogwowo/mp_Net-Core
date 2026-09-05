# 16 — Pixel 控制面板臨時改動清單（架構待重整）

> **狀態**：⚠️ 臨時應急，架構不符合既有分層，待日後重構
> **日期**：2026-09-05
> **背景**：為讓 LVGL 面板「點擊即發 ESP-NOW MODE_SET（0x0200 可動）」先跑通，
> 在 UI 頁面層做了繞過架構的臨時實作。本文標記所有改動點，供日後逐項修正。
> **範圍**：本樹 = 面板裝置（LCD + encoder + 按鍵 + LVGL）。

---

## 1. 一句話總結

臨時為了「點擊可動 → 馬上 ESP-NOW 廣播 0x3105 MODE_SET」，**在 LVGL 頁面裡直接
操作底層 `espnow` 物件 `send()`**，繞過了「UI 寫狀態 → task 消費 → gmode 中間層
扇出」的正規架構。這與既有 `ControlPanelTask`（消費 `_display_cmd` → 廣播 0x1501）
的分層精神相違，是本文要標記的重點。

---

## 2. 臨時改動清單（⚠️ 需重構）

### 2.1 `slave/ui/lvgl/page/pixel_controller.py` — UI 直接發硬體（核心 hack）

**改動**：`_espnow_send_mid()` 直接在頁面函式內：

```python
now = bus.get_service("NowBus")
esp = now._esp                        # 直接抓底層 espnow 物件
esp.send(b"\xff\xff\xff\xff\xff\xff", frame)   # 繞過 NowBus 封裝
```

若 `NowBus` 不存在還會自行 `espnow.ESPNow()` + `active(True)` + `add_peer(bcast)`。

**問題**：
1. UI 層直接碰硬體，違反「硬體一律由 driver 初始化、UI 只從 bus 取用」的原則
   （見 `doc/02_guides/06_lvgl_ui.md` §3）。
2. 繞過 `NowBus.broadcast()`（它本來就有 stats 計數、connected 判斷、MAX_PAYLOAD
   檢查），喪失這些防護。
3. 繞過 gmode，模式扇出（audio 同步）完全沒走。
4. `_set_mode()` / `_sel_mode_delta()` 也都呼叫這個硬體直發，列表選模式同樣繞路。

**正規做法（待改）**：
UI 只寫 `bus.shared["_pixel_cmd"] = {"mode": id}`，由 `PixelControlPanelTask` 消費
後發送（見 §2.2）。這條 UI→task 鏈已在 `_set_mode` 的上一版實作過，只是後來因
「0x0200 被 gmode 攔截」而改成硬體直發。

---

### 2.2 `slave/tasks/pixel_control_panel.py` — 變成孤兒 task

**現況**：此 task 已建立且註冊，但目前**沒有消費者**——因為 UI（§2.1）已經直接
硬體直發，不再寫 `_pixel_cmd`，所以這個 task 的 `loop()` 永遠讀不到東西。

**這個 task 的兩個歷史版本都留下來當參考**：
- 版本 A（gmode 版）：消費 `_pixel_cmd` → `gmode.set_mode()`。問題：面板自己沒有
  SERVO 模式（0x0200），gmode 會回 False 攔截，燈效/audio 都發不出去。
- 版本 B（廣播版）：消費 `_pixel_cmd` → 拆 (mode_type, mode_id) → `now.broadcast(
  0x3105 MODE_SET)`。方向正確（面板只是「發送給執行裝置」），但因 USB 上傳失敗
  未實地驗證。

**正規做法（待定）**：釐清「面板 → 執行裝置」的傳輸通道是 ESP-NOW 還是 UART，
再由這個 task 統一轉發（對齊 `ControlPanelTask._forward_display_cmd` 的「讀狀態
→ 廣播」模式）。0x0200 應由**執行裝置**自己的 gmode/pixel_task 去解析，面板端
不驗證。

---

### 2.3 `slave/Core_Manager.py` — 註冊了 `pixel_cpanel`

**改動**：
```python
from tasks.pixel_control_panel import PixelControlPanelTask
tm.register_task("pixel_cpanel", PixelControlPanelTask, default_affinity=(1, 0), layer=1)
```

**問題**：因為 §2.1 已硬體直發，這個 task 目前空轉（`_pixel_cmd` 永遠是 None）。

**正規做法（待定）**：等 §2.2 的 task 邏輯定案後，此註冊才真正生效；若最終決定
UI 直接發（短期），這行與 §2.2 檔可先移除。

---

### 2.4 `slave/ui/lvgl/page/__init__.py` — 新增 pixel_controller 頁註冊

```python
from ui.lvgl.page import pixel_controller      # try-import
("pixel_controller", pixel_controller),        # _PAGES_MOD
```

**這項是正確的**（加新頁面的標準流程，見 `doc/02_guides/06_lvgl_ui.md` §9），
保留。只是頁面內部邏輯（§2.1）是臨時的。

---

## 3. Bug 修復（✅ 保留，非臨時 hack）

以下改動是修 bug，**不是**臨時應急，不要誤刪：

| 檔案 | 修了什麼 | 原因 |
|---|---|---|
| `slave/lib/sys/bus_adapter.py`（+P4） | `SpiBusAdapter.write_data_async` 大 buffer 分 32KB chunk | 整幀 153600B 一次塞 4-deep DMA queue 會 `queue failed err=0x101` 只送第一段 |
| `slave/driver/tft_drv.py`（+P4） | 黑色填充統一走 `lcd.show(black)`（配合上面分 chunk） | 開機清屏只填一部分；兼容所有 adapter（SPI-DMA/一般 SPI/I2C/I80） |
| `slave/Core_Manager.py`（+P4） | `lvgl` 的 `layer=-1` → `layer=1` | layer=-1 在 `task_manager._task_eligible_for_boot` 永遠 return False → LVGL 不啟動 |
| `slave/Core_Manager.py`（+P4） | 取消註解 `cpanel`（ControlPanelTask） | 面板裝置標準配置需要它；之前「LVGL 有畫面沒訊號」就是它沒啟動 |
| `slave/action/pixel_actions.py`（+P4） | 移除「gmode 為 None 時自行寫 mode_id」的 fallback | 繞過 gmode 會漏 audio 扇出；gmode 一定存在，直接走 gmode 才是對的 |

---

## 4. 正規架構回顧（日後對照重構）

```
UI 頁面（只寫狀態，不碰硬體）
   │  bus.shared["_pixel_cmd"] = {"mode": id}
   ▼
PixelControlPanelTask（消費狀態 → 轉發）
   │  now.broadcast(0x3105 MODE_SET) 或 本板 gmode.set_mode()
   ▼
執行裝置收 0x3105 → pixel_actions.on_mode_set → gmode.set_mode()
   │  mode_id/mode_seq + audio 扇出
   ▼
PixelTask / DjTask 各自讀自己的模式池執行
```

關鍵原則（重申）：
- **UI 只寫狀態，不碰硬體**（`doc/02_guides/06_lvgl_ui.md` §3）。
- **模式是「大家各自讀」**：面板不驗證 0x0200 是否存在，執行裝置各自讀各自的
  模式池（0x0200 = SERVO 組 mode 0 是執行裝置/motor 的模式，面板沒有）。
- **gmode 是單一事實來源**：所有模式入口收斂到 `gmode.set_mode/stop_mode`，
  不要自行寫 `bus.shared["mode_id"]`（會漏 audio 扇出）。

---

## 5. 待決問題（下次重構時拍板）

1. **面板 → 執行裝置走 ESP-NOW 還是 UART？** 目前 `PixelControlPanelTask` 兩版都
   用 ESP-NOW，但 `CircuitTask`/`CircuitBus`（UART）也是可用通道，需確認實體接線。
2. **0x0200 的歸屬**：SERVO 模式應由執行裝置的 motor 任務（`ActionTask1`）執行，
   面板只是轉發。目前 `ActionTask1`（motor）在 Core_Manager 是**註解掉的**，執行
   裝置端要啟用它才有實際動作。
3. **ESP-NOW 不 loopback**：面板自己廣播、自己收不到。驗證「收到」必須用第二塊板
   跑 `test/protocol/espnow_mon.py`（ch6 + add_peer broadcast）。
4. **`_pixel_cmd` 契約**：定案後統一欄位名與語意（`{"mode": id}` / `{"stop": True}`
   / `{"brightness": v}`），並決定是否要做同值去重。

## 相關文件

- `02_guides/06_lvgl_ui.md` — LVGL UI 架構與分層原則
- `02_guides/08_pixel_subsystem.md` — pixel 子系統與模式池
- `01_protocol/04_pixel_protocol.md` — 0x31xx 模式指令（MODE_SET 0x3105）
- `15_audio_p4_block_vs_irq_espnow.md` — ESP-NOW 在此硬體的限制
