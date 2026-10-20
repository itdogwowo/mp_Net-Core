# Mon ↔ Master 配對合約（fastLED 必須跟隨）

> **狀態**：定案（2026-10-20）
> **權威**：本文件以 **mp_Net-Core 的 Mon（遙控器／發起方）規格為準**。
> fastLED 的 `mon_pairing` 實作必須符合本文件；不符合即為 bug。
> **參考實作**：`slave/action/net_actions.py` 的 `on_set_master()`（L408 起）、
> `doc/03_notes/01_changelog.md` §31–32、`doc/02_guides/18_node_discovery.md`。

---

## 0. 名詞（先統一，避免「Master」兩邊相反）

| 名詞 | 定義 |
|---|---|
| **Mon** | 遙控器／發起方；NC4 的 **master**（本文件的主體） |
| **Master** | fastLED 受控節點（LED 控制端；NC4 的 slave） |
| **`node_cid`** | Master 自己的 node CID＝STA MAC 末 4 hex（例：`a0:f2:62:ea:eb:04` → `0xEB04`） |
| **`master_cid`** | Master 記住的 **Mon CID**（本專案 `0x0101`） |
| **`master_mac`** | Master 記住的 **Mon 射頻 MAC**（取自 `0x1016` 收幀來源） |
| **`pair_claimed`** | Master **本次開機**是否已被認領（純記憶體，重啟歸零） |

> Mon 的 CID 是本專案設定的 `0x0101`；`0x0001` 只是舊 UART／舊面板 endpoint，不可混用。

---

## 1. 定址與身分

1. Mon 的 CID 由 Mon 設定（本專案 `0x0101`），**不是** MAC 推導。
2. Master 的 `node_cid` = STA MAC 末 4 hex。
3. `0x100D IDENTIFY_REQ` 只回身分：
   - `0x100E{node_cid, slave_id=MAC hex, ip=""}`。
   - **掃描不得改變配對方向**（不可把 `reply_cid` 寫進 `master_cid`）。
4. `0x1016 SET_MASTER` 是**唯一**會改 `master_cid` / `master_mac` 的指令。

---

## 2. `0x1016` 行為（MUST）

### R1. 唯一方向寫入者
只有 `0x1016` 能改 `master_cid` / `master_mac`；`0x100D`、`0x3101`、`0x3105` 都不得改。

### R2. 接受單播與廣播
- **單播**：射頻 dst = Master MAC、`addr = node_cid`。
- **廣播**：射頻 `FF:FF:FF:FF:FF:FF` 或 `addr = 0xFFFF`，**必須接受**。
- 廣播語意＝一次認領／解除一群；不得以 `broadcast` 為由拒絕。

### R3. 解除（`master_cid = 0xFFFF`）
收到後**無條件**：
- `master_cid = 0xFFFF`
- `master_mac = None`
- `role = None`
- `pair_claimed = False`（重新開放認領）
- 若本來就未配對 → 不寫 flash、直接成功。

**任何來源都可以解除**；不看 owner、不看 `pair_claimed`。

### R4. 認領（`master_cid != 0xFFFF`）
- 若 `pair_claimed == True` 且目前 `master_cid != 新 cid` → **忽略**（不換手）。
- 若同一個 `master_cid` 重複送 → **冪等確認**；`master_mac` 更新為本次來源 MAC；
  值沒變就不寫 flash。
- 否則接受：
  - `master_cid = payload`
  - `master_mac = 收幀來源 MAC`
  - `role = "slave"`
  - `pair_claimed = True`
  - 落盤（NVS/btree）。

### R5. 持久化
- `master_cid` / `master_mac` **落盤**，重開機由 `load_node()` 還原（回覆仍寄給它）。
- `pair_claimed` **只存記憶體**，每次開機歸零。
- 落盤失敗不得宣告成功；值沒變不寫。

### R6. 重啟可換手
`pair_claimed` 開機歸零 → **重啟後新的 Mon 可以直接認領**，不必先解除。
（與「先解除再換手」兩條路都必須成立。）

