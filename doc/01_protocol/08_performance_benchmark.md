# 網路 + 協議性能測試總結

> **用途**：記錄 ESP32-P4 / ESP32-S3 的網路吞吐與 NC4 協議解碼性能基準，以及本回合做的性能優化與結論。接手同事讀這份即可了解「現狀、瓶頸、甜蜜點、已做改動、如何復現驗證」，**不需要重跑測試**。
> **分類**：協議層（01_protocol）
> **最後更新**：2026-08-19
> **基準**：所有數字以本文件為準；細節定義見 `01_nc4_protocol.md`（協議格式）、`03_notes/02_buffer_architecture.md`（緩衝架構）。

---

## 0. 一分鐘結論

| 項目 | 結論 |
|---|---|
| **發送（上載）甜蜜點** | **4 KB**，這是 lwIP `TCP_SND_BUF`（約 4~5.7 KB）的**硬約束**，超過會懸崖式下跌，不是選出來的 |
| **接收（解碼）甜蜜點** | **8 KB**，這是「每幀固定開銷攤薄」的最優點，也是 `MAX_PAYLOAD` 的約定上限 |
| **兩台設備定位** | P4 靠乙太網（上載 9.35 / 下載 5.72 MB/s）；S3 靠 WiFi（上下載 ~0.8 MB/s，網路是絕對瓶頸） |
| **生產解碼能力** | P4 ≈ 3.13 MB/s @ 8K；S3 ≈ 2.58 MB/s @ 8K（單線程 `StreamParser`） |
| **解碼 C 層天花板** | ≈ 7.3 MB/s（被 `bytes()` 複製 + CRC32 的 C 層速率卡死） |
| **端到端瓶頸** | `min(網路, 解碼)`；S3 卡網路 0.8，P4 卡解碼 3.13 |
| **資料安全性** | 全程 CRC32 驗證完好、payload 回 `bytes` 副本（可跨 feed 持有）、壞幀靜默丟棄不 yield |

> 📌 **後續性能更新**：`update_changelog.md` 記錄了 `pop_frame` 零拷貝優化（純解碼 8K 從 1.11 → 4.00 MB/s），本文件為前次基準，兩者合看。

---

## 1. 測試環境

| | P4 | S3 |
|---|---|---|
| 傳輸介質 | RMII 乙太網（IP101 PHY） | WiFi STA |
| CPU | 雙核 | 雙核（240 MHz） |
| 記憶體 | GC 30MB / DRAM 443KB / PSRAM 29MB | GC 8MB / DRAM 326KB / PSRAM 8MB |
| PC 端工具 | `tools/PC/net_bench_pc.py`（TCP server + UDP beacon） | 同左 |

> 註：裝置與 PC 的 IP 是現場環境值，測試時依實際網路配置而定，這裡不列。

**關鍵架構事實**：接收與解碼**都在 core0**（`network` 與 `bus_decode` affinity 皆 `(1,0)`），同核串行。`bus_decode` 之所以獨立成 task，是為了讓**所有總線**（NetBus / CircuitBus / NowBus…）透過 `BusSources` 註冊表統一匯流到同一個 `StreamParser` 解碼。core1 是 pixel / hw_sample 等主力顯示任務。

---

## 2. 完整測試數據

### 2.1 P4（乙太網）

| 路徑 | 2K | 4K | 8K | 16K | 32K | 峰值 |
|---|---|---|---|---|---|---|
| 上載裸 TCP（分段 ≤4K） | 8.99 | **9.35** | 7.65 | 4.75 | 4.09 | **9.35 @ 4K** |
| 上載裸 TCP（基線，不分段） | 8.95 | 9.22 | 2.17 | 1.69 | 1.48 | 9.22 @ 4K（8K 起懸崖） |
| 下載裸 TCP | 3.22 | 3.22 | 3.25 | 5.13 | **5.72** | **5.72 @ 32K** |
| 上載 NC4 慢版 pack | — | — | — | — | — | 3.84 @ 32K |
| 上載 NC4 快版 pack | 5.68 | 6.48 | 7.02 | **7.37** | 7.33 | **7.37 @ 16K** |
| **生產解碼 `bench_decode`** | 1.33 | 2.23 | **3.13** | (8.55 噪聲作廢) | — | **3.13 @ 8K** |

