# Router 的兩個本機來源（`self` / `vbus`）—— **已決：不改 Router**

> **用途**：記錄「**本地發起 → 絕對內部執行，不外送**」這條路徑的**定案結論**，免得下次又要重推一遍。
> **狀態**：✅ **已決（2026-10）：選 A —— 不改 Router；分流改在「產生指令的人」那一側。**
> **最後更新**：2026-10
> **相關文件**：
> - `doc/02_guides/16_signal_router.md` **§3.3 / §3.4**（摺疊點、實測、決策都寫在那裡）
> - `doc/03_notes/19_remote_control_plan.md` **§2.5**（執行 vs 發射＝意圖，由產生者決定）
> - `todo/05_node_pairing.md` §3（UI 的分界線 —— **本決策的落地處**）

---

## 0. 定案

> **不動 Router。** 「本機發起」的兩個意圖由**產生指令的人**用**兩個 API** 分流：

| 意圖 | 用哪個 API | 走 Router？ | 會外送嗎 |
|---|---|---|---|
| **絕對內部執行** | `app.disp.exec_cmd(cmd, args, ctx)` | ❌ 不經 | ❌ **保證不會** |
| **可能對外發出** | `vbus.inject(frame)` | ✅ 經（`{in:"self", out:[…]}`）| 依 `out` 決定 |

**這就是 `doc/03_notes/19` §2.5 的落實**：

> 「這條指令是執行還是發射」**必須由產生指令的人決定**。
> 語意差別：`exec_cmd` 是「**我呼叫一個函式**」；`vbus.inject` 是「**本機發起一幀**」。

**Router 的角色不變**：它只管「**已經決定要發射的幀**走哪條管子」，不管意圖。

---

## 1. 為什麼不選 B（把 `vbus` 拆回來）

| | **A（採用）** | **B（否決）** |
|---|---|---|
| 改動 | **0 行** | ~6 行（`name_of()`＋vBus 建構＋`_LABEL_EXACT`）|
| 「絕對」| ✅ 結構上保證（不經 Router）| 要靠 `gate()` 新增保留語意 |
| 表上可見 | ❌ | ✅ |
| 風險 | 無 | 動 2026-09 定案；新增保留語意；`name_of()` 要能分辨「哪一種本機來源」|

**否決 B 的理由**：`exec_cmd` 已經提供**結構上**的保證（不經 Router ＝ 不可能被路由出去），
而 B 需要用「執行期語意」去達到同一件事，還要改動已被定案的來源模型。**A 更強且更便宜。**

> ⚠️ **B 已否決，連同理由。勿重提**（同 `doc/02_guides/16_signal_router.md` §7.3 的寫法）。

---

## 2. 事實記錄（留著，因為它是「為什麼會搞混」的答案）

### 2.1 原本的設計意圖是兩個本機來源

| 來源 | 語意 |
|---|---|
| **`vbus`**（設計上）| 本地發起 → **絕對內部執行，不會路由出去** |
| **`self`** | 本地發起 → 依 `out` 決定（可執行／可外送／兩者）|

**為什麼當時要分兩個**：`in` 是純量（Router §4.2）→ 一個來源恰好一條 route。
摺成一個名字之後，本機發起的幀只剩**一種行為**可配。

### 2.2 但**兩個摺疊點**都在查表之前（所以摺成了一個）

```python
def name_of(self, bus_obj):
    if is_local_bus(bus_obj):        # ← 摺疊點 ① io is None → 直接回 "self"
        return SELF
    ...
    return iface_name_from_label(label)   # ← ② "VBUS" → _LABEL_EXACT → "self"
```

### 2.3 ★ 實測：光寫 config 不會生效

`python -B temp/probe_router_vbus.py`

| 受測 | 結果 |
|---|---|
| 只寫 `{ "in": "vbus", "out": ["self"] }` → `by_in` | **`['vbus']`** ← 表裡**有**這一條 |
| 同上，`gate(真實 vBus)` | **`V_DROP`** ❌ |
| 同上，`gate(只有 label=VBUS)` | **`V_DROP`** ❌ |
| 改寫 `{ "in": "self", "out": ["self"] }` → `gate(...)` | `V_EXECUTE` ✅ |

