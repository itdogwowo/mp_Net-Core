# ViperIDE — mp_Net-Core 本地整合筆記

位置：`tools/WebMaster/viper-ide/`（**所有網頁工具統一收在 `tools/WebMaster/` 之下**，不另開平級目錄）

- **upstream**: https://github.com/vshymanskyy/ViperIDE （MIT）
- **vendored 版本**: 見 `package.json`（v0.6.5，2025-09 抓取 main 分支）
- **用途**: WebMaster「🐍 ViperIDE」分頁 — USB 燒錄 `.bin`、上傳/編輯項目檔案、REPL。

## 目錄

| 路徑 | 說明 |
|---|---|
| `src/` | 應用原始碼（魔改這裡：`src/ViperIDE.html` 佈局、`src/app.js` 邏輯、`src/lang/*.json` 翻譯、`src/transports/` 連接層…） |
| `build/` | 建置產物（**git 忽略**，WebMaster 掛載 `/viper/` 用） |
| `rebuild.bat` | Windows 一鍵重建（設 `VIPER_IDE_BASE_URL=.` + `python -B build.py --skip-tests`） |
| `build.py` | 官方建置腳本（含一處本地修補，見下） |
| `assets/` `docs/` `test/` `mcp/` `packages/` | 官方原樣保留 |

## 為什麼用 `VIPER_IDE_BASE_URL=.` 建置

ViperIDE 原始碼把所有資源（`micropython.wasm`、`mpy-cross`、`ruff`、favicon…）
都寫成 `${VIPER_IDE_BASE_URL}/assets/...`。若用官方預設（絕對 URL），只能掛在
網域根路徑。設成 `.` 之後全部變成 `./assets/...` 相對路徑，才能掛在
`http://<host>:<port>/viper/` 子路徑，而且跟 WebMaster **同源** → iframe 內
WebSerial/WebUSB 燒錄可用。

## 本地修補（build.py）

官方 `vendor_pypi_package()` 每次 build 都會跑 pip（裝 `python-minifier` 進
`src/tools_vfs/lib`）。本專案環境的 pip 暫存目錄不可寫（OSError Errno 13），
故加了一段：**目標套件已存在且未設 `VIPER_FORCE_PIP` 時跳過 pip**。
套件已由 vendoring 流程預先放入 `src/tools_vfs/lib/python_minifier/`（git 忽略，
重建時若缺，`rebuild.bat` 會經 build.py 自動 pip 安裝）。

## 重建流程（魔改後）

```bat
cd tools\WebMaster\viper-ide
rebuild.bat
```

重新整理 WebMaster 頁面（http://127.0.0.1:8000/）→ 🐍 ViperIDE 分頁即為新版。

## 已知取捨

- `manifest.json` 的 `start_url`/`scope` 仍是 `/`（PWA 安裝行為），iframe 使用不受影響。
- 服務在 `http://<LAN IP>`（非 localhost / 非 https）時，瀏覽器沒有 WebSerial/WebUSB，
  只能做編輯與 WebREPL 類連線 — 燒錄請用 127.0.0.1 或 https。
