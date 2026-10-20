# 遙控器（Mon）↔ fastLED Master 配對行為

> 以 mp_Net-Core 的 Mon 規格為準。fastLED 的 `mon_pairing` 行為必須符合本文件。

## 基準（主線實作）

行為以本文件為準；實作細節以 **mp_Net-Core 主線**這些檔案為準：

| 功能 | 主線位置 |
|---|---|
| 匯流排收發／學 MAC | `slave/lib/sys/now_bus.py`（`poll` / `learn_peer` / `resolve_addr` / `write`） |
| 收幀來源 MAC | `slave/tasks/bus_decode.py`（`_src_mac`） |
| CID ↔ MAC 對照 | `slave/lib/sys/peer_registry.py`（`learn_from_identify_rsp` / `learn_from_frame` / `by_cid`） |
| 身分／方向 | `slave/action/net_actions.py`（`on_identify_req` / `on_set_master`） |
| node 狀態 | `slave/action/status_actions.py`（`0x1101`） |
| 持久化 | `slave/lib/sys/ConfigManager.py`（`load_node` / `save_node` / `clear_node`） |
| payload 定義 | `slave/schema/sys.json` |

## 角色

- **Mon＝遙控器**：發起方；本專案是**兩顆按鈕的遙控器，不是螢幕、不是顯示器**。
  對方文件若寫「螢幕／Monitor」，指的都是同一個發起方角色；UI 可以是螢幕或按鈕，
  但配對行為與 UI 無關。
- **Master＝fastLED 被控端**：LED 控制；node CID 由 MAC 末 4 hex 決定（例 `0xEB04`）。
- **Mon 的 CID 由 Mon 自己設定，可隨時修改**，不是固定值。
  `0x1016` 的 payload 就是 Mon **當下**的 CID；本文件用 `mon_cid` 表示。
- `master_cid`：Master 記住的 Mon CID（最後一次 `0x1016` 帶上來的 `mon_cid`）。
- `master_mac`：Master 記住的 Mon 射頻 MAC（`0x1016` 收幀來源）。
- `pair_claimed`：Master **本次開機**是否已被認領（只存記憶體）。

## 匯流排配對（bus）

- `add_peer(mac)` 只是**發送通道**，不是配對；收得到誰的幀與 `add_peer` 無關。
- 位址從**收幀**學來：
  - 來源 MAC → `master_mac`／peer 表。
  - `0x100E` 的 `cid` / `slave_id` → peer 的 CID／MAC。
  - `0x1016` 的 payload → Mon 的 CID；來源 MAC → Mon 的 MAC。
- 每收一幀就 `learn_peer`／更新 CID↔MAC；`resolve_addr(cid)` 找 MAC。
- 回覆：協議 `addr = master_cid`、射頻 dst = `master_mac`；未知時回退最後來源／廣播。
- 沒有額外的「bus pair」指令：`0x100D` 是 discovery，`0x1016` 是 direction。

## 探索（`0x100D`）與隨機回覆

`0x100D` payload：`reply_cid:u16`，可選 `timeout_ms:u16`、`force:u8`。

| `reply_cid` | 模式 | 行為 |
|---|---|---|
| 具體 cid | 定向點名 | **每次都回**；`0x100E` 的 addr = `reply_cid` |
| `0xFFFF` | 廣播回覆 | 同一個問的人只回第一次（記最近 4 個來源 MAC） |
| `0xFFFF` + `force=1` | 強制 | 忘掉該來源的紀錄，重新回 |

- **隨機延遲（防撞）**：`timeout_ms > 0` → 抽 `[0, timeout_ms]` 的隨機延遲才回；
  `0`／缺席 → 立即回（舊客戶端）。**只有 `0x100D` 抖動**，其他廣播都是單向。
- **非阻塞**：handler 只排程，不得 `sleep`；延遲期間匯流排要繼續跑。
- `timeout_ms` 由 Mon 指定（例 `500`；真機回覆散開在 119–395ms）。
- `0x100E`：`cid`（node CID）＋`slave_id`（MAC hex）＋`ip`（ESP-NOW 空）。
- `0x100D` **不得**改 `master_cid`；`0x100E` 是 cid 的唯一來源。
- 多台可能同時回；Mon 在視窗內收集，再選 target。

## 完整配對流程

1. **Mon 開機**：套用自己的 MAC（own/spoof/ff）、選定 `mon_cid`（可隨時改）、設頻道。
2. **探索**：Mon 廣播 `0x100D{reply_cid, timeout_ms, force}`；Master 在隨機 `[0, timeout_ms]` 後回 `0x100E{cid, slave_id}`。多台可能回；Mon 在視窗內收集，選定 `node_cid`／MAC。
3. **讀狀態**：Mon 送 `0x1101{node}`；Master 回 `0x1102{bound, master_cid, master_mac}`。
4. **判斷**：
   - `bound=false` → 5. 綁定。
   - `bound=true` 且 `master_mac` = 自己 → 已綁定，跳 6。
   - `bound=true` 且 `master_mac` ≠ 自己 → 先解除（7）或重啟 Master（9）；不能直接搶。