### 2.2 S3（WiFi）

| 路徑 | 2K | 4K | 8K | 16K | 32K | 峰值 |
|---|---|---|---|---|---|---|
| 上載裸 TCP | 0.74 | 0.81 | **0.83** | 0.82 | 0.81 | **0.83 @ 8K** |
| 下載裸 TCP | 0.67 | 0.74 | 0.79 | 0.78 | **0.80** | **0.80 @ 32K** |
| 上載 NC4 慢版 pack | — | — | — | — | — | 0.50 @ 32K |
| 上載 NC4 快版 pack | 0.65 | 0.71 | 0.72 | **0.74** | 0.73 | **0.74 @ 16K** |
| **生產解碼 `bench_decode`** | — | — | **2.58** | (3.19 噪聲) | — | **2.58 @ 8K** |

> 單位皆為 MB/s。`bench_decode` 的 16K 數字（P4=8.55、S3=3.19）**不可信**：16K 只有 64 幀 + 取最佳值，受 GC/調度抖動污染，反推每幀耗時出現物理矛盾（16K 比 8K 快 2 倍以上）。真實 16K 能力見 §3.2。

---

## 3. 性能結論

### 3.1 兩個「甜蜜點」分屬不同環節，不衝突

| 環節 | 瓶頸 | 甜蜜點 | 約束性質 |
|---|---|---|---|
| 發送端（上載） | lwIP `TCP_SND_BUF` ≈ 4~5.7KB，單次 send 超過就阻塞等 ACK | **4 KB** | 硬約束，不能超 |
| 接收/解碼端 | 每幀固定開銷（find SOF + header + payload 副本） | **8 KB** | 攤薄最優 |

- 發送側 4K 是「物理上限」，不是「選出來的甜蜜點」。
- 接收側 8K 是「固定開銷攤薄」的最優，且 8K 同時滿足「不超 lwIP 發送約束 + 你的 `MAX_PAYLOAD` 約定」。
- **三方一致：8K 定案正確，不需要再測更大的。**

### 3.2 16K 的能力邊界（推測，非實測）

用「每幀耗時 = Python 固定開銷 F + C 層線性數據成本」模型外推：

| chunk | 預測每幀 | 預測吞吐 |
|---|---|---|
| 8K | 2.5 ms | 3.13 MB/s（實測 ✓） |
| 16K | 3.6 ms | ≈ 4.8 MB/s |
| 32K | 5.8 ms | ≈ 5.8 MB/s |
| ∞ | — | → 7.3 MB/s（C 層天花板） |

**結論**：16K 解碼能力約 4.8 MB/s（P4，+50%），但**真實負載用不到**——生產檔案傳輸只發 1~2K 幀（`download_chunk_size=2048`、`upload_chunk_size=1024`），控制指令 <1K。只有未來「大幀直播/流」場景才需要 16K。

### 3.3 端到端瓶頸 = `min(網路, 解碼)`

| | 網路上限 | 解碼能力 | 端到端 |
|---|---|---|---|
| P4 | 下載 5.72 | 3.13 @ 8K | **3.13（解碼卡）** |
| S3 | 0.8 | 2.58 @ 8K | **0.8（網路卡）** |

---

## 4. 本回合代碼改動清單

### 4.1 生產熱路徑（已接入本體，非死代碼）

