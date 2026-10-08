# 多級緩衝架構（Multi-Level Buffering）

> **用途**：理解 `slave/` 資料從「網路/SD/檔案 → 解析 → 應用 → DMA 輸出」一路上經過的所有緩衝層級，以及每一層的記憶體來源、所有權與使用慣例。
> **分類**：筆記（03_notes）
> **最後更新**：2026-08-16（對齊 worker_engine / taskmanager 雙模式）
> **對象**：任何要在 `slave/` 底下加緩衝、傳輸資料、優化記憶體的人。寫 code 前先讀這份 + `Skills/buffer-conventions`。

---

## 1) 為什麼需要多級緩衝

ESP32-S3 的 MicroPython 環境下，記憶體有限且 GC 不可預測。資料路徑若每一段都 `bytes 拼接 / bytearray 新建`，高頻率（網路封包、JPEG 幀、pixel 資料）下會造成：

- **GC 抖動**：每幀分配 → 回收 → 碎片化，導致播放卡頓
- **無謂複製**：同一份資料被拷貝 2~3 次，浪費 CPU 與記憶體頻寬
- **跨核心撕裂**：Core 0 寫一半 Core 1 讀，拿到半幀

本專案用 **5 個層級** 的固定架構解決這三件事，每一層都有明確職責與所有權，不重複發明。

---

## 2) 層級總覽

```
┌────────────────────────────────────────────────────────────────┐
│ L0 分配層   alloc_dma / free_dma        ← 唯一碰 heap_caps 的入口 │
│            (lib/sys/buffer_hub.py)       CAP_DMA 優先,bytearray 兜底 │
├────────────────────────────────────────────────────────────────┤
│ L1 Ring 層  AtomicStreamHub (SPSC 無鎖)  ← 跨任務/跨核心交換核心  │
│            (lib/sys/buffer_hub.py)       copy / view 兩種模式    │
├────────────────────────────────────────────────────────────────┤
│ L2 傳輸層   NetBus / CircuitBus / NowBus ← recv → rx_hub 零拷貝  │
│            (lib/sys/net_bus.py 等)       slot 前 2B 長度字首      │
├────────────────────────────────────────────────────────────────┤
│ L3 協議層   StreamParser + Proto.pack    ← 黏包/拆包、零分配組包  │
│            (lib/sys/proto.py)            viper 加速 compact/append│
├────────────────────────────────────────────────────────────────┤
│ L4 應用層   pixel_stream hub / jpeg fb   ← 雙核心供應鏈、framebuffer│
│            (tasks/render.py 等)          PSRAM-first 策略        │
├────────────────────────────────────────────────────────────────┤
│ L5 輸出層   bus_adapter DMA / fast_io    ← C 層分 chunk、雙緩衝   │
│            (lib/sys/bus_adapter.py 等)   4-deep queue 流水線      │
└────────────────────────────────────────────────────────────────┘
```

**關鍵原則**：資料流永遠是「上層寫入、下層消費」，每層之間只透過 `memoryview` 傳遞，不複製內容。**不要為了某一層的方便，跨層直接碰其他層的內部 buffer。**

---

## 3) L0 — 記憶體分配層：全專案唯一碰 heap_caps 的地方

`lib/sys/buffer_hub.py` 提供 `alloc_dma(size)` / `free_dma(buf, is_dma)`，是取得「DMA 記憶體（內部 SRAM）」的統一入口。

```python
from lib.sys.buffer_hub import alloc_dma, free_dma

buf, is_dma = alloc_dma(size)   # is_dma=True → 在內部 SRAM,要 free_dma 釋放
# ... 用 buf ...
free_dma(buf, is_dma)           # is_dma=False → no-op,bytearray 交給 GC
```

- `alloc_dma` 優先 `heap_caps.malloc(size, CAP_DMA)`，失敗自動 fallback `bytearray`。**回傳永遠可用，不會是 None。**
- `is_dma` 旗標必須記住並帶進 `free_dma`，否則 DMA buffer 漏記憶體（heap_caps 不受 GC 管理）。
- **禁止**在其他檔案直接 `import heap_caps`。歷史教訓：四個檔案各寫一份 try/except 樣板，全收斂到這裡了。