### R7. 直接換手
未解除、未重啟時，A → B 直接換手 **必須被忽略**（防無聲搶奪）。
A → 解除 → B，或 A → 重啟 → B，必須成功。

### R8. 無 ACK
`0x1016` 不回 ACK。驗證方式：
`0x1101 STATUS_GET{keys="node"}` → `0x1102` 的 `master_cid` / `master_mac` / `bound`。

---

## 3. 回覆與控制

1. 回覆 `addr = master_cid`、射頻 dst = `master_mac`。
2. `0x1102` 回**請求者**（讓未配對 Mon 也能讀 node，判斷是否配對）。
3. 控制（`0x3105/0x3103/0x3101`）只接受**已配對 Mon 的來源 MAC**；
   未配對可查身分／狀態，但不得控制。
4. `0x3102 MODE_LIST_RSP` 兩種格式：
   - `addr = node_cid`（無線定址 Master）→ 每筆 **2 bytes** `(type<<8)|id`。
   - `addr = 0x0001`（舊 Mon／UART endpoint）→ 每筆 **6 bytes** `type,id,total_ms`。
5. `0x3103/0x3104` 回已配對 Mon；`0x3105` 不需 ACK。

---

## 4. fastLED 現況 vs 本合約（必須修改）

| 項目 | fastLED 現況 | 本合約要求 |
|---|---|---|
| 解除來源 | `MonBinding::apply()` 只允許 owner | **任何來源**可解除（R3） |
| FF 廣播 | `if (broadcast \|\| addr != nodeCid) return false;` | **必須接受廣播**（R2） |
| 重啟換手 | NVS 持久化，重啟仍只認原 owner | `pair_claimed` 記憶體、重啟歸零（R5/R6） |
| 直接 A→B | 被 owner 檢查擋 | 被 `pair_claimed` 擋（R7） |
| 廣播認領 | 拒絕 | 接受（R2） |

> 現行 fastLED 的 owner-only／NVS／FF 拒絕是**較嚴的模型**，但**不是本專案規格**。
> 請依本文件修改；安全性由「`pair_claimed` 擋直接換手」承擔，而不是 owner 檢查。

---

## 5. 驗收測試（fastLED 必須全過）

| # | 步驟 | 期望 |
|---|---|---|
| T1 | 未配對，Mon `0x1016{0x0101}` 單播 | `master_cid=0x0101`、`master_mac=Mon MAC`、`bound=true` |
| T2 | 已配對，另一 Mon `0x1016{0x0102}` 單播 | 忽略，狀態不變 |
| T3 | 已配對，**另一 Mon** `0x1016{0xFFFF}` 單播 | 解除，`bound=false`、`pair_claimed=False` |
| T4 | 已配對，`0x1016{0xFFFF}` **FF 廣播** | 解除（R2/R3） |
| T5 | 已配對，重啟 Master，另一 Mon `0x1016{0x0102}` | 接受（`pair_claimed` 已歸零） |
| T6 | `0x100D` 掃描 | `master_cid`/`master_mac` 不變 |
| T7 | `0x1016` 後讀 `0x1101 node` | 反映新值；無 ACK 不影響 |
| T8 | `0x3101` 用 `addr=node_cid` / `0x0001` | 2-byte / 6-byte 格式正確 |

---

## 6. 參考

- 參考實作：`slave/action/net_actions.py` `on_set_master()` L408–500
  （解除不看 `_claimed`、`pair_claimed` 記憶體、`master_mac` 取來源）。
- 設計脈絡：`doc/03_notes/01_changelog.md` §31–32（含「換 master 不必重啟」實測）。
- 節點發現：`doc/02_guides/18_node_discovery.md`（身分 ≠ 方向、無聲搶奪）。
- fastLED 現行：`firmware/shared/src/transport/mon_pairing.cpp`、
  `firmware/shared/include/transport/mon_pairing.h`、
  `docs/communication/external_uart/Mon_ESP_NOW_pairing.md`。
