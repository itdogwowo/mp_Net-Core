# 待機 / 休眠整合計劃（Standby & Sleep）

> **用途**：把「待機 / 休眠」的共識、實測基線、分階段做法與風險集中一處，之後要實作時直接接手。
> **本檔自足**：不依賴任何對話上下文，單獨看就能動工。
> **狀態**：PLANNED（Phase 0 已完成＝量測工具與基線；Phase 1~3 尚未實作）
> **最後更新**：2026-10-24
> **相關文件**：
> - `test/boot/README.md` §7 — 量測工具與實測基線（開機時間軸 / import 成本 / 休眠對照）
> - `todo/11_idle_yield.md` — `sleep_ms(0)` → `machine.idle()` 的讓步優化（同屬耗電主題，無依賴）
> - `slave/lib/sys/task_manager.py`、`slave/lib/sys/network_manager.py`、`slave/driver/tft_drv.py`

---

## 0. 一句話

提供三種「待機」，由 config 切換、**預設關閉**：
**app 待機**（0 秒、WiFi 不斷）、**lightsleep**（0.3 ms、WiFi 要重連）、**deepsleep**（33 秒、等於重開機）。

---

## 1. 為什麼要有這份計劃（實測基線）

在 S3 Control Panel（ESP32-S3 Octal-PSIRAM、MicroPython `v1.29.0-preview`、240 MHz）上實測：

| 項目 | 數字 | 來源 |
|---|---|---|
| 冷開機 → `Boot complete → running` | **32.8 s** | `capture_boot.py --trigger hard --via-repl` |
| boot.py → `[BOOT] ok` | 10.3 s | 軟復位對照 |
| `lightsleep(2000)` 實際耗時 | 2000.0 / 2000.3 ms | `probe_sleep.py` |
| lightsleep 喚醒額外延遲 | **−0.03 / +0.31 ms** | 同上 |
| lightsleep 期間背景執行緒 | **完全停止**（計數 +0） | `bench_sleep_diff.py` |
| `time.sleep_ms(2000)` 期間背景執行緒 | 持續跑（+1992） | 同上 |

結論：**只有 lightsleep 能「保留狀態瞬間恢復」；deepsleep 醒來就是重開機，33 秒要再付一次。**

---

## 2. 名詞定義（先統一，避免再混淆）

| 名稱 | 是什麼 | 不是什麼 |
|---|---|---|
| `time.sleep_ms(n)` | 等待／讓步（CPU 全速） | ❌ 不省電、不是休眠 |
| `machine.idle()` | 放 GIL + `taskYIELD()`（esp32 實作只有這樣） | ❌ 不是省電模式 |
| **app 待機** | 停 task + 關背光，**CPU 照跑、WiFi 保持** | ❌ 不是休眠（但 0 秒恢復） |
| `machine.lightsleep(n)` | CPU/周邊時脈停、**RAM 與程式狀態保留** | ❌ 醒來後 WiFi 已斷，要重連 |
| `machine.deepsleep(n)` | 斷電，只留 RTC 域 | ❌ 不是「暫停」，醒來是完整重開機 |

---

## 3. 範圍

| 本計劃要做 | 本階段不做（另案） |
|---|---|
| config 定義 + `standby` 模組骨架 | deep sleep 的完整狀態還原（Phase 3 選配） |
| 子系統 `suspend()/resume()` 介面與順序 | 硬體 power path / 電池（`doc` 另案） |
| 閒置判定（輸入計時器）與三種模式 | 背光 PWM 調亮度（硬體改動） |
| 喚醒源：GPIO（按鍵/編碼器）、定時器、VBUS | 待機期間的 OTA |
| lightsleep 後的 WiFi 重連與顯示恢復 | 多螢幕/多面板同步待機 |

---

## 4. 架構設計

### 4.1 新增模組：`slave/lib/sys/standby.py`

```python
standby.init(bus)              # boot.py 尾端註冊（讀 config，預設 enable=0 → 完全 no-op）
standby.request(mode, reason)  # "app" | "light" | "deep"；由閒置計時器 / 按鍵 / 指令呼叫
standby.enter(reason)          # 進入待機（依順序 suspend 各子系統）
standby.exit()                 # 喚醒後恢復（反向順序）
standby.status()               # dict，給 web_ui / log / 除錯
standby.touch()                # 任何使用者輸入時呼叫，重置閒置計時
```

設計原則：
- **預設關閉**：`System.standby.enable=0` 時 `init()` 直接 return，不注冊任務、不改任何行為。
- **可逆**：所有動作都要有對應的 resume；任一子系統 suspend 失敗 → 整筆回滾（先 resume 已成功的）。
- **不碰硬體細節**：standby 只呼叫子系統的介面，不直接操作 SPI/GPIO。

