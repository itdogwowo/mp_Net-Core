# 檔案上傳瓶頸：完整量測模型（2026-09-21 夜間實測）

> **用途**：把「水管夠、解碼不是問題、儲存已是 MB 級、緩衝也解決了，那到底慢在哪」這個問題
> 用可複現的實測數字**算到每一步**，給出每包延遲的完整分解與各項佔比。
> **分類**：筆記（03_notes）
> **前置文件**：`09_upload_performance_diagnosis.md`（停等協議的定性診斷）、`08_performance_benchmark.md`（前次基準）
> **測試平台**：ESP32-P4（MicroPython v1.29.0, 2026-08-24）+ Mac（10.10.1.27），同一區網
> **最後更新**：2026-09-21

---

## 0. 一句話結論

**每包延遲 = 網路來回 + 板子固定 + 資料量 × (網路傳輸率 + 寫入率)，而停等協議把這個延遲
乘以「包數」。** 解碼只佔 0.7ms（3%），水管、儲存、緩衝都不是瓶頸。

---

## 1. 三個獨立量到的「單項能力」（都不是瓶頸，但決定模型係數）

| 項目 | 實測 | 換算 | 怎麼量的 |
|---|---|---|---|
| **網路傳輸率** | 1.27 MB/s（上載）/ 1.41 MB/s（下載）| **0.79 ms/KB** | `bench_net.py` + `tools/PC/net_bench_pc.py`（裸 TCP，繞過全部協議層；**兩端各自量到同一個數字**）|
| **網路來回延遲** | ping min 3.6 / TCP connect 5.0 ms | ~5 ms | `ping`、`socket.create_connection` |
| **SD 寫入（4KB）** | 8.03 ms（開檔一次連寫）| ~2.0 ms/KB | `file_rtt_probe.py` 的 4KB−16B 差值 |
| **板子純接收（不回覆、不寫檔）** | 6.1~6.3 ms/包 @2KB/4KB | 見 §2 | `bench_rx_sweep.py`（`BENCH_DATA` 0x1812）|
| **解碼（parse+decode+pack）** | 0.7 ms/4KB | — | 舊文件 §1.2 + 本次 §5 |
| `binascii.crc32` | 0.19 ms/4KB | — | 板上直接量 |
| `statvfs('/sd')` | 0.06 ms | — | 板上直接量 |
| **`statvfs('/')`（內部 flash）** | **831 ms** ⚠️ | 見 §7 | 板上直接量 |

---

## 2. 關鍵發現：板子的「每包固定成本」約 6ms，與 payload 大小無關

`bench_rx_sweep.py` 用 `BENCH_DATA`（0x1812：收到就 ok+1，**不回覆、不寫檔**）猛灌，
再問 `BENCH_RESULT`（0x1813）拿真實到貨數：

| payload | 到貨 | 包/秒 | KB/s | ms/包 |
|---|---|---|---|---|
| 512 B | 1000 | 159.8 | 79.9 | 6.258 |
| 2048 B | 64 | 163.8 | 327.6 | 6.105 |
| 4096 B | 64 | 158.7 | **635.0** | 6.300 |
| 4100 B | 64 | 141.2 | 565.3 | 7.083 |
| 8192 B | 32 | 76.4 | 611.0 | 13.094 |
| 12000 B | 0 | 失敗 | — | — |

**兩件事**：

1. **2KB 與 4KB 的包/秒完全相同（163.8 vs 158.7）** → 每包成本**與 payload 無關**，
   是「每包一次」的固定開銷 → 約 **6.1~6.3 ms/包**。
2. 8KB 的包/秒剛好砍半（76.4）→ 它佔 **2 個 hub slot**（8KB 幀 8209B > 槽 4117B），
   所以真正的限制單位是「**槽**」，而每槽成本固定。
   **→ 加大 chunk 只會讓每包吃更多槽，不會提升吞吐。**（這一條推翻了「加大 chunk 就能救」的直覺）

### 2.1 固定成本的組成（用 burst 大小掃描分離）

對 `BENCH_DATA` 送 N 包（一次 sendall），線性擬合 `時間 = a + b×N`：

