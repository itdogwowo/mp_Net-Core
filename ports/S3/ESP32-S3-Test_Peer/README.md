# ESP32-S3-Test_Peer — 純協定對端（零硬體）

## 一句話
**一台「只會講協定」的假執行端**：拿來驗證遙控器（`ESP32-S3-Control_Panel_V2`）
的**發現 → 綁定 → 查詢**整條鏈，不需要真的接燈、接 SD、接 TFT。

## 為什麼要有這個 port
遙控器頁（`slave/ui/lvgl/page/remote.py`）要能：
① 看到節點清單 ② 綁定方向 ③ 取得模式清單/細節。

這三件事全都要**兩個節點**才測得出來。用真板測的代價：
- 要湊兩台板子、兩條 USB、兩個 REPL
- 對端要接 WS2812 / SD（`/pixel/modes/*.json`）才有模式池
- 對端一改壞，還要先修對端才能繼續測面板 —— 測試與被測互相糾纏

本 port 把對端變成**可拋棄的**：沒有硬體、模式池寫死在原始碼裡、
改壞了直接重上傳，面板一行都不用動。

## 它「有」什麼
| 能力 | 來源 |
|---|---|
| ESP-NOW 射頻 | `Network.ESP_now = {enable:1, channel:6}`（**必須與面板同頻**） |
| 身份 `cid=0x0002` | `System.cID`（與面板的 `0001` 配對；**不可留空**，留空會被自動填成 MAC 末 4 碼） |
| 假模式池 3 筆 | `Core_Manager.py` 的 `FAKE_MODES` → `bus.shared["pixel_maps"]` |
| 開機 + 每 10 秒公告 | `AnnounceTask` → 廣播 `0x1002 SLAVE_ANNOUNCE` |
| 回應查詢 | `net_actions` / `pixel_actions` 的既有 handler（隨 `app.py` 全量註冊） |

## 它「沒有」什麼（刻意的）
`Core_Manager.py` 只註冊 5 個任務：

```
layer 0  network  now  log
layer 1  announce                       ← 本 port 唯一新增的任務
layer 2  bus_decode
```

**不註冊**：`pixel` / `render` / `stream` / `dj` / `audio_player`（沒硬體）、
`lvgl`（沒 TFT）、`web_ui` / `cpanel` / `pixel_cpanel` / `schedule`（面板角色的事）。
理由：省開機時間、少 log 噪音，而且「執行端」本來就不該有面板的行為。

硬體段全部 `enable:0` → `boot.py` 不會建出 `st_pixel`，
所以 `bus.shared["pixel_maps"]` 不會被 `PixelTask` 覆蓋，假模式池能活著。

## 兩個必須知道的設定紅線
1. **`System.watchdog` 必須 `enable:0` 且 `auto_rearm_ms:0`。**
   `auto_rearm_ms > 0` 是「測試模式自動回安全態」——開機後連續 N ms 沒收到指令
   就自動存 `enable=1` + `machine.reset()`。對端是**沒人對它發指令**的裝置
   （它只往外公告），沉默是常態 → 留著會自己重啟，而且重啟後 WDT 是開的，
   之後每次中斷都變成 8 秒硬重置。要保護就直接 `enable:1`，不要靠 auto_rearm。
2. **ESP-NOW 頻道必須與面板一致（本 port 與面板 V2 都是 6）。**
   不同頻道 = 完全收不到，而且兩邊都不會報錯。

## 上傳
```
1) slave/ 全量                     ← 基礎（與其他 port 相同）
2) 本 port 的 config.json + Core_Manager.py   ← delta 覆蓋
3) RESET
```

> ⚠️ `Router.enable=0`，但**開機時 Router 仍會把自動補齊的路由表寫回
> `config.json`**（`bus_decode.finalize_router` → autofill + 寫檔）。
> 所以第一輪開機後 `Router.routes` 會從 `[]` 變成 5 條。這是預期行為
> （「所有通道都會被註冊，沒註冊的補 `self`」），不是設定被弄壞。
> 那些 route 在 `enable=0` 時完全不作用。

## 預期 log（REPL）
```
[INFO] 🧪 [CoreManager] 假模式池: 3 個 ['測試A', '測試B', '可動']
[INFO] ESP-NOW: standalone mode, ch=6
[INFO] ESP-NOW ready, ch=6
[IMMEDIATE] [Peer] SLAVE_ANNOUNCE 廣播 (3 modes) ret=True
🔹 [NOW-Bus] IDENTIFY_REQ (0x100D)          ← 面板來敲門了
[INFO] [Peers] ＋ AABBCCDDEEFF cid=None mac=AABBCCDDEEFF via=frame iface=NOW-Bus
```

## 面板那邊應該看到什麼
1. 節點清單出現這台（`via=frame`：**被動**學到的，只先有 MAC）
2. 按「掃描」→ 本機回 `0x100E` → 清單那筆補上 `cid=0002`（`via=identify_rsp`）
3. 按「綁定」→ 面板送 `0x1016 SET_MASTER`，本機 `bus.master_cid` 記住面板
4. 按「取得清單」→ 本機回 `0x3102` → 3 筆（`0001 0002 0200`）
5. 按「取得細節」→ 本機逐筆回 `0x3108` → 名稱 `測試A / 測試B / 可動`

## 離線驗證（不用板子）
```bash
python -B /tmp/nodetest/peer_smoke.py       # 開機 → 廣播合法的 0x1002（欄位逐項比對）
python -B /tmp/nodetest/peer_roundtrip.py   # 收到 0x100D → 學會面板 MAC → 單播回 0x100E
```
兩支都是 CPython + shim；實作於 `slave/`，不佔用板上資源。

## 已知缺口（不是本 port 的問題）
- **真實執行端 `ports/S3/ESP32-S3-1_18` 的 `Network.ESP_now.enable = 0`**
  → 那台板子現在**完全不收 ESP-NOW**。要它回應遙控器，先把這個改 1。
- **`0x1002 SLAVE_ANNOUNCE` 原本沒有任何發送端** → ESP-NOW 上「被發現」的
  唯一途徑（MAC 無法列舉）是空的。本 port 的 `AnnounceTask` 是第一個發送端；
  真實執行端要不要也公告，是**產品決策**（公告＝任何人都看得到你）。
- **ESP-NOW 沒有「關」指令**（只有 `0x1301 NOW_INIT`）→ 面板的 ESP-NOW 開關
  目前只能開不能關。見 `doc/03_notes/19_remote_control_plan.md` §11。