**唯一例外** — framebuffer 用「PSRAM-first」策略，走 framebuffer 自己的 `_alloc_fb()`：

```python
# 優先序: CAP_SPIRAM (大但慢) → CAP_DMA → bytearray
```

> ⚠️ **2026-10 更正**：本節舊版把這個例外綁在 `tasks/jpeg_player_task.py`，**該任務已刪除**
> （見 `slave/tasks/lvgl_task.py:13`「jpeg 播放器已移除」；`0x31xx` 域已改由 pixel 模式播放使用）。
> 「framebuffer 走 PSRAM-first、獨立於 `alloc_dma`」這個**原則仍然成立**，但不要再去找那個檔案。
> 本文件其他位置只要提到 `jpeg_player_task.py` / `media_source.py` / `pack_source.py`，同屬已移除的舊內容。

framebuffer 需要的是一次性大塊、慢速 OK，與 ring 的「內部 SRAM、快速、跨 core 一致」需求不同，所以獨立。**除非你在做 framebuffer，否則一律走 `alloc_dma`。**

### 誰用了 L0

| 使用者 | 記憶體來源 | 釋放方式 |
|--------|-----------|---------|
| `AtomicStreamHub(try_dma=True)` | CAP_DMA slot | `hub.close()` → `free_dma` |
| `fast_io.Storage` | 單一 DMA buffer | `s.close()` / `__del__` |
| `fast_io.StreamReader` | 雙 DMA buffer | `r.close()` |
| `jpeg_player_task` framebuffer | CAP_SPIRAM → CAP_DMA → bytearray | `on_stop()` 手動 `heap_caps.free` |

---

## 4) L1 — Ring 層：`AtomicStreamHub`（SPSC 無鎖環形緩衝）

跨任務、跨核心的資料交換全部用 `AtomicStreamHub`（`lib/sys/buffer_hub.py`）。它是 **SPSC** — 單一寫入者、單一讀取者。

### 內部結構：三狀態 slot 狀態機

```
num_buffers 個 slot,每個 slot 狀態 ∈ {IDLE, READY, READING}
   IDLE    → 寫端可用 (get_write_view / write_from)
   READY   → 寫完待讀 (commit 後)
   READING → 讀端取出中 (get_read_view 後,release_read 歸還)
```

- `_w_ptr` 只有寫端碰、`_r_ptr` 只有讀端碰 → **無需鎖**。
- `@micropython.native` 標註熱路徑方法，盡量快。

### 建構

```python
hub = AtomicStreamHub(size=1024, num_buffers=3, try_dma=False)
#                  ↑ 每 slot 位元組數  ↑ slot 數  ↑ True = slot 配在內部 SRAM
```

### 兩種使用模式（涵蓋所有場景，同一個 hub 同一筆資料不要混用）

**copy 模式**（小封包、省事）：

```python
# 寫端
hub.write_from(source_bytes)     # 整塊 copy 進 slot;滿了回 False
# 讀端
hub.read_into(target_bytes)      # 整塊 copy 出來;空了回 False
```

**view 模式**（大 frame、零拷貝）：

```python
# 寫端
v = hub.get_write_view()         # 取出 slot 的 memoryview;滿了回 None
if v is not None:
    v[:n] = my_data
    hub.commit()                 # IDLE → READY,推進寫指標

# 讀端
v = hub.get_read_view()          # 取出 READY slot;空了回 None
if v is not None:
    process(v)
    hub.release_read()           # 歸還 slot(READING → IDLE)
```

其他 API：`flush()`（全清）、`dirty`（讀指標處是否 READY）、`get_fill_level()`、`force_get_view()`。

### 誰用了 L1

