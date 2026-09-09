# 15 — P4 (ESP32-P4 + 外接 C6) 音訊播放「加速」調查 + ESP-NOW 不可用

> **狀態**：✅ 調查完成（block 模式 1× 正常；irq 模式不可靠退回 block；ESP-NOW 此板無法使用）
> **日期**：2026-09-04
> **板子**：ESP32-P4-ETH_mp3，MicroPython v1.29.0 `ESP32_GENERIC_P4-C6_WIFI`
> **一句話**：block 模式播放經實測是精準 1×，**沒有加速**；「加速」是錯誤的計量
> proxy（把「塞進 DMA ring 的量」當「已播出量」）造成的假象。真正的大問題是
> **這顆 P4 的 MicroPython 固件沒編入 `_espnow`，ESP-NOW 根本無法啟動**。

---

## 1. 調查背景

用戶回報：啟動 ESP-NOW 後「發了指令對方沒收到」；且質疑是不是 `schedule` 霸佔
頻道 / MP3 播放造成。後續聚焦「播放會加速，一直讀一直讀」，期望是「填滿就停、
消耗了才補」。

## 2. 板子環境（實測）

- `import sys` → MicroPython 1.29.0，`ESP32_GENERIC_P4-C6_WIFI`
  （WiFi 是**外接 ESP32-C6**，走 hosted RPC）
- SD：`/sd` FAT，`/sd/audio/` 有 4 首 WAV + `playlist.json`
  （E_/ME_ 開頭，44.1k/16bit/stereo）
- config：`Audio.mode` 原為 `block`；`Network.wifi.enable=1`、`ESP_now.enable=1`
  （板上 config 與 repo 不完全一致，以板上為準）

## 3. 關鍵實測結果

### 3.1 `I2S.write()` block 行為 — 精準硬體節拍 1× ✅

獨立 probe（不經 app），塞 8KB silence ×6：

```
prime write 8192 bytes in 46 ms
write#0 -> 8192 bytes, took 47 ms
...每筆穩定 46-47 ms
```

→ block `write()` 每 8KB 阻塞 ~46ms = 44.1k/16bit/stereo 的真實 1× 節拍。

### 3.2 SD 讀取 — 極快、無天然節流

`fp.readinto(8KB)` 實測 **~1.4ms/筆**。producer（dj_task）讀檔不受硬體節流，
「煞車」只能靠 consumer（audio_player）的 block write。

### 3.3 block 播真實檔案 10 秒 = 10.03 秒 wall-clock = 1× ✅

獨立測試餵 `ME_44100_16_2.wav` 跳過前 5 秒、播 10 秒的量：

```
RESULT block: fed=1769472/1764000 in 10031 ms wall => 1.00x 1x OK
```

**→ block 模式完全不加速。**

### 3.4 I2S irq 觸發率 = 1× ✅（硬體層）

註冊 `i2s.irq(cb)` 後持續非阻塞塞 silence，3 秒窗 irq 觸發 **21.3/s**
（期望 21.7/s = 每 8KB 46ms）。硬體 DMA irq 節拍正常。

### 3.5 「加速」假象的來源（重要）

非阻塞 `I2S.write()` 回傳的是「**成功放進 DMA ring** 的 bytes」，**不是「已播出」
的量**。irq 模式下 producer 瞬間把 40KB DMA ring 灌滿，若用「write 回傳累加」
當播放進度，會得到 **0.27×「加速」** 的假象（實測），但硬體其實照 1× 在播。
同理，dj_task 的 `_pos_ms` 用 `bytes_played`（SD 讀取量）算，**不是**真正進 I2S
的量 → 讀得比播得快時，pos 會「超前」→ 看起來像快轉。

> **教訓**：判斷播放走速只能看「硬體消費的證據」（block write 的 wall-clock，
> 或 irq 觸發率 × 每格真實時長），不能看「producer 塞了多少」。

### 3.6 irq 非阻塞路徑在 P4 有 EINVAL（不穩定）

最小 irq callback 補料測試最後丟 `OSError: [Errno 22] EINVAL`。各種 size 的
普通 `write()` 都正常，但 **callback 內非阻塞續寫 / ring 滿狀態下的行為**會
EINVAL。→ **P4 這版固件的 irq 非阻塞餵法不可靠，決策退回 block。**

### 3.7 ESP-NOW：此板固件缺 `_espnow`，無法啟動 ❌

```
import espnow  →  ImportError: no module named '_espnow'
```

boot log 亦見 `ESP-NOW init error: no module named '_espnow'`。build 為
`ESP32_GENERIC_P4-C6_WIFI`，外接 C6 的 hosted ESP-NOW 沒有對應到 MicroPython
`_espnow`。**這是固件層級**，非程式問題。

---

## 4. 結論

1. **「播放加速」不是真的**。block 模式實測精準 1×；先前誤判來自用錯計量
   proxy（`fed`/`bytes_played` = 塞入量 ≠ 播出量）。若 user 真的「聽起來快」，
   需再確認聽的當下是不是 block 模式、以及是否 mode 3/sfx 路徑疊了多個 program。
2. **irq 非阻塞餵法**在 P4 這版固件有 EINVAL / 語意不清，**退回 `Audio.mode="block"`**。
   目前板上 config 已切回 block，app 穩定播放。
3. **ESP-NOW 此板不可用**（缺 `_espnow`）。若需 ESP-NOW：
   - 換支援 ESP-NOW 的板/固件（如 ESP32-S3，內建 WiFi）；
   - 或改用 WiFi TCP/WebSocket / UART 做控制通道。

## 5. 程式碼變更記錄

- `ports/P4/ESP32-P4-ETH_mp3/tasks/audio_player_task.py`：本次加入 irq 路徑的
  重入防護 `_feeding`、ring 滿保留 `_pend=(view, off)` 不丟槽、`_silencing`
  去重。**但因為 §3.6 決定退回 block，這些 irq 變更目前是休眠路徑**
  （config `Audio.mode="block"` 時不執行）。block 路徑保持原樣。
- `config.json`：曾切 `Audio.mode="irq"`，**已切回 `"block"`**；曾加
  `Buffer.now_rx_slots=4`（無害，保留）。

## 6. 復現 / 驗證 SOP

板子 USB port 每次插入可能不同，用 `ls /dev/cu.usbmodem*` 找。

1. app 跑著時 raw REPL 進不去 → 先 Ctrl-C（首次若 WDT 開會 auto-disable+重啟一次，
   之後測試模式無狗，再 Ctrl-C 即停到 REPL）。
2. `python3 -B -m mpremote connect <port> resume exec "..."`（resume 不 soft-reset）。
3. block 1× 驗證：`/tmp/p4_block_play_test.py`（repo 外）播 10 秒 → wall ~10s。
4. irq 觸發率：`/tmp/p4_irq_rate.py` → ~21.3/s。
5. ESP-NOW：`import espnow` → 若 ImportError `_espnow` 即此板固件不支援。