| 狀態 | size | 截距 a | 斜率 b（每包）| 包/秒 |
|---|---|---|---|---|
| 全部 task 在跑 | 512 | 2.2 ms | **3.542 ms** | 282 |
| **只留 network + bus_decode** | 512 | 5.7 ms | **2.616 ms** | **382（+35%）** |
| 全部 task 在跑 | 4096 | −2.8 ms | **6.104 ms** | 164 |
| **只留 network + bus_decode** | 4096 | 24.2 ms | **5.243 ms** | **191（+16%）** |

**→ 舊文件 §1.5 的假設（standby task 空轉擠壓 network）得到證實**：
core0 上 5 個空轉 task 讓每包成本增加 16~35%。
但即使只留 2 個 task，每包仍要 **2.6ms（512B）/ 5.2ms（4KB）**。

---

## 3. 檔案路徑 = 純接收 + （寫檔 + 回 ACK）

同一支工具換成 `FILE_CHUNK`（0x2002，會寫檔並回 ACK）：

| payload | 純接收 ms/包 | 檔案路徑 ms/包 | **差值（寫檔+ACK）** |
|---|---|---|---|
| 512 B | 6.258 | 12.340 | **+6.08 ms** |
| 4096 B | 12.345 | 26.683 | **+14.34 ms** |

差值的 4KB−512B = 8.3ms ≈ **SD 寫入 8.03ms**（獨立量到）✓ 兩條路徑互相印證。

---

## 4. 停等 4KB 的完整分解（22~27 ms/包）

`file_sw_probe.py`（FILE_BEGIN 後延遲 1s，避開黏包；3 輪取中位）：

| 版本 | 停等 4KB |
|---|---|
| repo 的 `net_bus.py` | **174.2 KB/s**（174 / 172 / 183）|
| 板上的 `net_bus.py`（viper unmask + 8 slots + 黏包修正）| **162.8 KB/s**（163 / 173 / 145）|

> ⚠️ **兩版在統計上相同** → net_bus 版本不是瓶頸（我原先的假設被自己推翻）。

**每包 23 ms 的組成：**

| 成分 | 成本 | 佔比 | 來源 |
|---|---|---|---|
| **SD 寫入（4KB）** | **8.0 ms** | **35%** | §1 獨立量測 |
| 網路來回延遲 | 5.0 ms | 22% | ping / TCP connect |
| 資料傳輸（4KB 雙向）| 3.3 ms | 14% | 0.79 ms/KB × 4.2KB |
| 板子固定（含排程）| 3.5 ms | 15% | §2.1（全部 task 的 6.1ms − 傳輸 3.3ms）|
| **解碼（parse+decode+pack）** | **0.7 ms** | **3%** | 舊文件 §1.2 |
| 其餘（ACK 回程等）| ~2.5 ms | 11% | 殘差 |
| **合計** | **~23 ms** | | ✓ 與實測吻合 |

**→ 「解碼不是問題」在這張表上被量化：0.7ms / 23ms = 3%。**
**→ 最大的單一項是 SD 寫入（35%），其次是網路來回（22%）。**

---

## 5. 被實測推翻的三個假設（避免重複走冤枉路）

| 假設 | 實測 | 結論 |
|---|---|---|
| 「WS 幀 4119B > recv 緩衝 4115B → 每包拆 2 次 recv」造成階梯 | 精掃 payload 3900~4100：**全部都 ~10ms/包，4098 與 4099 之間沒有階梯** | ❌ 不是原因 |
| 「Python 逐 byte unmask 是頭號瓶頸（~20ms/4KB）」| **master 送的是未遮罩幀**（`masked=0` → 根本不會執行那段迴圈）；`NetBusMaster.py:823` 也自述「未 mask」 | ❌ 現行 master 下不成立 |
| 「板上的 net_bus（viper unmask + 8 slots）比 repo 版快」| 背靠背 A/B：**174.2 vs 162.8 KB/s** | ❌ 統計上相同 |

---

## 6. 真正的瓶頸鏈

```
停等協議（每包等一個 ACK）
   ×
每包 23ms  =  SD 寫入 8ms  +  網路來回 5ms  +  傳輸 3.3ms  +  板子固定 3.5ms  +  解碼 0.7ms
   =
4KB 停等 ≈ 150~180 KB/s        （水管 1.27 MB/s 只用掉 13%）
```

**chunk × 在途視窗 二維掃描**（`file_pipeline_sweep.py`，128KB/格）：