### 4.2 子系統介面約定

每個 service 可選擇性提供 `suspend()` / `resume()`（沒有就跳過）：

| 子系統 | suspend | resume |
|---|---|---|
| `task_manager` | `pause_all()`（停新排程、等當前 tick 結束） | `resume_all()` |
| `lvgl` | 停 flush、停 render task | `lvgl.invalidate()` 強制重畫 |
| `tft` | `DISPOFF`（可選 `SLPIN`） | `DISPLAY_ON`（`SLPOUT` 後等 120 ms） |
| 背光 | `bl.value(0)`（Phase 4 改 PWM duty=0） | `bl.value(1)` / 還原 duty |
| `audio` | mute + 停 I2S | 還原音量、重開 I2S |
| `network` | （light/deep 才）`disconnect()` | 重連 + `pm=0` + NetBus 重新註冊 |
| `pixel` | 保持當前幀（不送新幀） | 續送 |
| `pca9685` | 保持輸出（可選進 SLEEP） | 還原輸出 |

### 4.3 進入 / 退出順序

**進入（app 待機）**
```
1. 停止接受新輸入（避免邊睡邊按）
2. task_manager.pause_all()
3. lvgl 停 flush / 停 render
4. tft DISPOFF
5. bl.value(0)
6. （light 模式才）network.disconnect() → lightsleep(ms 或 None)
```

**退出（喚醒）**
```
1. bl.value(1)                    # 先給使用者「回來了」的回饋
2. tft DISPLAY_ON（必要時完整 init）
3. lvgl.invalidate()
4. task_manager.resume_all()
5. （light 模式）WiFi 重連（背景，不阻塞 UI）
6. 重置閒置計時器
```

> ⚠️ 順序不可顛倒：先 resume task 再開顯示，會出現「畫面已亮但內容是舊的」的殘影。

### 4.4 閒置判定

- 來源：按鍵 / 編碼器 / 觸控 / 網路指令 / 播放中狀態
- 規則：`now - last_input > System.standby.idle_ms` → 進入待機
- 例外（不進待機）：OTA 中、升級中、`engine_run` 播放中（可設定）、除錯模式
- 計時器用現有 `lib/sys/timer.py`，不新增 task

---

## 5. Config 設計（`config.json` → `System.standby`）

```json
"System": {
  "standby": {
    "enable": 0,
    "mode": "app",
    "idle_ms": 60000,
    "wake_pins": [1, 2],
    "wake_level": 0,
    "light_ms": 0,
    "wifi_off": 1,
    "restore": ["page", "mode", "brightness"]
  }
}
```

| 欄位 | 語義 | 預設 |
|---|---|---|
| `enable` | 0 = 完全停用（行為與現在一致） | 0 |
| `mode` | `app` / `light` / `deep` | `app` |
| `idle_ms` | 無輸入多久進待機 | 60000 |
| `wake_pins` | 喚醒用 GPIO（標籤或編號） | `[]` |
| `wake_level` | 0 = 低電位喚醒、1 = 高電位 | 0 |
| `light_ms` | lightsleep 上限；0 = 只靠 GPIO（無限） | 0 |
| `wifi_off` | 待機時是否主動斷 WiFi | 1 |
| `restore` | 醒來要還原的狀態清單（Phase 3 用） | `[]` |

---

## 6. 分階段

### Phase 1 — app 待機（風險最低，先做這個）
- 改動：新增 `lib/sys/standby.py`；`Core_Manager` 註冊；`boot.py` 尾端 `standby.init(bus)`
- 內容：停 task、停 render、關背光、（可選）降頻
- **驗收**：進入 < 100 ms、退出 < 100 ms；WiFi 不掉；連續 100 次無洩漏；畫面無殘影
- 回滾：`enable=0` 即可，無殘留

### Phase 2 — lightsleep
- 改動：同上 + 喚醒源設定（`esp32.wake_on_ext0/ext1`）+ WiFi 重連 + 顯示恢復
- 內容：睡前 `network.disconnect()`、`bl.value(0)`；醒來重連（背景）＋ `pm=0` 重設
- **驗收**：喚醒到可操作 < 5 ms（不含重連）；重連 < 5 s；連續 10 次循環不卡死；USB 斷線不影響功能
- 注意：`lightsleep` 期間 GPIO 保持 → **背光一定要先關**，否則省的電全被背光吃掉

### Phase 3 — deepsleep（選配）
- 改動：睡前把狀態寫 NVS（`esp32.NVS`）；`boot.py` 早期讀回
- 內容：定時/GPIO 喚醒；醒來還原頁面/模式/音量
- **驗收**：狀態還原正確；喚醒總時間可接受（目前 33 s，需先做開機優化）
- 前提：Phase 1/2 完成 + 開機時間優化（config 重複解析、lazy import）有進展

