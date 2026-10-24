# NC4 匯流排模擬器（00_nc4_illustrated.html）稽核與修正紀錄

> 🗄️ **歷史紀錄（2026-10-24 標記，不再更新）**：本稽核是 2026-10 當時的快照；
> 下方「程式端待辦」請以現行程式碼為準（`MODE_STOP` 已由 `pixel_control_panel` 實作）。

> **用途**：`doc/00_nc4_illustrated.html` 的分頁 2「匯流排模擬器」是**照協議文件**
> （`doc/01_protocol/`＋`slave/schema/*.json`）實作的教學沙盤。這份記下 2026-10 那次
> 逐指令稽核的**全部發現**：哪些是模擬器自己的錯（已修）、哪些是**程式還沒跟上協議**
> （未修，屬韌體待辦）、以及協議文件自身的矛盾。
> **狀態**：🟢 模擬器端已修完並通過 73 項回歸；🔴 **程式端 5 處回覆位址 + MODE_STOP
> 暫停語意仍是待辦**（刻意不在這次動韌體）
> **最後更新**：2026-10
> **相關**：`doc/00_nc4_illustrated.html`、`doc/01_protocol/01_nc4_protocol.md` §定址模型、
> `doc/01_protocol/02_command_index.md`、`doc/01_protocol/04_pixel_protocol.md`、
> `test/protocol/sim_doc_selftest.js`

---

## 0. 一句話

**模擬器要反映的是協議文件，不是現在的程式碼。** 稽核出來的差異因此分成兩類：
「模擬器寫錯協議」→ 這次修掉；「程式沒實作協議」→ 列在 §3 等韌體排程
（`0x3106` 的 `action` 就是後者，使用者已確認是「還來不及改程式」）。

---

## 1. 裝置 ID：三台 cid 互不相同

模擬器原本把 **Master 和 Slave A 都當成 `0x0001`**，於是「已配對 → 回覆寄 0x0001」時，
位址 `0x0001` 同時是節點 A 的 cid，會出現「位址是 0x0001，不是它 → 丟棄」這種自相矛盾的說明。

| 裝置 | cid | 對齊的實機 |
|---|---|---|
| Master／面板 | `0x0001` | `ports/S3/ESP32-S3-Control_Panel_V2/config.json`（`cID:"0001"`, hostname `s3-cpanel`） |
| 節點 燈條 A | `0x0002` | `ports/S3/ESP32-S3-Test_Peer/config.json`（`cID:"0002"`） |
| 節點 燈條 B | `0x0003` | —（新號） |

同時修掉根源：`master_cid` 從「一個全域值」改成**每台節點各自持有**
（`SLAVE_DEFAULTS.masterCid`）。`0x1016 SET_MASTER` 只改「收到那一幀的那一台」——
這才是協議寫的「告知對方『你的 master 是我』」。
「⑤ 線路狀態」面板也從兩顆全域按鈕改成**逐台配對列**（`0x0002` / `0x0003` 各一組）。

> 實機的 cID 由 `ConfigManager.ensure_cID()` 於 T0 用 `machine.unique_id()` 末 4 碼
> 自動配發（`slave/config.json` 的 `cID:""`），所以量產不會撞號；`0x0001` / `0x0002`
> 是**角色固定**的板子（面板／測試節點）才寫死。

---

## 2. 模擬器端：已修正清單

### 2.1 邏輯（會產生錯的位元組或錯的結論）

| # | 問題 | 修正 | 依據 |
|---|---|---|---|
| 1 | 抖動用 `spread/(n+1)*(idx+1)*rand` → **保證時槽不重疊**，narration 還寫「不撞線」 | 每台**獨立均勻亂數**抽 `[0, timeout_ms]`；抽到差 < 3ms（≈ 一個幀的傳輸時間）就**照實顯示碰撞** | `02_command_index.md`：`timeout_ms`=「隨機延遲 [0,timeout_ms]」 |
| 2 | `0x3106 action` 兩顆按鈕行為相同、標籤卻寫「暫停」 | `action=0` → 暫停（保留模式狀態、燈熄）；`action=1` → 全關（清燈、`mode→0`） | `04_pixel_protocol.md` §2.5 |
| 3 | `0x3105 brightness=255` 被當成「設成 255」 | 255 = **不改**（沿用目前值）；`0` 是合法值（最暗） | `04_pixel_protocol.md` §2.4 |
| 4 | `0x3102 mode_type` 寫死 `1`，過濾也用 `m.type===1` | **回音 query 的 `mode_type`**，過濾改用 `req.mode_type` | `04_pixel_protocol.md` §2.2 |
| 5 | `0x1011`/`0x1018` 把 `action` 當答案（查詢 → 一律回 `enabled=0`） | `action` 0=查／1=開／2=關；查詢回**當下狀態**（WebREPL 開機預設開 → `enabled=1`） | `02_command_index.md` 0x1010/0x1017 |
| 6 | `0x1016` 一次改**全部**節點，且寫死 `0x0001` | 只改 `hit`（收到那一幀的節點），值取自 payload | `02_command_index.md` 0x1016 |
| 7 | 廣播回覆時，其他節點被標成「不是給我的 → 丟棄」 | 廣播幀 → 「位址過濾放行；誰有 handler 誰才會動」 | `app.handle_stream` 的 ADDR 過濾語意 |
| 8 | `req.mt/mid/replyCid/enab` **寫死 1** | 全部由 `def.fields` 推導（schema 欄位順序 = wire 順序） | `slave/schema/*.json` |
| 9 | `0x1102` 的 JSON 被 `clip(...,120)` 截斷 → **非法 JSON**（實測第 160 字元爆） | 送**完整可解析**的 JSON；要縮小範圍請用 `keys` | `02_command_index.md` 0x1102 |