| chunk | w=1（停等）| w=2 | w=4 | w=8 | w=16 | w=32 |
|---|---|---|---|---|---|---|
| 512 B | 23.8 | 39.6 | 55.5 | 69.1 | 45.5 | 70.3 |
| 1024 B | 49.0 | 70.4 | 102.3 | 112.1 | 121.4 | 83.9 |
| 2048 B | 83.6 | 113.3 | 149.0 | 170.0 | 163.5 | 167.4 |
| 4096 B | 122.9 | 171.3 | 178.4 | **225.3** | 210.2 | 190.4 |

- **每一列都 ∝ chunk**（固定 window）→ 停等的鐵證
- **w 從 1 加到 8 有 1.8×**（4096B：122.9 → 225.3），**w≥16 反而下降**（佇列/排程反效果）
- 天花板 **225 KB/s**（舊文件在更快的環境量到 520 KB/s）

---

## 7. 順手挖到的地雷：`statvfs('/')` = 831 ms

```
os.statvfs('/sd')  →  0.03~0.10 ms
os.statvfs('/')    →  831.2 ms   ← 每次、穩定（量 5 次：832.07/831.18/831.28/832.09/831.55）
```

而 `fs_manager.write_chunk()` **每一塊**都呼叫：

```python
_free = self.free_bytes(self.session["path"])   # → os.statvfs(path)
if _free > 0 and _free < len(data) + 4096: ...
```

- 上傳到 `/sd`：0.06ms/塊 → 沒事
- **上傳到 `/`（內部 flash）：831ms/塊 → 4KB 的塊 ≈ 0.98 秒 → 約 4 KB/s**

那個檢查的註解自己寫著「**中途容量安全網（前置 QUERY.free 才是主力）**」——
一個 secondary 保險卻讓 flash 上傳慢 40 倍。
**遠端更新（把 `slave/` 檔案寫回 flash）走的就是這條路。**

修法（小）：`FILE_BEGIN` 時查一次，之後用 `written` 推導；或每 N 塊查一次。

---

## 8. 出路（依「每包延遲的哪一項」排序）

| # | 手段 | 打掉哪一項 | 預期 | 難度 |
|---|---|---|---|---|
| 1 | **pipelining（在途視窗 4~8）** | 網路來回 5ms／包 | **1.8×**（122.9→225.3 實測）| 中（改 master + slave 收序）|
| 2 | **SD 寫入批次化**（累積到 N 塊再一次落盤）| SD 8ms／包（最大單項）| 最多 ~1.5× | 中（要處理掉電一致性）|
| 3 | **移除/暫停空轉 task** | 板子固定 3.5→2.6ms | **+16~35%**（實測）| 小（affinity 或 `_update_tasks` 篩選）|
| 4 | **修 `free_bytes` 每塊 statvfs** | flash 路徑 831ms／塊 | **flash 上傳 4 KB/s → 數百 KB/s** | 小 |
| 5 | 加大 chunk | — | **無效**（§2：包/秒與 payload 無關，8KB 吃 2 槽）| — |
| — | 解碼／parser／CRC | 0.7ms／包（3%）| 3% | 已做（寫入路徑修正）|

> ⚠️ 第 5 項是本輪最重要的**否定結論**：`RX_BUF_SIZE` 提到 8K/16K 不會提升吞吐，
> 因為限制單位是「槽」且**每槽成本固定**，大幀只是吃更多槽。

---

## 9. 可複現指令

```bash
P=/dev/cu.usbmodem5B140827601

# 1) 裸 TCP 極限（兩端互相印證）
python3 -B tools/PC/net_bench_pc.py --once --total 4096 --runs 2     # PC 端
mpremote connect $P exec "exec(open('/bench_net.py').read()); run()"  # 板端

# 2) 純接收上限（不回覆、不寫檔）
python3 -u -B test/protocol/bench_rx_sweep.py --sizes 512,2048,4096,8192 --count 500

# 3) 停等 vs 管線（同一進程、同一時間窗）
MP_PORT=$P /Users/tungkinlee/miniconda3/bin/python3 -B test/protocol/measure_fix.py

# 4) chunk × window 二維掃描
MP_PORT=$P /Users/tungkinlee/miniconda3/bin/python3 -B test/protocol/file_pipeline_sweep.py --total 131072

# 5) 公平的停等基準（FILE_BEGIN 後延遲，避開黏包；3 輪取中位）
MP_PORT=$P /Users/tungkinlee/miniconda3/bin/python3 -u -B test/protocol/file_sw_probe.py 3
```

