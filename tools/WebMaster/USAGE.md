# NetBus WebMaster 使用教學

操控 mp_Net-Core 設備（NC4 協議）的網頁控制台。設備透過 WebSocket 連入，
瀏覽器端做串流控制、檔案管理、遠端命令。

---

## 1. 一鍵啟動

```bash
cd tools/WebMaster
python launch.py            # 預設 port 8000
python launch.py 9000       # 自訂 port
python launch.py --no-browser   # 不自動開瀏覽器
```

`launch.py` 會自動：
1. 建立虛擬環境 `.venv`（若無）
2. 安裝 `requirements.txt` 的模組
3. 啟動伺服器
4. 2 秒後開瀏覽器到 `http://127.0.0.1:<port>/`

Windows 也可直接雙擊 `launch.bat`。

> 若 venv 建立/裝模組失敗，會自動改用「目前 Python」（需已裝 fastapi/uvicorn/websockets）。

---

## 2. 設備怎麼連進來

設備（slave）韌體會**主動連到 master 的 WS**：

```
ws://<master_ip>:<port>/ws/<slave_id>
```

所以設備 config 的 `master_IP`、`master_port` 要指到這台機器。設備開機或收到
DISCOVER 敲門時會自動連入。連上後左側「設備」清單會出現。

設備的 `master_port` 要對上 `launch.py` 用的 port（預設 8000）。

### 設備概況（左側「設備概況」面板）

左側欄可**摺疊/展開**（topbar 的 ☰）。

- **預設只顯示連線設備**：綠點 = 在線，可點選操作（顯示 IP + 上線秒數 + PlayID）。
- **顯示離線/已知設備**：勾選「顯示離線/已知設備」後，會列出所有曾連過或
  `slave_map.json` 有紀錄的設備（紅點 = 離線，只能看狀態、不能操作），依 `slave_id` 排序。
- 每台線上設備右側有 **⚙ 設定** 按鈕：

「已知設備」來源：`slave_map.json`（NetBusMaster 的紀錄）作為種子 + WebMaster
跑起來後連線過的設備會自動累積（存到 `tools/WebMaster/devices.json`）。

#### 發現 / 敲門

- **🔍 發現（廣播）**：送 DISCOVER (0x1001) 到 `255.255.255.255` + 子網廣播 → 所有網路上的
  設備會依 `ws_url` 連回本 master。
- **固定 IP 敲門**：輸入框填 IP（多個以逗號分隔，如 `10.161.185.89,10.161.185.22`）→ 按「連」
  → unicast DISCOVER 到指定 IP:9000。

#### 設定（⚙）

點線上設備的 ⚙ 或 topbar 的 ⚙，開啟設定視窗：

- **config.json**：按「下載」取得設備 config → 在文字框編輯 → 「上傳」寫回設備。
- **設備操作**：軟重啟(0x100F) / 重掃檔案(0x200B) / 列出待確認(pending)。

---

## 3. 介面說明（分頁）

左側 = **設備導航**（發現/敲門 + 上線 slave 清單；揀一台做目前操作對象）。
每台 slave 有自己嘅 **profile**：`config.json`、delta（`/sd/.delta.json`）、
manifest（`/manifest.json` 本地 + `/sd/.manifest.json` SD），喺「⚙ 設備」tab 睇。

右側 tabs（按功能分組）：

### ⚙ 設備（per-slave profile + 維護）

- **設備概況**：PlayID / IP / 狀態（喺左側揀邊台就跟住變）。
- **config.json**：按「下載」取設備 config → 編輯 → 「上傳」寫回。
- **delta / manifest**：按鈕讀 `/sd/.delta.json`、`/manifest.json`、
  `/sd/.manifest.json` 內容。
- **設備操作**：軟重啟(0x100F) / 重掃檔案(0x200B) /
  **重建索引·本地**（剷 `/manifest.json` + 重啟，開機自動重掃）/
  **重建索引·SD**（0x200B target=1，背景掃描重建全表）/
  列出待確認（delta）。

### 🎛 播放