| 檔案 | 改動 | 目的 |
|---|---|---|
| `slave/lib/sys/proto.py` | CRC 用 `memoryview` 切片算（省 bytes 複製）；`feed`/`compact` 改 memoryview slice 賦值（memmove，替代 viper 逐 byte）；新增 `MAX_PAYLOAD` / `RX_BUF_SIZE` / `SEND_CAP` 三常量唯一真相源 | 拆幀零多餘複製 + 大小約定單點管理 |
| `slave/tasks/bus_decode.py` | 消費 rx_hub 改用 view 模式（`get_read_view`/`release_read`），取代 `read_into` copy 模式 | 消掉 slot→_read_buf 一層複製 |
| `slave/lib/sys/net_bus.py` | 發送 `_send_all` 加 `SEND_CAP` 分段；接收 buffer 改硬編碼 `RX_BUF_SIZE`（不再讀 config `size`） | 固化 4K 發送硬約束 + 接收 buffer |
| `slave/lib/sys/circuit_bus.py` | 接收 buffer 改硬編碼 `RX_BUF_SIZE`（不再讀 config `size`） | 與 net_bus 對齊 |
| `slave/app.py` / `slave/tasks/web_ui.py` | `StreamParser(max_len=MAX_PAYLOAD)` 收斂，不再各自寫死數字 | 單點管理 |
| `slave/driver/network_drv.py` | `init_network` 建立後立即 `init_from_config()` | 修復 boot 不連網的斷點 |
| `slave/lib/sys/network_manager.py` | 關 WiFi 省電、LAN DHCP 等待、已連指定 SSID 就沿用不重連 | 根治 ETIMEDOUT / 省電延遲 |
| 全部 `config.json` | 刪除 `Buffer.size` 死鍵（已由 `RX_BUF_SIZE` 硬編碼取代） | 移除失效設定 |
| `slave/config.json` + `ports/P4/General/config.json` | 修 JSON 尾逗號 | 修復 `json.loads` 整檔失敗 |
| `tools/PC/net_bench_pc.py` | 修正 `lib.proto` import 路徑（`../../slave`） | 修復 PC 端協議模式崩潰 |

### 4.2 測試專用（**未接入生產，不要誤用**）

| 檔案 | 說明 |
|---|---|
| `test/protocol/bench_net.py` | 網路極限測試工具。**framed 直讀**（`download_burst_proto`）是測試專用快速實現：payload 回 memoryview（零拷貝但不可跨 feed 持有）、沒做 addr 過濾。**它的「協議下載 3.72 MB/s」不代表生產能跑到，作廢。** |

---

## 5. 測試腳本（接手同事可用）

### 5.1 正確性驗證 `test/protocol/test_proto_hotpath.py`

驗證 `StreamParser`（feed slice 賦值 + compact）與 buffer_hub view 模式的**正確性**。自產自解，不需網路。

```python
exec(open("test/protocol/test_proto_hotpath.py").read())
run_all()
```

10 個用例：單幀 / 黏包 / 半包 / 隨機分段 / 垃圾重同步 / compact / 最大負載 8K / CRC 壞幀丟棄 / payload 生命週期 / hub view 模式。

**結果：10/10 PASS**（兩台設備實跑確認）。

### 5.2 性能驗證 `test/protocol/test_proto_speed.py`

- `bench_decode()` — 單線程純解碼（生產 `StreamParser` 的 CPU 上限），**這是權威解碼數字**。
- `bench_pipe()` — 雙線程生產/解碼管道，**只用來看「完整性無遺漏」，吞吐因 MicroPython GIL 串行是參考值，不要當真**。

```python
exec(open("test/protocol/test_proto_speed.py").read())
bench_decode()      # 2K/4K/8K 各跑一次
bench_pipe()        # 驗證不丟數據
```

### 5.3 已知的測速陷阱（接手前必讀）

1. **soft reboot 後第一跑偏慢**：冷啟動（模組快取 / 記憶體池未熱）。以第二、三跑為準。
2. **2K 在 S3 上很吵**：後台 pixel/WiFi 任務搶 CPU，短時間測時被污染。8K 是最穩定的可複現數字。
3. **16K 數字不可信**：幀數太少（64 幀）+ 取最佳值，會出假高值。要乾淨測 16K 需改腳本（暖機 + 中位數 + 幀數放大）。
4. **`bench_pipe` 的吞吐不作數**：GIL 串行 + 計時含 producer 預熱，只信「完整性 ✅」。

---

## 6. 未完成 / 邊界事項