5. **綁定**：Mon 送 `0x1016{mon_cid}`（單播或 FF）。Master 記 `master_cid=mon_cid`、`master_mac=來源`、`pair_claimed=true`、落盤。`0x1016` 無 ACK。
6. **確認**：Mon 再送 `0x1101 node`；`master_cid=mon_cid`、`master_mac`=自己、`bound=true` 才算成功。之後送 `0x3105/0x3103/0x3101`；Master 只接受已配對 Mon。
7. **解除**：任何 Mon 送 `0x1016{0xFFFF}`（單播或 FF）。Master 清 `master_cid/master_mac/role`、`pair_claimed=false`、落盤；Mon 用 `0x1101` 確認 `bound=false`。
8. **換手**：解除後，新 Mon 送 `0x1016{新 mon_cid}` 即可；不必重啟。
9. **重啟**：`master_cid/master_mac` 保留、`pair_claimed` 歸零 → 新 Mon 可直接綁；舊記錄被新綁定覆蓋。

```text
Mon（遙控器）                         Master（fastLED）
  |-- 0x100D 廣播 ------------------->|
  |<-- 0x100E {cid, slave_id} --------|
  |-- 0x1101 {node} ----------------->|
  |<-- 0x1102 {bound, master_mac} ----|
  |-- 0x1016 {mon_cid} -------------->|  單播或 FF；無 ACK
  |-- 0x1101 {node} ----------------->|
  |<-- 0x1102 {master_cid=mon_cid} ---|
  |-- 0x3105 / 0x3103 / 0x3101 ------>|
  |<-- 0x3104 / 0x3102 ---------------|
  |-- 0x1016 {0xFFFF} --------------->|  任何 Mon；單播或 FF
  |-- 0x1101 {node} ----------------->|
  |<-- 0x1102 {bound=false} ----------|
```

| 目前狀態 | 下一個動作 |
|---|---|
| `bound=false` | `0x1016{mon_cid}` 綁定 |
| `bound=true`，`master_mac`=自己 | 直接控制（或 `0x1016{mon_cid}` 確認） |
| `bound=true`，`master_mac`≠自己 | `0x1016{0xFFFF}` 解除，或重啟 Master |

## CID 配對：綁定 `0x1016{mon_cid}`（`mon_cid != 0xFFFF`）

- 尚未綁定：接受；記 `master_cid = mon_cid`、`master_mac = 來源 MAC`、
  `pair_claimed = true`；落盤。
- 已綁定、同 CID：視為確認（冪等）；必要時更新 `master_mac`；不重寫 flash。
- 已綁定、不同 CID：**忽略**（先綁先贏）。
- 位址：單播或 **FF 廣播**都必須接受。

## 解除：`0x1016{0xFFFF}`

- **任何來源**都可解除；**單播或 FF 廣播都接受**。
- 位址：射頻 dst = `FF:FF:FF:FF:FF:FF`（廣播）或單播；NC4 `addr = 0xFFFF`（廣播）或 `node_cid`（單播）。
- 不得因為 `broadcast` 拒絕；`apply()` 的 owner 檢查也要 bypass（直接 `monPairingClear()`）。
- 清空 `master_cid`、`master_mac`、`role`；`pair_claimed = false`；落盤。
- 解除後，任何 Mon 可**立即**重新綁定；不需要重啟。
- 本來就未綁定 → 直接成功，不寫 flash。

> `0xFFFF` 原本只是「未設定」哨兵；現行主線自 `03b12615`（2026-10-04）起才把它
> **明確定義為解除**：清除 `master_mac`／`role`、`pair_claimed=false`、落盤。
> 只把 `master_cid` 設回 `0xFFFF` 而沒有清除，不算解除。

## 重啟

- `master_cid` / `master_mac` 保留（回覆用）。
- `pair_claimed` 歸零 → 重啟後新的 Mon 可直接綁定。
- **重啟不是換手的必要條件**：正常換手用 `0x1016{0xFFFF}` 解除即可。
- fastLED 不需要 MPY 那種「重啟清 residue」：`pair_claimed` 是普通 RAM 變數，
  開機初始化 `false`、解除時設 `false` 即可；重啟只是驗證它**不落盤**。

## 其他

- `0x100D` 只回身分，**不得**改配對。
- `0x1016` 沒有 ACK；用 `0x1101 node` 的 `master_cid` / `master_mac` / `bound` 驗證。
- 控制（`0x3105` / `0x3103` / `0x3101`）只接受已配對 Mon 的來源 MAC。

## 驗收

1. 已配對，別的 Mon 單播 `0x1016{0xFFFF}` → `bound=false`。
2. 已配對，FF 廣播 `0x1016{0xFFFF}` → `bound=false`。
3. 解除後，新 Mon 單播 `0x1016{mon_cid}` → `master_cid = mon_cid`、`bound=true`。
4. 重啟 Master 後（驗證 `pair_claimed` 不落盤），新 Mon 單播 `0x1016{mon_cid}` → 綁定成功。
   ※ 這不是要求每次換手都重啟；正常換手用 #3 的解除。
5. `0x100D` 掃描後 `master_cid` / `master_mac` 不變。
6. `0x100D{timeout_ms=500}`：回覆分散在 0–500ms（不是全部同時到）。
7. 同一來源重複 `0x100D`（`reply_cid=0xFFFF`、無 `force`）→ 只回第一次；`force=1` → 再回。
8. `reply_cid=具體 cid` → 每次都回。
