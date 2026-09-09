# WebMaster — 網頁操作介面

操控 mp_Net-Core 設備（NC4 協議）的輕量 Web 控制台。MP3 用瀏覽器原生 `<audio>` 播放，後端零音訊依賴。

> **完整使用教學見 [USAGE.md](USAGE.md)**（一鍵啟動、設備連線、分頁操作、API、排障）。

## 啟動（推薦）

```bash
cd tools/WebMaster
python launch.py          # 預設 0.0.0.0:8000（自動建 venv + 裝模組 + 開瀏覽器）
python launch.py 9000     # 自訂 port
```

瀏覽器開 `http://<本機IP>:8000/`。

> 也可直接 `python3 -B run.py [port]`（不建 venv，須先裝好 fastapi/uvicorn/websockets）。

## 設備連線

slave 韌體會連到 master 的 `ws://<ip>:<port>/ws/<slave_id>`。所以 WebMaster 的 WS port（預設 8000）要與 slave config 的 `master_port` 一致，slave 開機或敲門時會自動連入。

## 端點

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/` | 操作台 SPA |
| GET | `/viper/` | 🐍 ViperIDE（MicroPython IDE：USB 燒錄 bin / 編輯上傳專案檔 / REPL，`tools/WebMaster/viper-ide/build` 存在時掛載） |
| GET | `/api/viper` | ViperIDE 狀態（built? / vendored version） |
| WS | `/ws/{slave_id}` | slave 連入（binary NC 幀） |
| WS | `/ws/ui` | 瀏覽器控制台（JSON） |
| GET | `/api/devices` | 在線設備清單 |
| GET | `/api/mp3` | MP3 清單 |
| GET | `/media/{name}` | MP3 檔案 |
| GET | `/api/commands` | 所有 NC4 命令（cmd_id+名稱+參數） |
| POST | `/api/upload/{slave_id}?remote_path=...&chunk_size=...` | raw body 上傳檔案 |
| GET | `/api/files/{slave_id}` | 設備 manifest 檔案清單 |
| GET | `/api/download/{slave_id}?path=...` | 下載檔案（attachment） |
| POST | `/api/delete/{slave_id}?path=...` | 刪除檔案 |
| POST | `/api/confirm/{slave_id}?path=...` | 確認覆蓋（刪 .bak） |
| POST | `/api/undo/{slave_id}?path=...` | 復原（.bak 蓋回） |
| POST | `/api/promote/{slave_id}?path=...` | /sd 暫存 → root |
| POST | `/api/firmware/{slave_id}?dry_run=..&confirm=..&reboot=..` | 固件 delta 更新（比對 slave/ vs manifest） |

## UI → WS 指令（JSON）

```json
{"action": "stream_prepare", "slave_id": "S1", "file_name": "/ram/live.bin", "play_mode": 0}
{"action": "stream_play",   "slave_id": "S1", "start_frame": 0}
{"action": "stream_pause",  "slave_id": "S1", "paused": true}
{"action": "stream_stop",   "slave_id": "S1"}
{"action": "stream_seek",   "slave_id": "S1", "frame": 0}
{"action": "stream_fps",    "slave_id": "S1", "fps": 40}
{"action": "ram_upload",    "slave_id": "S1", "remote_path": "/ram/live.bin", "data_b64": "..."}
{"action": "file_list",     "slave_id": "S1"}                                        // → {data:{path:{s,h,pending}}}
{"action": "file_confirm",  "slave_id": "S1", "path": "/boot.py"}
{"action": "file_undo",     "slave_id": "S1", "path": "/boot.py"}
{"action": "file_delete",   "slave_id": "S1", "path": "/boot.py"}
{"action": "file_promote",  "slave_id": "S1", "path": "/boot.py"}
{"action": "file_download", "slave_id": "S1", "path": "/boot.py"}                    // → {data_b64}
{"action": "cmd",           "slave_id": "S1", "cmd_id": "0x1101", "args": {"query_type": 0}, "expect": "0x1102", "timeout": 5}
```

## 測試

```bash
# in-memory 整合測試 (不需硬體 / 不需網路)
python3 -B test_webmaster.py

# 端到端 mock slave (需 pip install websockets)
python3 -B run.py 8000 &
python3 -B mock_slave.py ws://127.0.0.1:8000/ws/MOCK MOCK
```

## 🐍 ViperIDE 分頁（燒錄 bin / 上傳項目檔案）

WebMaster 分頁列新增「🐍 ViperIDE」分頁，把官方 [ViperIDE](https://github.com/vshymanskyy/ViperIDE)
（MIT）**完整 source vendored 到 `tools/WebMaster/viper-ide/`**，以 `VIPER_IDE_BASE_URL=.` 建置成
相對路徑版，由本 server 以**同源 iframe** 掛在 `/viper/`。用途：

- **燒錄 bin**：板子 USB 插在開瀏覽器的電腦上，按 USB 圖示 → Flash，可燒官方或自訂 `.bin`（WebSerial/WebUSB，限 Chrome/Edge）。
- **上傳項目檔案**：連上裝置後可把整包專案/資料夾拖進去上傳、編輯 `.py`、REPL 操作。

注意：

- WebSerial/WebUSB 需 **secure context**：請用 `http://127.0.0.1:<port>`（或 https）開啟 WebMaster；純 `http://<LAN IP>` 無法燒錄。
- 若 `tools/WebMaster/viper-ide/build/` 不存在（例如剛 clone），`/viper/` 不會掛載，分頁會顯示建置提示；跑一次 `tools/WebMaster/viper-ide/rebuild.bat` 即可。
- 要魔改 ViperIDE（介面/連接層/新增功能）直接改 `tools/WebMaster/viper-ide/src/`，再跑 `rebuild.bat` 重新整理頁面即可看到。
- 已對 `tools/WebMaster/viper-ide/build.py` 加一處本地修補：`python-minifier` 已存在時跳過 pip（無 pip/無網環境可重建，`VIPER_FORCE_PIP=1` 強制更新）。

## 模組

- `protocol.py` — NC4 打包/解包（復用 slave/lib/sys）
- `device_manager.py` — slave 連線 + 回應匹配 + 狀態快取 + 心跳
- `transfer.py` — 上傳/下載/promote/confirm/delta（ACK 停等）
- `stream.py` — 串流控制（含 RAM 緩衝區實時播放）
- `audio.py` — MP3 清單（播放由瀏覽器 `<audio>` 負責）
- `server.py` — FastAPI 入口
- `static/` — SPA 前端
- `viper-ide/` — vendored ViperIDE（MIT）source，`build/` 產物掛載為 `/viper/`
