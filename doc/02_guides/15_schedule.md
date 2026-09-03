# 定時指令排程（schedule 任務）

> **用途**：開機時**自行尋找排程檔**（預設 `/schedule.json`，無 config 開關），
> 找到就依時間軸把 NC4 指令**寫進 vBus**（內部虛擬總線），走
> 收指令 → 解碼 → 分派 → 執行 的內部鏈路（可當指令模擬器，或未來通用定時任務）。
> **位置**：`slave/tasks/schedule.py`（Core_Manager 常駐註冊；檔案無項目時 idle）

---

## 1. 為什麼只寫 vBus

- 實體總線（uart1/uart2）的 rx_hub 是 SPSC：`CircuitTask.poll()` 與
  `BusDecodeTask` 各自不斷寫入/讀走，外部再塞幀既不是「寫入讀取緩衝」的語義，
  又會與輪詢競爭 → 不可行。
- **vBus** = schedule 自己建立的內部虛擬總線（`CircuitBus(io=None)`，不碰任何
  腳位），註冊進 `bus_sources`，由 `BusDecodeTask` 當一般來源消費。
  唯一寫入者是 schedule → 乾淨、無競爭，等同「master 從線上交了一幀進來」。

### vBus 注入的實際寫法（schedule.py）

```python
# 1) 建立虛擬總線（io=None → 不接任何實體腳位），註冊進解碼來源
self._vbus = CircuitBus(None, label="VBUS")
sources = bus.get_service("bus_sources")   # 沒有就建一個 BusSources()
sources.add(self._vbus)                    # BusDecodeTask 每 ~100ms 刷新來源會看到它

# 2) 注入 = 把完整 NC4 訊框寫進它的 rx_hub（與實體線收到的格式一模一樣）
def _inject(self, cb, frame):
    hub = cb.rx_hub
    view = hub.get_write_view()            # 拿一個空 slot（原子；唯一寫入者 = schedule）
    struct.pack_into("<H", view, 0, n)     # slot 前 2 bytes = 資料長度（u16 LE）
    view[2:2 + n] = frame                  # 訊框 bytes（"NC\x04" + addr/cmd/len + payload + CRC32）
    hub.commit()                           # 標成 READY → 解碼端可取
```

### 注入之後（與實體收到的位元組走同一條鏈）

```
BusDecodeTask.loop()
  └─ bus_sources → 每個來源有 rx_hub
       ├─ hub.get_read_view() → 2-byte 長度 + data
       └─ app.handle_stream(parser, data, label="VBUS", ...)
            └─ StreamParser.pop_frame() → Dispatcher.dispatch(cmd, payload)
                 └─ action（0x3105 → gmode.set_mode → bus.shared["mode_id"] 共用狀態）
                      └─ PixelTask / MP3 等消費方跟狀態執行
```

log 上 `(VBUS)` label 與 `🔹 [VBUS] STATUS_GET (0x1101)` 就是這條路徑的證據。

## 2. 排程檔格式（/schedule.json）

首次啟動若檔案不存在 → 自動產生**空範本**（不會誤發任何指令），填好後重開機即生效。

```json
{
  "repeat": 0,
  "schedule": [
    { "addr": "0xFFFF",
      "ms":   1000,
      "bus":  "vBus",
      "cmds": {"cmd": "0x3105", "payload": "00 05 00 00 FF"}
    }
  ]
}
```

| 欄位 | 說明 |
|---|---|
| `repeat` | 0 = 播完一次；-1 = 無限循環；N = 循環 N 次（可選） |
| `schedule[]` | 每筆 = 一個發射時機 |
| 筆內 `ms` | 由任務啟動起算第幾 ms 發送 |
| 筆內 `addr` | 目標位址（0xFFFF = 廣播，可選） |
| 筆內 `bus` | `vBus`（預設）= 注入給自己（走內部解碼鏈）；`circuit:<i>` / `net:<i>` = 從 circuit bus 列表 / net bus 列表選第 i 項，用該物件的 `write()` 發出去（不需知道實體是 uart 還是網路） |
| 筆內 `cmds` | 單個 `{cmd,payload}`（自動打包 NC4 含 CRC32）、多筆清單、或純 hex 字串 = raw 完整訊框 |