| 欄位 | 說明 |
|---|---|
| 檔案 / 緩衝區 | 播放嘅資料檔（如 `/ram/live.bin`、`data.bin`） |
| Play Mode | 0=播放一次 / 1=循環 |
| FPS | 渲染幀率 |
| 起播幀 | 從第幾幀開始 |

按鈕：**準備 / 播放 / 暫停 / 停止 / 跳轉**。

下方「RAM 實時播放」：選檔案、填緩衝區路徑、上傳到 RAM 後可直接串流。

### 📁 檔案

- **上傳檔案**：選本地檔案 + 填遠端路徑 → 上傳（進度條顯示）。
- **下載**：喺「要下載嘅路徑」填路徑 → 下載。
- **列出**：列出設備 manifest 嘅所有檔案（路徑/大小/狀態）。
- 每個檔案有按鈕：
  - **下載**：抓返本地。
  - **待確認**（黃色徽章）：寫入 root 已留 `.bak` 但未 confirm。
    - **確認**：正式生效（刪 `.bak`、清 pending）。
    - **還原**：回滾到 `.bak` 舊版。
  - **刪除**：刪除該檔。

> 上傳同名檔會自動兩段式 commit（留 `.bak` + pending）。冇 confirm 嘅話
> 設備 3 次重啟會自動回滾舊版——見到「待確認」要記得處理。

### 🔥 固件

固件全量更新（delta）：比對本地 `slave/` 與設備 manifest，**只上傳差異檔**。

- **預覽差異**：先 `dry_run=1`，只列出差異，唔上傳。
- **執行更新**：上傳所有差異檔。選項：上傳後確認（預設勾）、上傳後重啟。
- 結果顯示上傳咗幾檔、清單。

> 只有「本地 `slave/` 有、且 sha 與設備 manifest 不同」嘅檔先會上傳，
> 反覆執行唔會重複推相同內容。

### 🔌 PoE

交換器 PoE 電源控制（Cisco 3560，重用 `tools/PC/poe_restart.py`）：

- **動作**：重啟（斷電→等待→恢復）/ 關閉 PoE（只斷電）/ 開啟 PoE（只供電）。
- **交換器**：Light-SW-01 / Light-SW-02（可多揀；空 = 兩台都要）。
- **Port**：空 = 全部 1-45；或自訂 `3,5,10-15`。
- **Dry-run**（預設勾）：只預覽指令，唔會真斷電。要真做記得取消勾選
  （需主機裝 `netmiko`）。

### ⌨ Console

通用命令探險家：揀任意 NC4 命令 → 填參數 → 送出 → 睇回應。

- **cmd_id**：從 schema 下拉（含 0x2005 FILE_QUERY、0x1101 STATUS_GET…）。
- **args**：JSON 參數（揀命令後自動填範例）。
- **expect**：期待嘅回應命令（可空）；填咗先會等回應。
- **timeout**：等待秒數。

適用於試任何協議功能、偵錯。

### 🐍 ViperIDE（燒錄 bin / 上傳項目檔案）

內嵌官方 ViperIDE（MicroPython IDE，vendored 在 `tools/WebMaster/viper-ide/`，MIT）：
直接跟板子 USB 對話，適合 **開發/維修期** 的板端操作（不經 NC4 網路協議）。

- **連線**：板子用 USB 插在「開瀏覽器的電腦」→ 點 ViperIDE 工具列 USB 圖示選 port。
  - 一定要用 `http://127.0.0.1:<port>`（或 https）開啟 WebMaster —— WebSerial/WebUSB
    需要 secure context，`http://<LAN IP>` 不行。
  - 瀏覽器限 Chrome / Edge（Chromium）。
- **燒錄 bin**：USB 連上後，檔案樹/工具列進入 Flash 流程，選官方或**自訂 `.bin`** 直接燒
  （含 erase flash 選項；燒錄中別斷電）。
- **上傳項目檔案**：連上後直接把整包專案/資料夾拖進檔案樹即上傳（也可逐一編輯 `.py`、
  跑 REPL、用 package manager 裝 micropython-lib）。