| 用途 | 位置 | 模式 |
|------|------|------|
| 每個 bus 的接收環 | `NetBus.rx_hub` / `CircuitBus.rx_hub` / `NowBus.rx_hub` | view 模式寫入（recv_into 直進 slot） |
| 網路消費端鏡像 | `NetBus.cache_hub`（惰性建立，rx_hub 的鏡像） | view 模式 |
| 雙核心像素流 | `pixel_stream` service（Core0 寫 / Core1 讀） | 供應鏈 view + copy |
| 直接幀模式 | `0x3003 STREAM_FRAME` → `pixel_stream.write_from()` | copy |

---

## 5) L2 — 傳輸層：recv 直接進 ring，零拷貝

`lib/sys/net_bus.py`（TCP/WS/UDP）、`lib/sys/circuit_bus.py`（UART 實體線）、`lib/sys/now_bus.py`（ESP-NOW）共用同一套架構：

### 5.1 接收路徑（零拷貝）

```
socket.recv_into(pv)          ← 直接寫進 rx_hub 的 slot (pv = view[2:])
struct.pack_into("<H", view, 0, n)   ← slot 前 2 bytes 存實際長度 (_hub_off=2)
rx_hub.commit()
```

```python
# 每筆資料在 slot 內的佈局:
# [0:2] = u16 LE 長度 n | [2:2+n] = 實際資料
```

`BusDecodeTask`（`tasks/bus_decode.py`）一次 drain 所有 bus 的 `rx_hub`，用 `read_into` 取出後交給 `StreamParser`：

```python
# tasks/bus_decode.py 核心
for b in self._buses:
    hub = getattr(b, "rx_hub", None)
    while used < max_slots and hub.read_into(self._read_buf):
        ln = self._read_buf[0] | (self._read_buf[1] << 8)   # 長度字首
        self.app.handle_stream(parser, mv[2:2+ln], ...)     # 餵給協議層
```

### 5.2 重要行為開關（來自 `config.json` 的 `Buffer` 區塊）

`config.json` 已有：

| config key | 預設 | 作用 |
|-----------|------|------|
| `size` | 16384 | 每 slot 位元組數（實際 recv buffer ≤ 4096，已被 `RX_BUF_SIZE` 取代，見 `01_protocol/08_performance_benchmark.md`） |
| `rx_hub_buffers` | 16 | 通用 rx_hub slot 數 |
| `drop_on_full` | 1 | ring 滿時：1 = 丟棄（讀走資料），0 = 停止收 |
| `drain_reads` | 1 | 每次 poll 最多 recv 幾次 |
| `fb_mode` | auto | framebuffer 模式 |

程式碼額外讀取（可選，未設定時用 fallback）：

| config key | fallback | 作用 |
|-----------|----------|------|
| `net_rx_slots` / `now_rx_slots` / `u8_rx_slots` | 2 | 各 bus 的 ring slot 數（上限 4） |
| `decode_budget_slots` | 32 | BusDecodeTask 單次 loop 最多解析幾筆 |

### 5.3 WebSocket 拆幀（就地 unmask）

`NetBus.poll()` 的 WS 分支：

- header 不完整 → 存進 `_ws_hdr`（14 bytes）暫存，湊齊再解析。
- payload 就地 `chunk[j] = b ^ mask`，**不回新 bytes**。
- 拆完的 payload 直接寫進 rx_hub slot，不做第二次複製。

### 5.4 消費端鏡像：`cache_hub`

`read_into(target)` 提供「不碰 rx_hub」的消費路徑：

```python
# 首次呼叫時惰性建立 cache_hub(與 rx_hub 同 size/slots)，之後永久重用
cache = self.cache_hub or self._make_cache_hub()
view = cache.get_read_view()      # 從鏡像讀
ln = view[0] | (view[1] << 8)     # 長度字首
target[:n] = view[2:2+ln]
cache.release_read()
```

- 每次 `_commit()` 時，rx_hub 寫入的同時鏡像複製一份到 cache_hub。
- **解碼器讀 rx_hub、消費者讀 cache_hub，兩邊各是獨立 SPSC，互不影響。**

---

## 6) L3 — 協議層：`StreamParser` + `Proto.pack`

`lib/sys/proto.py` 是封包組裝/解析核心，有兩個非常重要的效能設計。