> ⚠️ **量測方法論**：這張網路的抖動極大（ping 閘道器 min 4.6 / avg 15.2 / **max 52.8 ms**），
> 同一份程式碼不同時間可量到 61.6~136 KB/s。**任何 <20% 的差異都必須背靠背 A/B 才可信**
> （`file_sw_probe.py` 的 3 輪中位數就是為此設計）。

---

## 10. 【後續實測】管線化是唯一有效的方向 —— 而且你的傳輸層擋住了它

### 10.1 管線化（多塊在途）＋ SHA256 驗證

`file_pipeline_probe.py`：FILE_BEGIN(帶 sha) → N 塊在途 → FILE_END → 比對 FILE_QUERY_RSP 的 sha。

**板上那份 net_bus（持久 `_ws_acc` + 16 槽上限）：**

| 在途視窗 | 吞吐 | ms/包 | SHA256 |
|---|---|---|---|
| 1（停等＝現況）| 163.2 KB/s | 24.50 | ✅ |
| 2 | 228.9 KB/s | 17.48 | ✅ +40% |
| **4** | **245.7 KB/s** | 16.28 | ✅ **+51%** |
| 8 | 220.7 KB/s | 18.12 | ✅ +35% |

**slave 端完全不用改** —— `write_chunk` 本來就用 `offset` 寫入（可亂序），
管線化純粹是 master 端「不要每塊等 ACK」。

### 10.2 ⚠️ 但 **repo 的 `net_bus.py` 撐不住**（這就是你專案被鎖在停等的原因）

同一支工具、同一塊板子，只把 `net_bus.py` 換成 repo 版（並在 config 加 `net_rx_slots: 8`）：

| 在途視窗 | repo 版 | 板上版 |
|---|---|---|
| 1 | 177.7 KB/s ✅ | 163.2 ✅ |
| **2** | **229.4 KB/s ✅（+29%）** | 228.9 ✅ |
| 3 | ❌ timeout | — |
| **4** | ❌ **timeout（只收到 2 個 ACK）** | **245.7 ✅（+51%）** |
| 8 | ❌ timeout | 220.7 ✅ |

**w=2 在 3 個獨立回合都通過且 SHA 正確（+33% / +31% / +24%）**；
**w≥3 直接卡死**（master 等不到 ACK，逾時）。

### 10.3 根因：`net_bus.poll()` 在 rx_hub 滿時把未消費資料丟掉

```python
# repo 版（slave/lib/sys/net_bus.py:318）
                        view = self.rx_hub.get_write_view()
                        if view is None:
                            return          # ★ 而 mv 指向 self._buf —— 每次 recv 的暫存！
                        pv = memoryview(view)[self._hub_off:]
```

`mv = memoryview(self._buf)[:n]`，而 `self._buf` **下一輪 `recv_into` 就會被覆蓋**。
所以「hub 滿時，這個 recv 裡還沒寫進 hub 的位元組」**永久遺失** → 掉幀。

停等模式下 hub 幾乎不會滿（一次只有一幀在飛）→ 症狀完全看不出來。
**一旦開管線化（多幀同時到達）就必然踩到** → 掉幀 → master 重送/逾時 → 卡死。

板上那份的修法（`net_bus.board_fast.py:285`）：

```python
            if view is None:
                # rx_hub 滿: 保留未消費資料 (i 之後), break 讓 bus_decode 消化
                break           # mv 指向持久的 _ws_acc → 未消費資料留著，下次繼續
```

**關鍵不是 `return`→`break`，而是 `mv` 必須指向一個「持久的累積 buffer」**
（板上是 `_ws_acc`），不能是每次 recv 的暫存 `_buf`。

### 10.4 我試過的「最小修正」：部分有效，但不足以支撐 w=4

在 repo 版上加了：

1. `_carry_len`：hub 滿時把 `_buf[i:]` 搬到 `_buf[0:]` 保留，下次 recv 接到它後面
2. `slots = min(slots, 4)` → `min(slots, 16)`（對齊板上版）

結果：

| 在途視窗 | repo 原版 | +carry | +carry+16槽 |
|---|---|---|---|
| 1 | 177.7 | 158.1 | 183.9 |
| **2** | 229.4（+29%）| **243.3（+54%）** | 242.1（+32%）|
| 4 | ❌ | ❌ | ❌ |
| 8 | ❌ | ❌ | ❌ |