### 2.2 回覆內容

| 指令 | 舊（錯） | 新（依協議／schema） |
|---|---|---|
| `0x100E` / `0x1015` 的 `ip` | 純 IP 字串 | **多介面 JSON**（只列有 IP 的介面） |
| `0x1006 tasks_json` | 6 個名字的陣列 | `{boot_phase, boot_layer, boot_done, tasks:[{name,layer,affinity,running_core,running}]}` |
| `0x1102`（keys 空） | 7 個 config 路徑 | 內建 ＋ provider metrics（`slave_id/mem_free/fps/frame_count/…`） |
| `0x1102`（指定 keys） | 回固定 7 個 key | **只回要求的那幾個**；找不到的集中 `_unknown` |
| `0x1102` 的 `Pixel.*` | 當成存在的值 | 移除（schema 沒有這個 key；`node.master_cid` 改回**整數**） |
| `0x1104 message` | echo 請求 JSON | 逐 key 結果說明（`寫入快取 N 個 key; 存檔 N 個 key`） |
| `0x3108 total_ms` | 保留（協議說「模式總長」） | 不變 —— **程式**寫死 0，列 §3 |
| `0x1013 ok` | 保留（模擬成功案例） | 不變 |

### 2.3 版面（由上往下：怎麼用 → 指令記錄區 → 模擬器）

第一次進來的人原本要自己拼：「這是什麼」→（很長的控制盤）→ 拓撲 →（最底部才看到）流量記錄。
改成**三段式**，照使用順序排：

| 區 | 內容 | 為什麼在這個位置 |
|---|---|---|
| **01 怎麼用：三個動作就好** | 三張卡（選發送目標／按一顆指令／回頭看結果）＋「兩個小玩法」（點板子斷電、點線拔線） | 一進來就先給最少可動的資訊；區塊內有錨點連結可跳段 |
| **02 指令記錄區** | 流量記錄表（**固定高度、框內滾動**）＋ 四格統計（＋ `清除紀錄`） | 先讓使用者知道「按下去會留下什麼」，且紀錄就在操作區**上方**，不用滑到頁尾 |
| **03 模擬器** | 三台位址說明 → 發送目標 → 控制盤（左）＋ 拓撲（右）→ 操作提示 → 「這個模擬器要你看見的事」 | 實際操作區；`⟲ 重設` 黏在拓撲工具列上，播放時永遠看得到 |

> 元素 id 全部沿用（`simTbody` / `targetPick` / `topo` …），只換 DOM 順序 —— JS 用
> `getElementById`，所以不受影響；改完 73 項自我測試仍全過。

**記錄表的高度**：紀錄是 append-only，不限制高度的話 24 次指令就有 48 列、把整頁撐長。
`.logscroll` 給 `max-height:min(70vh, 700px)` ＋ `overflow-y:auto` ＋
`overscroll-behavior:contain`（滾到底不會帶著整頁跑），`thead th` 用 `position:sticky`
讓欄位名在框內滾動時一直看得見；最新的一列在最上面（`tbody tr:first-child` 淡底標示）。

高度調過一次：`48vh / 460px` → **`70vh / 700px`**（第一版太矮，實測只看到 3～4 列）。
實測（1400×1000 視窗、24 次指令 = 48 列）：
`rows=48 box=598/5117 page=1894`，`ovf=auto`、`sticky=sticky`
—— 48 列全在、整頁仍只有 1894px（不限高的話約 7000px）。

> 列的實際高度差很多（`說明` 欄只有 130px 寬，長句會折成 5～6 行 → 該列 ~145px；
> 短句列 ~78px），所以「看得到幾列」是 4～8 列在跳。要再擠更多列，
> 可以考慮把 `說明` 欄加寬（130px → 240px）讓長句少折幾行 —— **未做**，屬另一個決定。

### 2.4 紀錄與 UI

- **流量記錄改成 append-only 的 session 紀錄**：移除「只留 40 列」的截斷；
  時間軸（timeline）與紀錄**刻意分開** → 按 `⏮ ◀ ▶` 回放**不會**刪掉任何一列、
  也不會重算統計。統計（Master 發出／Slave 回覆／碰撞／被丟棄）同樣改成整場累計。