### 6.1 `Proto.pack`：共享 buffer 零分配組包

```python
# 模組級共享 buffer(_pack_buf / _pack_mv)，惰性配一次、之後永久重用
global _pack_buf, _pack_mv, _pack_cap
if _pack_buf is None or _pack_cap < total:
    _pack_cap = total + 512          # 預留成長空間
    _pack_buf = bytearray(_pack_cap)
    _pack_mv = memoryview(_pack_buf)

struct.pack_into("<2sBHHH", b, 0, SOF, CUR_VER, addr, cmd, ln)  # header 直接寫
b[HDR_LEN:HDR_LEN+ln] = payload                                 # payload 直接寫
```

- 舊版用 `header + payload + crc` 的 bytes 拼接（每幀分配 + 複製，佔協議成本 78%），現已改寫進共享 buffer，**零分配零複製，組包快約 17x**。
- **⚠️ 生命週期契約**：`pack()` 回傳的 memoryview 指向共享 buffer，**下一次呼叫會覆蓋**。呼叫端必須「立即消費」（送出/寫入），不可跨呼叫持有。專案內所有呼叫點都是 `send(pack(...))` 立即消費。

### 6.2 `StreamParser`：黏包/拆包 + SOF 重同步

```python
parser = StreamParser(max_len=...)   # 內部 bytearray(max_len + 9 + 4)
parser.feed(data)                    # viper 加速 append(不足時 compact 搬移)
for ver, addr, cmd, payload in parser.pop():   # 生成器，一次撈出所有完整封包
    dispatch(cmd, payload, ctx)
```

- **SOF 重同步**：錯位時 `self._buf.find(SOF, ...)` 掃描下一個 `b"NC"`。
- **CRC 驗證優先**：收齊整幀先驗 CRC32，錯就直接跳過重同步，不 dispatch。
- **max_len 保護**：`ln > max_len` 時跳過，避免誤同步讀到超大 LEN 造成記憶體溢出。
- **viper 加速**：`_viper_compact`（搬移保留資料）與 `_viper_append`（追加）避免 Python 層逐 byte 迴圈。
- **零拷貝快路徑**：`pop_frame()` 回 `(ver, addr, cmd, payload_mv)`（見 `03_notes/01_changelog.md §5`）。

### 6.3 解析策略總結

```
feed(bytes) → 內部緩衝 (compact 騰空間) → pop() 逐幀:
   找 SOF → 驗 VER / LEN → 湊齊整幀 → 驗 CRC32 → yield (ver, addr, cmd, payload)
   任一失敗 → start 前移 1 byte 繼續找下一個 SOF
```

---

## 7) L4 — 應用層：雙核心供應鏈與 framebuffer

### 7.1 `pixel_stream` hub — 雙核心資料交換

Core 0（網路/控制）與 Core 1（渲染/顯示）之間用一個 `AtomicStreamHub` 交換像素資料：

```
Core 0                                   Core 1
┌──────────────────────┐                 ┌──────────────────────┐
│ stream_actions.py    │                 │ tasks/render.py      │
│ handle_supply_chain  │                 │ RenderTask.loop()    │
│  (定時調用)           │                 │                      │
│ get_write_view()     │                 │ hub.read_into(       │
│ f_local.readinto(v)  │  pixel_stream   │   st_pixel.big_buffer) │
│ commit()             │ ──────────────→ │ show_all()           │
└──────────────────────┘   (Core0 寫)    └──────────────────────┘
```

供應鏈邏輯（`action/stream_actions.py`）：

- `is_seeking` 時：`hub.flush()` → 開檔 → `seek()` → 預填第一幀 → commit → 主動回 `STREAM_READY_ACK(0x3008)`。
- 播放中：利用 `hub.dirty` 檢查供給，空了才 `get_write_view()` + `f_local.readinto(view)` + `commit()`。
- `0x3003`（Direct Mode）直接把整包 pixel 塞進 hub：`pixel_stream.write_from(a["pixel_data"])`。

### 7.2 JPEG 播放器 framebuffer — 單 framebuffer 平行 pipeline