### Phase 4 — 硬體（另案，不在本計劃）
- power path（拔 USB 不斷電）、VBUS 偵測、背光 PWM、面板 VCC 高側開關
- 動機見「§9 未決問題」

---

## 7. 驗收清單

- [ ] `enable=0` 時行為與現在 100% 相同（diff 級驗證）
- [ ] 三模式可切、可由 config 指定、可由指令觸發
- [ ] 待機 / 喚醒不影響 task 時序（`[TM]` 每輪耗時不變差）
- [ ] 連續循環 100 次：`gc.mem_free` 穩定、無 task 堆積
- [ ] 螢幕恢復無殘影、無花屏
- [ ] 網路恢復：重連 + NetBus 重新註冊 + heartbeat 正常
- [ ] 按鍵/編碼器喚醒可靠（連按 20 次都醒）
- [ ] 量測記錄：待機電流（外部電流表）、喚醒延遲

---

## 8. 風險 / 已知坑

| 坑 | 說明 | 對策 |
|---|---|---|
| soft reboot → hard reset | 本專案 LVGL 偵測到殘留會主動 hard reset，等於重開兩次 | 開發時用 `machine.reset()`；待機不要用 soft reset |
| lightsleep 期間 USB CDC 消失 | PC 工具會失聯（裝置本身沒重開） | 量測/日誌寫檔（`/bench_results.jsonl`），插回再 pull |
| WiFi `pm=0` ↔ 漏 ARP | 已知取捨：省電會漏幀 | `wifi_off=1` 時直接斷線，避免「半睡半醒」 |
| PSRAM 保持電流未知 | heap 在 PSRAM，light sleep 仍需保持 | 實測再定案；必要時評估 PSRAM 電源域 |
| 中斷/編碼器狀態 | 喚醒後中斷配置可能要重設 | resume 時重新 `irq()` 註冊 |
| 背光沒關就睡 | 省電失效（背光 20–60 mA） | 進入待機的第一步就要關 |

---

## 9. 未決問題

- [ ] 待機時要不要保留 WebREPL（除錯便利 vs 耗電）
- [ ] 按鍵語義：短按喚醒、長按關機？還是單一喚醒？
- [ ] 是否做 VBUS 偵測（需硬體）→ 決定「拔線不斷電」能否實現
- [ ] 與 `schedule` 任務的互動：待機時排程是否暫停、醒來是否補跑
- [ ] `restore` 清單要包含哪些（page / mode / brightness / audio position）

---

## 10. 參考：量測工具

| 工具 | 用途 |
|---|---|
| `test/boot/capture_boot.py` | 冷開機時間軸（`--trigger hard --via-repl`） |
| `test/boot/dev_run.py` | 透過 USB 執行測試（自動等開機、處理 USB 重新列舉、`--pull` 讀回檔案） |
| `test/boot/probe_sleep.py` | lightsleep 延遲、deepsleep 喚醒開銷 |
| `test/boot/bench_sleep_diff.py` | 證明「等待」與「休眠」的差別 |
| `test/boot/bench_import.py` / `bench_gc.py` / `bench_yield.py` | 開機成本、GC、讓步成本 |

用法與實測數據見 `test/boot/README.md`。

---

## 11. 休眠 task 的設計（提案，2026-10-24）

### 11.1 提案

把待機做成 TaskManager 的一個 task（名稱 `standby`，最低優先）：收到休眠訊號後，
依序**收斂其他 task → 關閉周邊 → 呼叫 `lightsleep()` → 醒來恢復**。

### 11.2 職責邊界

| 誰 | 負責 |
|---|---|
| `standby` task | 只做**協調**：收斂、排序、逾時、回滾、記錄醒來原因 |
| 各 task | 提供「我在迴圈邊界，可以停」的回報；不自己睡 |
| 各子系統 service | 提供 `suspend()/resume()`；standby 只按順序呼叫，不直接碰 SPI/GPIO |

### 11.3 收斂：**直接用 TaskManager 現有的 layer 機制，不要另發明協議**

`TaskManager` 已經有完整的「停任務 → 起任務」機制：

```
disable_layer(n) → _mark_dirty() → 下一輪 runner_loop 的 _update_tasks()
                → 對該層每個 task 呼叫 on_stop() → 從 active_list 移除
```

而且 `task.loop()` 是在 **runner_loop 這個執行緒裡輪流呼叫**的（協作式，不是每個 task 一條 thread），
所以 `on_stop()` **一定落在「兩次 loop 之間」**，不會打斷進行中的 SPI/I2C 傳輸。

→ 因此不需要握手協議，也不需要「等 task 回報 idle」；只要**等 `active_tasks` 清空**即可。