> ★ **「進表」≠「會被查到」** —— `doc/02_guides/16_signal_router.md` §3.3 那句
> 「`in: "vbus"` ✅ 有效」是對的但**會誤導**（已改成「進表，但永遠查不到」）。

### 2.4 `router_selftest.py` 為什麼掛

```python
# router_selftest.py:189-196
        {"in": "vbus", "out": ["self"]}])
eq(r2.gate(FakeBus("VBUS"), ADDR_BROADCAST, 0x3105, b""), V_EXECUTE,
   "label VBUS → 邏輯名 vbus")
```

它寫的是**原設計**（`VBUS → "vbus"`）。實作在 2026-09 統一時偏離，測試沒跟上。

**A 定案之後，這兩行是「測試錯」而不是「實作錯」** → 修法是把測試改成：
- 用 `in: "self"` 測「本機發起」（真實行為）
- 或明確斷言 `VBUS → "self"`（把現行語意釘住）

---

## 3. 待辦 —— ✅ 全部完成（2026-10）

- [x] 修 `test/protocol/router_selftest.py`：改成符合現行語意（§2.4）
      → **75/75 ALL PASS**。實際修了**三個**問題，不只一個：
      1. `build()` 沒有註冊介面 → route 全進 `_pending` 等 `sync_ifaces()` 結算
         → `by_in` 是空的，**看起來像「全部被拒絕」**。`build()` 改成自動註冊
         route 用到的名字（`skip=` 可指定故意不註冊的）。
      2. `FakeBus("VBUS")` 的期待改成現行語意：`VBUS → "self"`（見 §2.4）＋
         這張表沒有 `self` 條目 → `V_DROP`。
      3. ★ `Router._by_id` 用 `id(bus_obj)` 當索引，而測試用的都是**臨時物件**
         → 被 GC 回收後位址被下一個臨時物件重用，`name_of()` 會查到
         **已經死掉的物件**的名字（實測 `FakeBus("CTRL-WS")` 查到 `'uart1'`）。
         修法：`build()` 保留強引用（`r._keepalive`）。
         真機上各 bus 都是長命物件所以無害，但這是**測試專屬的陷阱**，值得記著。
- [x] `doc/02_guides/16_signal_router.md` §3.4 標記「已決：A」
- [x] `todo/05_node_pairing.md` §3 寫入 UI 的兩個入口

### 3.1 順手收掉：`lib/` 三級重構後 test/ 腳本的路徑（同日）

`test/protocol/night_run/REPORT.md` 記的「**`lib/` 三級重構後 test/ 腳本未同步 import 路徑**」
一次清掉：`lib.X` → `lib.sys.X`，共 **24 處 / 15 個檔案**（tft/sd/thread/husb238/ui/bench_net）。
只改**程式碼行**（行首是 `from`/`import` 的），說明文字裡提到的舊路徑是歷史敘述，保留。
另外補上三個測試缺的 `sys.path` bootstrap：
`test_proto_hotpath.py`、`test_proto_speed.py`（以前只能從專案根目錄跑）。

結果：`test/protocol/` 與 `test/buffer/`、`test/thread/` 在 PC 上**全綠**
（基線時 `router_selftest.py` 與 `test_proto_hotpath.py` 是壞的）。
⚠️ `test/ui/ui_test_tool.py` 是**裝置專用**（硬寫 `/ui/lvgl/src`、假設 boot.py 已跑），
在 PC 上 import 不到 `ui` 是正常的；`espnow_*.py` / `rs485_probe.py` / `wtt_rx_probe.py`
同理（需要 `network`/`machine`）。

## 筆記

- **本項不阻塞 `todo/05_node_pairing.md`** —— 配對只要 `{in:"self", out:["now"]}`（面板 config 已有）。
- 這已經是第 **二** 次「文件／測試保留了原設計，實作漂走」的案例
  （第一件是 `todo/05` 的 `by_cid()`）。→ **動這塊之前先讀文件，不要相信現況。**
  差別是：`by_cid()` 那次是**實作漂走、文件對**（要修實作）；
  這次是**設計改了、測試沒跟上**（要修測試）。**兩者要先分清是哪一種。**