> 這分頁跟本機 NC4 網路通道無關 — 它是板子的**另一條開發通道**。板子量產後走
> 「📁 檔案 / 🔥 固件」分頁的 NC4 更新；要重新燒錄/救磚才需要 USB + 本分頁。

**魔改**：ViperIDE source 就在 `tools/WebMaster/viper-ide/src/`（含中文可改翻譯、UI、連接層）。
改完執行 `tools/WebMaster/viper-ide/rebuild.bat`，重新整理 WebMaster 頁面即生效。
`build/` 不存在時 `/viper/` 不掛載，分頁內會顯示建置提示。

### 📋 終端

即時日誌：連線狀態、指令結果、錯誤等。可清除。

---

## 4. 常用操作範例

### 上傳一個檔案到設備

```bash
# 直接呼叫 REST
curl -X POST "http://127.0.0.1:8000/api/upload/SLAVE_ID?remote_path=/sd/data.bin&chunk_size=4096" \
     --data-binary @local.bin
```
或用 UI「檔案」分頁上傳。

### 下載設備檔案

```bash
curl "http://127.0.0.1:8000/api/download/SLAVE_ID?path=/boot.py" -o boot.py
```

### 查設備狀態

Console 分頁：cmd_id=`0x1101`，args=`{"query_type":0}`，expect=`0x1102`。

---

## 5. REST API 列表

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/` | SPA 頁面 |
| GET | `/viper/` | 🐍 ViperIDE（`tools/WebMaster/viper-ide/build` 存在時） |
| GET | `/api/viper` | ViperIDE 分頁狀態（built / version） |
| GET | `/api/devices` | 所有已知設備概況（依 id 排序，含 online 狀態） |
| GET | `/api/commands` | 所有 NC4 命令（cmd_id + 名稱 + 參數） |
| POST | `/api/upload/{sid}?remote_path=..&chunk_size=..` | raw body 上傳 |
| GET | `/api/files/{sid}` | 設備 manifest 檔案清單 |
| GET | `/api/download/{sid}?path=..` | 下載檔案（attachment） |
| POST | `/api/delete/{sid}?path=..` | 刪除檔案 |
| POST | `/api/confirm/{sid}?path=..` | 確認覆蓋（刪 .bak） |
| POST | `/api/undo/{sid}?path=..` | 復原（.bak 蓋回） |
| POST | `/api/promote/{sid}?path=..` | /sd 暫存 → root |
| POST | `/api/firmware/{sid}?dry_run=..&confirm=..&reboot=..` | 固件 delta 更新 |
| POST | `/api/knock?broadcast=1&port=..` | DISCOVER 廣播發現 |
| POST | `/api/knock?ip=10.1.2.3,10.1.2.4&port=..` | DISCOVER 定向敲門 |

### WebSocket

| 路徑 | 說明 |
|---|---|
| `/ws/{slave_id}` | slave 連入（binary NC 幀） |
| `/ws/ui` | 瀏覽器控制台（JSON） |

WS `/ws/ui` 指令範例：

```json
{"action":"stream_play","slave_id":"S1","start_frame":0}
{"action":"file_list","slave_id":"S1"}
{"action":"file_confirm","slave_id":"S1","path":"/boot.py"}
{"action":"cmd","slave_id":"S1","cmd_id":"0x1101","args":{"query_type":0},"expect":"0x1102","timeout":5}
```

---

## 6. 排障

| 症狀 | 檢查 |
|---|---|
| 設備不是線 | 設備 `master_IP`/`master_port` 有沒有指到本機 + WebMaster 的 port |
| 設備顯示離線 | WS 通道斷線（`/ws/{slave_id}` 的 finally → unregister）；master 不做「無回應」定時判定 |
| 上傳失敗 | 設備在線？`chunk_size` 合不合理？遠端路徑有無權限問題 |
| `等待確認` 一直出現 | 該檔已寫入 root 未 confirm；用「檔案」分頁按「確認」 |
| 連線/指令跳 `Connection reset` | 設備在重啟 / 看門狗 re-arm；等它穩定再操作 |