`tasks/jpeg_player_task.py` 的設計：

```
decode_into(block N) → 寫入 fb[block_N 區域]
         ‖ (平行)
DMA 正在讀取 fb[block_N-1 區域] 發送到 LCD
```

- **單 framebuffer**（PSRAM-first 分配，見 L0），不同 offset 平行使用。
- 無中間 hub、無 bounce copy、**零 GC 分配**。
- 輸出走 `bus_obj.write_data_async(mv)`（分段 `_SEND_CHUNK = 32KB`），由 C 層自動 async 分 chunk（見 L5）。

### 7.3 統一資料層與媒體來源

- `lib/sys/fs_manager.py` → `bus` 上的 `data` / `fs` service，`media_source._open_read` 優先走它，失敗退回原生 `open()`。
- `lib/sys/media_source.py` → 統一媒體來源抽象（`folder` / `jpk` / `bin` 三模式），對外只有 `read_into(fb)`。

---

## 8) L5 — 輸出層：C 層 DMA 分 chunk 與雙緩衝串流

### 8.1 `BusAdapter.write_data_async` — 不做 Python bounce

`lib/sys/bus_adapter.py` 的 `SpiBusAdapter.write_data_async`：

```python
# 大 buffer(>32KB max_transfer_sz) → C 層自動 async 分 chunk 直送
# (內部 RAM 或 PSRAM 皆異步;不再過 Python bounce 序列化)
for attempt in range(2):
    try:
        return self._spi.write(data)
    except RuntimeError as e:
        self._spi.wait_all()          # queue 滿 → 清空後重試一次
        ...
```

### 8.2 `write_frame_dma` — 4-deep queue 流水線

```python
# 分 chunk 填 4-deep queue,pending>=3 時退讓最早的(留 1 slot 餘裕)
while rem > 0:
    n = min(chunk, rem)
    if self._spi.pending() >= 3 and tids:
        self._spi.wait(tids.pop(0))
    try:
        tid = self._spi.write(mv[off:off+n])
        tids.append(tid)
    except RuntimeError:
        self._spi.wait_all(); continue
    off += n; rem -= n
```

不逐 chunk wait，把 queue 填滿 → 硬體連續 DMA → 最後一次 `flush()/wait_all()` 收尾。

### 8.3 `fast_io.StreamReader` — SD 讀取雙 DMA 緩衝

`lib/sys/fast_io.py` 的 `StreamReader`（雙 DMA buffer 零複製串流，實測 12.8 MB/s）：

```python
r = StreamReader(buf_size=16384, n_bufs=2)   # 兩個 DMA buffer
r.start(alloc, "scene.jpk")

while True:               # feed(讀 SD) → next(拿 memoryview) → release(歸還)
    if not r.feed(sec):   # 一個 buffer 忙著被消費,另一個在讀 SD
        v = r.next()
        if v is not None:
            process(v)    # 零複製,直接操作 memoryview
            r.release()
        continue
    ...
```

| 方法 | 說明 |
|------|------|
| `feed(sector)` | 從 SD 讀一個 chunk 到空閒 DMA buffer |
| `next()` | 取得 ready chunk 的 `memoryview`（零複製） |
| `release()` | 歸還 buffer |
| `read_into(buf)` | 複製模式（相容 Storage 風格） |

**重要**：`next()` 回傳的 memoryview 指向 DMA buffer，處理後**必須 `release()`**，否則 buffer 無法回收。

### 8.4 `fast_io.Storage` — 檔案級別讀寫

單 DMA buffer，繞過 FAT/VFS 直接操作 raw sector。分段寫入流程：

```
write_begin → alloc.json 佔位 (CRC=FFFFFFFF)
write(chunk) → SD 寫入 + CRC32 累計
write_end   → alloc.json 更新為最終 CRC32
```

---

## 9) 各層記憶體來源速查