`cmd`/`payload`/`addr` 都支援 `0x` 前綴與空格分隔的 hex；payload 欄位順序依
`slave/schema/*.json`。常用例：

- `0x3105 MODE_SET` payload = `mode_type(u8) mode_id(u8) start_delay_ms(u16 LE) brightness(u8)`
  - 馬達正弦波 `motor_sine`（id2，type=0）：`"00 02 00 00 FF"`
- `0x3106 MODE_STOP` payload = `"01"`（全關閉）
- `0x1101 STATUS_GET` payload = `"01"`

> 內部模式 id = `(mode_type<<8)|mode_id`；type=0 就是 `/pixel/modes/*.json` 的 id。

## 3. 模式狀態機（指令只改狀態，播放器跟狀態）

指令（MODE_SET / MODE_STOP）只寫**共用狀態**（`bus.shared` 的
`mode_id` / `mode_seq` / `mode_start_at` —— key 不綁 pixel，MP3/audio 等模組共用），
PixelTask 只是其中一個消費方：

| 情況 | 行為 |
|---|---|
| `mode_seq` 沒變 | 現在播什麼就繼續播（循環由模式自身參數控制） |
| 不同 mode | 立即丟棄現有 player 切換 |
| 相同 mode 再收到 | 由頭 restart |
| `mode_id=0`（MODE_STOP） | 停、填中性值；不回 auto |
| 開機無指令 | 若 `registry.json` 的 `auto_play=true` 播清單**一次**；基礎框架預設 `false`（不自動播放） |

基礎框架（`slave/` 預設）不自動播放，只留一個馬達正弦波效果
`uart_motor_sine`（`/pixel/modes/motor_sine.json`，id2）+ 一支範本排程。

## 4. 觀察是否執行成功

- 送出端：`[Schedule] item#N +xxxxms vBus -> cmd=0xXXXX payload=NB (VBUS)`
  + 追加到 `/schedule_trace.log`（USB log 串流不可靠時以檔案為準）。
- 執行端（action 的 print/log）：`🔹 [circuit] MODE_SET (0x3105)` →
  `[Pixel] MODE_SET type=0 id=5 ...` → `▶ remote play mode 5`。
- 實體效果：馬達/燈照模式動作。

## 5. 調整流程

1. 編輯 `/schedule.json`（PC 端：`python -m mpremote connect COMx fs cp local.json :/schedule.json`）。
2. 重開機 → 排程從 boot 重新計時執行（不需 config 開關、不需改程式）。
3. `repeat: -1` 無限循環。

## 相關

- `slave/pixel/effects/effects.py` — 馬達正弦波效果 `uart_motor_sine`（你的設定方法：program 段序列 `uart_motor_sine/uart_motor_stop` + direction/speed_percent/speed_curve/end_Time + cycles/hold_raw；**輸出走 `write:"w"`**：全像素同值 = raw<<4，raw 0x00(全速收)/0x80(停)/0xFF(全速伸) 都是有效命令）
- `slave/lib/hw/uart_motor.py` — `st_load_and_convert` 原樣收 W 通道 raw byte（0 不改寫成停）；停止/熄燈由 render `clear_all()/stop_motors()` 明確填 neutral(0x80) 再推幀
- `slave/lib/sys/global_mode.py` — 共用模式狀態寫入口（mode_id/mode_seq/mode_start_at）
- `slave/tasks/pixel_task.py` — 狀態機消費方（開機 auto 單 pass、同 ID restart、異 ID 即切）
- `doc/01_protocol/01_nc4_protocol.md` — NC4 封包/CRC
- `doc/01_protocol/02_command_index.md` + `slave/schema/*.json` — 指令與 payload
- `slave/tasks/schedule.py` — 實作（vBus = CircuitBus(io=None)，不新增 lib）