| 事項 | 狀態 | 說明 |
|---|---|---|
| addr 過濾 | 未做（舊版）→ 已由 `bus.cid` 過濾補上（見 `01_nc4_protocol.md §5`） | 定址模型落地後 RX 端可逐 address 掃描 |
| seq 連續性檢查 | 未做 | CRC 已保證完整性，但沒顯式驗「幀序號連續」來偵測「整幀漏掉」的邊緣 |
| framed 直讀生產化 | 未做 | 測試專用的 framed 直讀未接入生產；生產仍走 `StreamParser`（安全、有黏包處理） |
| 16K 精確數字 | 未乾淨實測 | 需改 `bench_decode` 才能可信量測；但真實負載用不到，暫不急 |

---

## 7. 如何修改大小上限（唯一真相源 `lib/sys/proto.py`）

三個大小約定全部收斂在 **`slave/lib/sys/proto.py` 開頭**，各司其職、彼此正交：

```python
MAX_PAYLOAD = 8192   # 幀負載上限（純 payload, 不含 header/CRC）
RX_BUF_SIZE = 4115   # 接收端每次收多少（net_bus + circuit_bus 共用）
                     # = HDR 9 + file_id 2 + offset 4 + data 4096 + CRC 4
                     #   → 剛好裝得下一個滿載 FILE_CHUNK 幀
SEND_CAP    = 4096   # socket 每次 send 的分段上限（lwIP 硬約束）
```

| 常量 | 含義 | 誰在用 | 改動影響 |
|---|---|---|---|
| `MAX_PAYLOAD` | 單幀 payload 上限 | `StreamParser`（內部自動 +13 建緩衝） | 放寬/收緊都只改這一行 |
| `RX_BUF_SIZE` | 每次 recv/readinto 收多少 | `net_bus.py`、`circuit_bus.py` | 同上 |
| `SEND_CAP` | 每次 send 分段大小 | `net_bus.py` 的 `_send_all` | 同上 |

> ⚠️ 這三個是「工程約定」不是協議硬限制。`MAX_PAYLOAD` 的 `LEN` 欄位是 uint16 理論可到 64KB；定 8K 的理由（對端 ESP、無線長幀不可靠、現有指令 ≤2K）見 `01_nc4_protocol.md §1`。

### Buffer 區塊的現況（`config.json`）

**`Buffer.size` 已刪除**——它曾是「接收 buffer 大小」的舊來源，現已被 `RX_BUF_SIZE`
（**4115**，＝一個滿載 FILE_CHUNK 幀：9+2+4+4096+4）硬編碼取代，改了沒用，故移除。
（舊版本文件寫「4K」，那個大小**裝不下**一個滿載 FILE_CHUNK 幀，正是這個常數要對齊的目標。）

`Buffer` 區塊現在**只剩運行時吞吐旋鈕**（這些跟協議大小無關，仍走 config）：

| 鍵 | 含義 | 預設 |
|---|---|---|
| `drop_on_full` | 接收環滿了丟不丟 | 0 |
| `drain_reads` | 每次 poll 讀幾段 | 1 |
| `send_retry` | 發送重試次數 | 64 |
| `net_rx_slots` | net 接收環 slot 數 | 2（上限 4） |
| `u8_rx_slots` | circuit UART 接收環 slot 數 | 2（上限 **16**；`slave/config.json` 實際設 8） |
| `rx_hub_buffers` | 各 bus rx_hub slot 數 | 16 |
| `fb_mode` | framebuffer 模式 | `"auto"` |
| `decode_budget_slots` | 單輪解碼預算 | 32 |

> 要調吞吐，動的是這些旋鈕；要改大小上限，只動 `proto.py` 那三個常量。兩者別混淆。

## 相關文件

- `01_nc4_protocol.md` — NC4 封包格式（MAX_PAYLOAD / 大小約定）
- `03_notes/02_buffer_architecture.md` — 多級緩衝架構
- `03_notes/01_changelog.md` — 更新紀錄（`pop_frame` 零拷貝優化等）