- 新增 **`⟲ 重設`**（黏在舞台工具列，紅框）＝清紀錄＋統計＋時間軸＋裝置狀態＋接線／電源／配對；
  以及流量表標頭的 **`清除紀錄`**（只清紀錄＋統計，裝置狀態不動）。⑦ 情境的「重置」改名「全部重設」。
- 流量表標頭顯示 **`N 列 · 全部保留`** 計數。
- ③ 系統／身分 `12 條` → **10 條**（實際按鈕數）；`0x1016` 從②移到③（②只剩真正不回覆的）；
  兩顆 `0x1103` 移到④；① 標題改「目標由上方選擇器決定」；移除從未被讀取的 `addr:` 死資料。
- §04 黏包拆包模擬器的紀錄也改成不截斷（面板本身可滾動）。
- 修掉 `id="simReset"` **重複**（§04 與 §07 撞號 → 新鍵改名 `simResetAll`）。
- 碰撞列不再顯示「← 0x0000」（碰撞沒有單一來源）。

---

## 3. 程式端待辦（**未動韌體**，協議為準）

| # | 位置 | 現況 | 應為 |
|---|---|---|---|
| 1 | `slave/action/pixel_actions.py:35` | `Proto.pack(rsp, payload)` → `ADDR=0xFFFF`（廣播） | `addr=bus.master_cid` |
| 2 | `slave/action/status_actions.py:227`（`0x1102`） | 同上 | 同上 |
| 3 | `slave/action/status_actions.py:247`（`0x1104`） | 同上 | 同上 |
| 4 | `slave/action/sys_actions.py:188`（`0x1006`） | 同上 | 同上 |
| 5 | `slave/action/sys_actions.py:213`（`0x100B`） | 同上 | 同上 |
| 6 | `slave/lib/sys/global_mode.py` `stop_mode(action)` | **不看 `action`**，兩者都是全停 | `0=暫停`（保留模式狀態）／`1=全關` |
| 7 | `slave/action/pixel_actions.py:137` | `total_ms` 寫死 `0` | 回該模式總長 |
| 8 | `0x1011`/`0x1018` 的 `info` 字串 | `listening 0.0.0.0:8266` / `web_ui affinity=(…)` | 與協議描述對齊（或更新文件） |

> 影響（為什麼 1–5 不是美觀問題）：`addr=0xFFFF` 是**廣播幀**，位址過濾放行 →
> 線上每一台都會處理它。例如不是 local provider 的板子會把別人的 `0x3102`
> 吃進自己的模式表（`on_mode_list_rsp`）；ESP-NOW 上更會從 unicast 變成 `BCAST_MAC`
> （`now_bus.resolve_addr` 第一條就攔 `0xFFFF`）。
> 協議寫的是「slave 記住後，**所有回應的 `addr` 欄位預設都填 `bus.master_cid`**」
> （`01_nc4_protocol.md` §定址模型）。

---

## 4. 協議文件自身的矛盾（未改，屬對外契約）

`doc/01_protocol/04_pixel_protocol.md`：

1. **§2.2 標題句**寫「列表只含 ID + 總時間（每筆固定 **6 bytes**）」，
   但同一段的 `entry` 定義與 `slave/schema/pixel.json` 都是 **2 bytes（u16 模式 id）**。
   模擬器照 schema（2 bytes）→ 建議把那句改成 2 bytes、並移除「+ 總時間」。
2. **§2.3** 寫 `total_ms`「= `MODE_LIST_RSP.total_ms`」，但 `0x3102` 沒有這個欄位
   → 應改為「該模式的總長（0 = 不設限）」。

---

## 5. 驗證方式（可重跑）

```bash
# 語法
python3 -B -c "import re;s=open('doc/00_nc4_illustrated.html',encoding='utf-8').read();open('/tmp/s.js','w').write(re.search(r'<script>(.*)</script>',s,re.S).group(1))"
node --check /tmp/s.js

# 逐指令回歸（73 項）：DOM shim 跑真的 busSim + 獨立解碼器驗每個封包
node test/protocol/sim_doc_selftest.js
```

> ⚠️ **`.gitignore:229` 把整個 `test/` 排除**，所以 `test/protocol/sim_doc_selftest.js`
> 目前**只存在本機、不會進版控**（和 `test/` 裡其他檔案一樣）。要讓它跟著 repo 走，
> 得搬家（例如 `tools/PC/`）或在 `.gitignore` 開例外 —— 未定，先照原政策。

`test/protocol/sim_doc_selftest.js` 涵蓋：三台 ID 互異、每個指令的 payload 欄位順序、
每個回覆的 cmd/addr/內容、`0x1016` 只改被定址那台、**7 條回覆路徑都依各自的
`master_cid`**、抖動是機率性的（400 次抽樣的碰撞率）、紀錄只增不減（30 次指令 > 40 列、
回放不刪列、清除才歸零）、以及**全部指令 × 全部目標 = 119 個封包的 SOF/VER/LEN/CRC32**。

最後一次結果：**73 通過 / 0 失敗**（碰撞率約 7%，符合 50ms 視窗的預期量級）。