| 層 | 元件 | buffer 類型 | 誰分配 | 誰釋放 |
|----|------|------------|--------|--------|
| L0 | `alloc_dma` | CAP_DMA / bytearray | `buffer_hub` | `free_dma`(或 GC) |
| L1 | `AtomicStreamHub` | 每 slot 一個 buffer | hub 建構時 | `hub.close()` |
| L2 | bus `_buf` / `_ws_hdr` / `_drop_buf` | 普通 bytearray(小) | bus 建構時 | GC |
| L3 | `StreamParser._buf` | bytearray(max_len+13) | parser 建構時 | GC |
| L3 | `Proto._pack_buf` | 模組級共享 bytearray | 首次 pack 時 | 永不釋放(程序生命期) |
| L4 | `pixel_stream` hub | 看 hub 建構參數 | boot/app | hub 生命期 |
| L4 | jpeg framebuffer | CAP_SPIRAM → CAP_DMA → bytearray | `_alloc_fb()` | `on_stop()` |
| L5 | SD DMA buffer | CAP_DMA | `Storage`/`StreamReader` | `close()` |

---

## 10) 使用規則（寫 code 前必看）

1. **要 DMA 記憶體** → `alloc_dma`，禁止自己 `import heap_caps`（framebuffer 例外）。
2. **跨任務/跨核心交換** → `AtomicStreamHub`，一對 TX/RX ring；禁止自建 ring。
3. **小資料塞 ring** → copy 模式；大 frame → view 模式；同一筆資料不要混用。
4. **不要加 bounce buffer** — 熱路徑已由 C 層分 chunk；真的需要時先討論。
5. **`Proto.pack()` 回傳值要立即消費**，不可跨呼叫持有。
6. **`StreamReader.next()` 的 view 用完要 `release()`**；`get_read_view()` 用完要 `release_read()`。
7. **`read_into` 的目標 buffer 要夠大**，ring 滿了回 False 是正常，不是錯誤。
8. **BusDecodeTask 由 `BusSources` 提供 bus 清單**，新增 bus 記得 `BusSources.add(bus)`。
9. **記憶體估算**：`heap_caps.get_largest_free_block(caps)` 比 total 重要，大 buffer 不保證連續。

---

## 11) 相關檔案

- `lib/sys/buffer_hub.py` — `alloc_dma` / `free_dma` / `AtomicStreamHub`（L0 + L1）
- `lib/sys/net_bus.py` — TCP/WS/UDP bus，recv → rx_hub + cache_hub（L2）
- `lib/sys/circuit_bus.py` — UART 實體線 bus（L2）
- `lib/sys/now_bus.py` — ESP-NOW bus（L2）
- `lib/sys/bus_sources.py` — `BusSources` 註冊表（BusDecodeTask 消費）
- `lib/sys/proto.py` — `Proto.pack` 共享 buffer / `StreamParser`（L3）
- `lib/sys/schema_codec.py` / `lib/sys/schema_loader.py` — payload 編解碼
- `lib/sys/fast_io.py` — SD `Storage` / `StreamReader` 雙 DMA 緩衝（L5）
- `lib/sys/bus_adapter.py` — SPI/I2C/I80/RGB adapter，DMA 分 chunk（L5）
- `tasks/bus_decode.py` — `BusDecodeTask`，drain 所有 rx_hub（L2→L3 橋）
- `tasks/network.py` / `tasks/circuit.py` — bus poll 驅動
- `tasks/render.py` — `RenderTask`，Core1 消費 pixel_stream（L4）
- `tasks/jpeg_player_task.py` — framebuffer pipeline + PSRAM-first 分配（L4）
- `action/stream_actions.py` — 供應鏈（seek/ready/預讀）（L4）
- `lib/sys/media_source.py` / `lib/sys/pack_source.py` — 統一媒體來源（folder/jpk/bin）

## 相關文件

- `02_guides/01_fast_io.md` — SD 卡中央儲存管理器（L5 實作）
- `02_guides/03_memory_management.md` — heap_caps 記憶體分配
- `01_protocol/01_nc4_protocol.md` — NC4 封包（L3 協議層）
- `01_protocol/08_performance_benchmark.md` — 性能基準（L2/L3 熱路徑改動）