危險點仍然存在：**不能從別的 thread 突然呼叫 `lightsleep()`**（阻塞 I/O 會放 GIL，可能睡在別的 thread 的
SPI 傳輸中途）。所以休眠要由 **runner_loop 自己在 active_list 空掉之後**執行。

正確流程：

```
1. standby.enter()：disable_layer(2) → disable_layer(1) → disable_layer(0)
2. runner_loop 下一輪：_update_tasks() → 各 task.on_stop() → active_list 空
3. active_list 為空時 → standby_sleep()：關周邊 → 設喚醒源 → lightsleep()
4. 醒來 → standby_wake()：開周邊 → enable_layer(0..2) → 下一輪 on_start()
```

> 這是 §4.3 順序表的前置條件：**先讓 task 全停，再關周邊，最後才睡**。
> 若 standby 自己也是一個 task，記得把它註冊在**不會被 disable 的層**，否則它會把自己停掉。

### 11.4 喚醒源

| 喚醒源 | API | 適用 | 注意 |
|---|---|---|---|
| 單一 GPIO | `esp32.wake_on_ext0(pin, level)` | 按鍵、單一開關 | 電平要穩定（上拉/下拉） |
| 多個 GPIO | `esp32.wake_on_ext1((p1, p2), level)` | 編碼器 A/B、多按鍵 | 全部同電平觸發 |
| RTC 定時器 | `machine.lightsleep(ms)` | 定期醒來（心跳/重連/採樣） | — |
| **GPIO + 定時器同時** | 兩者都設 | **實務標準做法** | 誰先到誰叫醒 |
| 觸控 | `esp32.wake_on_touch(True)` | 有 touch pad 的板子 | S3 支援 |
| VBUS 插入 | 分壓進 GPIO | 插電自動開機 | 需硬體 |

**保險規則：永遠設一個定時喚醒上限。** 只靠 GPIO 時，若腳位電平設錯（例如按鍵卡住、上拉沒開），
裝置會永遠不醒，只能拔電。定時器是唯一的逃生門。

```python
import machine, esp32, time

btn = machine.Pin(1, machine.Pin.IN, machine.Pin.PULL_UP)
esp32.wake_on_ext0(btn, esp32.WAKEUP_ALL_LOW)     # 按鍵拉低 = 喚醒
machine.lightsleep(30 * 60 * 1000)                # 上限 30 分鐘（保險）
print("wake_reason:", machine.wake_reason())      # PIN_WAKE / RTC_WAKE
```

### 11.5 程式骨架

```python
# lib/sys/standby.py
class Standby:
    def __init__(self, bus, tm):
        self.bus, self.tm = bus, tm
        self.mode = None            # None | "app" | "light" | "deep"
        self.timeout_ms = 30 * 60 * 1000   # 定時喚醒上限（保險）

    def request(self, mode):
        """任何來源（按鍵/閒置計時/指令）都可呼叫；只登記，不動硬體。"""
        self.mode = mode
        self.tm.disable_layer(2)    # 先停高層（render/lvgl/UI）
        self.tm.disable_layer(1)
        self.tm.disable_layer(0)    # 最後停底層

    def maybe_sleep(self, active_empty):
        """由 runner_loop 在 active_list 空掉後呼叫（core0 限定）。"""
        if not self.mode or not active_empty:
            return
        mode, self.mode = self.mode, None
        self._suspend_devices(mode)          # 背光 → 面板 DISPOFF → 音訊 → (light) WiFi
        self._arm_wake()                     # ext0 + 定時器
        machine.lightsleep(self.timeout_ms)
        self._resume_devices()               # 反向恢復
        self.tm.enable_layer(0)              # 依序重啟（on_start 會重建硬體）
        self.tm.enable_layer(1)
        self.tm.enable_layer(2)
        log.info("wake_reason=%s pins=%s" % (machine.wake_reason(), machine.wake_pins()))
```

`runner_loop` 只加三行（`slave/lib/sys/task_manager.py`）：

```python
active_list = self._active_list[core_id]
if not active_list:
    if core_id == 0:
        _standby.maybe_sleep(True)      # ← 唯一新增的呼叫點
    time.sleep_ms(0)
    continue
```

### 11.6 注意事項

- 醒來後**重新註冊中斷**：編碼器/按鍵的 `irq()` 在睡眠期間不會跑到 Python，恢復時要重掛。
- 按鍵要 **debounce 20–50 ms**；編碼器喚醒後直接讀 A/B 現況，不要假設「沒動過」。
- `machine.wake_reason()` / `wake_pins()` 要寫進 log，除錯時才知道是誰叫醒的。
- 本專案**不要**用 deep sleep 做這件事：醒來 33 秒（實測），體驗比重新開機還差。
- lightsleep 期間 USB CDC 可能消失，除錯時日誌一律寫檔（`/bench_results.jsonl`）。