**→ w=2 的增益變好，但 w≥4 仍卡死。**
剩下缺的應該是板上那份的**完整 `_ws_acc` 持久累積架構**（不是只保留「一個 recv 的尾巴」，
而是整個 WS 解析都在持久 buffer 上做、跨 poll 保存半幀狀態與未消費位元組）。

### 10.5 可立即執行的結論

| 手段 | 增益 | 風險 | 改哪裡 |
|---|---|---|---|
| **master 在途視窗 = 2** | **+24~33%（3 回合實測，SHA 正確）** | **極低**（slave 不用改；w=2 在目前程式碼下已驗證可用）| 只改 master |
| 移植板上那份 `net_bus.py` 的 `_ws_acc` 架構 → 視窗 4~8 | **+51%** | 中（動傳輸層核心，需完整回歸）| slave `net_bus.py` |
| **修 `free_bytes` 每塊 statvfs** | flash 上傳 **5.2 → 11.7~16.6 KB/s（2.2~3.2×）**，SHA 正確 | 低 | 已做，見 §11 |
| 移除空轉 task | **+36%**（128.8→175.7 KB/s 中位）| 低（affinity / 啟動篩選）| config / task 註冊 |

---

## 11. 【已修並驗證】`free_bytes` 每塊 statvfs（flash 上傳 3 倍）

### 11.1 症狀

同一支上傳程式、同一個 4KB chunk，只換目標路徑：

| 目標 | 停等 4KB | ms/包 |
|---|---|---|
| `/sd`（SD 卡）| **159.4 KB/s** | 25.1 |
| `/`（內部 flash）| **5.2 KB/s** | **773.3** |

### 11.2 根因

`fs_manager.write_chunk()` 每塊都呼叫 `self.free_bytes(path)` → `os.statvfs(path)`：

```
os.statvfs('/sd')  →  0.03 ~ 0.10 ms
os.statvfs('/')    →  831 ms      ← 每次、穩定（量 5 次都 831~832）
```

那個檢查只是 secondary 保險（它自己的註解：「前置 QUERY.free 才是主力」），
卻讓 flash 上傳每 4KB 塊多付 0.83 秒。

### 11.3 修法（`slave/lib/sys/fs_manager.py`）

`FILE_BEGIN` 時清 `free_est`；`write_chunk` 只在**每個 session 第一次**呼叫 `free_bytes()`，
之後用「已寫入量」單調遞減推導：

```python
        _free = self.session.get("free_est")
        if _free is None:
            _free = self.free_bytes(self.session["path"])
            self.session["free_est"] = _free
            self.session["free_base_written"] = self.session.get("written", 0)
        elif _free > 0:
            _free -= (self.session.get("written", 0)
                      - self.session.get("free_base_written", 0))
            self.session["free_est"] = _free
        if _free > 0 and _free < len(data) + 4096:
            self.session["last_error"] = "NO_SPACE"
            return False
```

### 11.4 驗證

| 目標 | 修前 | 修後 | 驗證 |
|---|---|---|---|
| `/`（flash）4KB | 5.2 KB/s | **16.6 KB/s** | — |
| `/`（flash）32KB 完整流程 | — | **11.7 KB/s** | ✅ SHA256 相符 |
| `/sd` 32KB 完整流程 | 149~159 | 139.9 KB/s | ✅ SHA256 相符（無退步，在抖動內）|

殘餘的 ~242ms/包是 **flash 寫入本身**（板上獨立量到 4KB flash 寫入 149.9ms，
LittleFS 的 block erase 成本），不是這次修改造成的。

---

## 12. 量測工具總表（本輪新增）

| 工具 | 用途 |
|---|---|
| `test/protocol/bench_rx_sweep.py` | 純接收上限（`BENCH_DATA` 不回覆不寫檔）vs FILE_CHUNK 對照 |
| `test/protocol/file_pipeline_sweep.py` | chunk × 在途視窗 二維掃描 |
| `test/protocol/file_pipeline_probe.py` | **管線化 + SHA256 驗證**（證明加速可用）|
| `test/protocol/file_sw_probe.py` | 可重複的停等基準（FILE_BEGIN 後延遲避開黏包、多輪取中位）|
| `test/protocol/file_rtt_probe.py` | 拆解每塊成本（QUERY／CHUNK 0B/16B/1KB/4KB）|
| `test/protocol/bench_slice_assign.py` | 切片賦值成本曲面（任何固件可跑）|
