# ViperIDE 技術報告（mp_Net-Core 本地整合版）

> 對象：`tools/WebMaster/viper-ide/`（vendored 自 upstream ViperIDE v0.6.5）
> 用途：作為「讓 AI 更容易代為執行、甚至協作使用」的改造基礎
> 本文事實以 v0.6.5 原始碼為準；本機已套用 LOCAL.md 所述之本地修補與本報告第 9 節之清理

---

## 1. 專案身分與組成元件盤點

### 1.1 上游身分

| 項目 | 內容 |
|---|---|
| 名稱 | ViperIDE（`package.json` name、`manifest.json` display_name） |
| 版本 | 0.6.5（2025-09 自 main 分支抓取，見 LOCAL.md） |
| 作者 | Volodymyr Shymanskyy；MCP 部分另有 Andrew Leech（`mcp/manifest.json` author、`mcp/README.md`） |
| 授權 | MIT（`LICENSE`） |
| 定位 | 給 MicroPython / CircuitPython 開發板的「純網頁 + 行動端」IDE：內建編輯器、檔案總管、REPL、編譯器、linter、多種裝置連線 |
| 專案結構 | 核心單頁應用（`src/ViperIDE.html` 僅 222 行殼）+ 大型 ES module（`src/app.js` 2671 行）+ 獨立 MCP 伺服器套件（`mcp/`）+ Python 建置腳本（`build.py`） |

### 1.2 組成正規（這不是單一程式，而是拼合件）

README「Used software」自述的骨幹元件，與 `package.json`、`src/` 實際 import 交叉驗證如下：

| 元件（runtime 依賴） | 版本 | 用途 | 掛載點 |
|---|---|---|---|
| CodeMirror 6（`codemirror` + `@codemirror/lang-{python,json,markdown}`、`legacy-modes`） | ^6.x | 主程式碼編輯器（Python/JSON/Markdown/TOML/INI/PEM/mpy-dis 多種模式） | `src/editor.js`（507 行） |
| `@uiw/codemirror-theme-{monokai,material}` | ^4.25 | 編輯器主題 | `editor.js:19`（monokai 為主） |
| Xterm.js（`@xterm/xterm` + addon-fit + addon-web-links） | ^6.0 | REPL／終端 | `app.js:19-21`、`#xterm` |
| Ruff WASM（`@astral-sh/ruff-wasm-web`） | 0.16.2 | Python lint／format（`ruffLinter`、`prettifyPython`） | `python_utils.js:5`、`editor.js:326` |
| MicroPython WASM（`@micropython/micropython-webassembly-pyscript`） | 1.27.0 | ①瀏覽器內虛擬裝置 ②工具 VM（跑 minifier／mpy-tool） | `transports/vm.js`、`python_utils.js`、`emulator.js` |
| mpy-cross-wasm（`@vshymanskyy/mpy-cross-wasm`，GitHub tgz） | v1.1.0 | 語法驗證與 `.mpy` 編譯（依主機板 ABI 選檔） | `python_utils.js:4,96` |
| python-minifier（pip 塞進 `src/tools_vfs/lib`） | 3.2.0 | Python 程式碼最小化（在工具 VM 內執行） | `build.py` vendor、`python_utils.js:242` |
| mpy-tool.py（內嵌 79 KB） | — | `.mpy` 反組譯 | `tools_vfs/mpy-tool.py`、`python_utils.js:267` |
| web-serial-polyfill | ^1.0.15 | 無 WebSerial API 瀏覽器的序列 fallback | `app.js:26` |
| PeerJS | ^1.5.5 | WebRTC P2P 連線（`WebRTCTransport`） | `transports/webrtc.js:6` |
| i18next + browser-languagedetector | ^26/^8 | 多語系（21 個語言檔） | `app.js:16-17,2160` |
| toastr | ^2.1.4 | 通知 toast | `app.js:15` |
| FontAwesome（svg-core + solid/regular/brands） | ^7.3 | 圖示 | `app.js:52-60` |
| github-fork-ribbon-css | ^0.2.3 | About 分頁「GitHub」緞帶 | `app.js:11` |
| marked | ^18 | Markdown 渲染（README 分頁） | `markdown.js:9` |
| `@gera2ld/tarjs` | ^0.3.1 | 工具 VFS tar.gz 解包 | `python_utils.js:2` |
| ua-parser-js | ^2 | 裝置/瀏覽器偵測 | `app.js:38` |
| is-standalone-pwa | — | PWA 狀態判斷 | `app.js:23` |
| **@amplitude/analytics-browser** | ^2.45 | **遙測／分析（注意：含第三方上報，見 §10 風險）** | `app.js:39` |

| 元件（dev / 建置） | 版本 | 用途 |
|---|---|---|
| Rollup 4 + node-resolve/commonjs/json/replace/terser/import-css/serve/sourcemaps2 | ^4.x | 三入口 IIFE 打包 |
| ESLint 10 + globals + @eslint/js | ^10 | lint（`npm run lint`） |
| Mocha + Chai | ^11/^6 | 測試（`npm run test`，`.mocharc.json`） |
| serialport | ^13 | Node 端序列（測試與 Node 傳輸） |

| mcp/ 套件依賴 | 用途 |
|---|---|
| `@modelcontextprotocol/sdk` ^1.12 | MCP server/client 骨架 |
| `zod` | 工具參數 schema |
| `ws` | HTTP/WS 伺服器與控制通道 |
| `serialport` ^12 | 序列橋（免瀏覽器授權直連 USB） |
| `open` | 啟動時自動開瀏覽器 |

### 1.3 語系現況

`src/lang/` 21 檔：ar, de, el, en, es, fr, he, hi, id, it, ja, ko, nl, pl, pt, ro, ru, sv, uk, zh-CN, zh-TW。每檔結構 `{ "translation": { ... } }`（i18next resources 格式），共 **33 個鍵**：工具列（run/save/clear/conn.ws/conn.ble/conn.usb/terminal/fullscreen）、側欄選單（package-mgr/file-mgr/settings）、設定項目（interrupt-running-code、force-serial-poly、expand-minify-json、use-word-wrap、render-markdown、refresh-after-run、auto-soft-reset、use-natural-sort、lang、zoom、install-package-source）、檔案區（no-files/connect/used）、about（cta/report-bug）。建置時 `build.py gen_translations()` 合併成 `build/translations.json` 供 `app.js:32` import。

---

## 2. 總體架構

```
┌────────────────────────── 瀏覽器（單一 HTML 產物）─────────────────────────┐
│  src/ViperIDE.html(222行) ── app.css / app_common.css（rollup 抽出後內嵌）  │
│   └─ app.js（2671行，調度層）                                               │
│        ├─ editor.js ─────────── CodeMirror 6（python/json/markdown…）       │
│        ├─ editor_tabs.js ────── 多分頁（檔案 tab＋終端 tab）                 │
│        ├─ xterm ─────────────── REPL 終端                                   │
│        ├─ python_utils.js ───── ruff-wasm／mpy-cross-wasm／工具 VM          │
│        ├─ package_mgr.js ────── MicroPython 套件索引與安裝                   │
│        ├─ fs_cache.js ───────── 每裝置的檔案快取（草稿/世代）                │
│        ├─ tree_view.js ──────── 檔案樹 UI                                   │
│        ├─ rawmode.js ────────── MicroPython raw REPL 協定                   │
│        ├─ settings.js ───────── localStorage 設定                            │
│        └─ transports/ ───────── 統一 Transport 抽象                          │
│             ├─ web_serial.js ── Web Serial API                              │
│             ├─ bluetooth.js ─── BLE（Nordic UART／Adafruit／CH9143）         │
│             ├─ websocket.js ─── WebSocket REPL（relay／WebREPL）             │
│             ├─ webrtc.js ────── PeerJS P2P                                  │
│             └─ vm.js ────────── MicroPython WASM（虛擬裝置）                 │
└──────────────┬──────────────────────────────────────────────────────────────┘
               │ WS 控制通道（control_client.js：檔案/編輯器/REPL/裝置控制）
┌──────────────▼──────────────────────────────────────────────────────────────┐
│  mcp/（Node MCP 伺服器：viperIDE-mcp 0.1.0）                                 │
│   index.js ── 24 個 viperIDE_* 工具（stdio JSON-RPC ⇄ Claude/其他 AI）        │
│   ├─ ide-server.js ── localhost 隨機埠：伺服 build/ 產物＋/ws 控制通道       │
│   ├─ bridge.js ────── 向已在瀏覽器開啟的 IDE 下指令                          │
│   └─ serial-bridge.js ─ serialport 直連 USB，偽裝 WebREPL 握手               │
└──────────────────────────────────────────────────────────────────────────────┘
```

兩條「AI ⇄ 硬體」路徑：
1. **MCP 控制瀏覽器中的 IDE**（IDE 開在使用者面前，人機同時可用）
2. **MCP serial-bridge 直連序列埠**（不需瀏覽器 WebSerial 授權彈窗）

另有 `websocket_relay.cjs`：通用 WS 中繼（port 8080），房號模型 — `/new/<ID>` 掛「裝置端」，`/<ID>` 掛「IDE 端」，雙向轉發 bytes，用於跨網段/中繼 WebREPL。

---

## 3. src/ 目錄地圖（模組 → 職責 → 規模）

| 檔案 | 行數 | 職責摘要 |
|---|---|---|
| `ViperIDE.html` | 222 | 單頁殼：tool-panel（save/run/3 連線鈕/expand）、side-menu 六分頁（files/pkg/tools/settings/about）、main-editor、terminal(xterm)、dpi-ruler；`<script src=app.js>` |
| `app.js` | 2671 | 調度主體：連線生命週期（connectDevice→wirePort→initDeviceSession）、斷線重連（startReconnectLoop/toasts）、檔案樹與檔案操作、py 工具命令、套件安裝、xterm 初始化（~2372-2463，含 keymap）、i18n applyTranslation(2160)、Amplitude、PWA updateApp(2618)、拖曳調整 initDrag(2637) |
| `app.css` / `app_common.css` | — | 佈局與共用樣式（rollup 抽 CSS→build.py 內嵌） |
| `editor.js` | 507 | CodeMirror 6 封裝：python/json/markdown/toml/ini/pem/mpy-dis 模式、特殊註解裝飾（連結/註解色）、ruffLinter(326)、mpyCrossLinter(304)、createNewEditor(405) |
| `editor_tabs.js` | 243 | 檔案分頁：createTab/displayOpenFile/activateTab，tab ⇄ editor element |
| `rawmode.js` | 520 | `MpRawMode`(61)：raw REPL 進出、`\x04` 執行、hex 傳檔、軟重置偵測（SOFT_RESET_BANNER/REPL_PROMPTS/RAW_REPL_BANNER） |
| `repl_monitor.js` | 88 | `ReplMonitor`：提示符狀態機（回應中/已就緒） |
| `fs_cache.js` | 551 | `FsCache`(87)：依裝置 key 的快取（IndexedDB）、DRAFT_PREFIX 草稿自動儲存、世代同步 |
| `tree_view.js` | 494 | 泛用 `TreeView`：資料夾樹、拖放（TREE_DRAG_TYPE）、行內改名 |
| `zip.js` | 107 | 手寫 zip writer（createZipSync，STORE/CRC32），匯出專案 |
| `package_mgr.js` | 319 | MicroPython 套件：MIP_INDEXES、GitHub/GitLab URL 改寫、rawInstallPkg 下載至裝置 lib/ |
| `python_utils.js` | 390 | 工具核心：parseStackTrace、validatePython/compilePython（mpy-cross per ABI）、minifyPython（工具 VM + python-minifier）、prettifyPython（ruff）、disassembleMPY（mpy-tool）、loadVFS（tar.gz） |
| `emulator.js` | 62 | `createBrowserVM`：WASM MicroPython 虛擬裝置（載入 vm_vfs 範例碟） |
| `settings.js` | 123 | localStorage 設定讀寫＋變更通知（checkbox/select 對應 HTML id） |
| `markdown.js` | 81 | marked 渲染＋淨化（inertHTML 防注入）＋文件 URL 改寫 |
| `utils.js` / `utils_browser.js` | 121/320 | sleep/Mutex/fetch 家族/路徑分割、DOM 捷徑（QSA/QS/QID）、sanitizeHTML、IdleMonitor、getUserUID |
| `control_client.js` | 373 | **遠端控制用戶端**：initControlClient(21)→wsUrl(22) connect/send/dispatch(125)；包裝現行 port 提供：檔案樹 walk、讀檔、編輯器內容、REPL 緩衝、raw mode 包覆、對話框覆寫（給自動化用） |
| `connection_uid.js` | 61 | `ConnectionUID`：連線識別（控制端安全用途） |
| `transports/base.js` | 204 | `Transport` 抽象：open/close/讀寫佇列、chunk 處理 |
| `transports/web_serial.js` | 146 | WebSerial + polyfill 分支（force-serial-poly 設定） |
| `transports/bluetooth.js` | 242 | WebBluetooth：Nordic UART(NUS)/Adafruit/CH9143 三種服務協定 |
| `transports/websocket.js` | 155 | `WebSocketREPL`：ws://… WebREPL 握手＋資料流 |
| `transports/webrtc.js` | 150 | PeerJS 封包傳輸 |
| `transports/vm.js` | 189 | `MicroPythonWASM`(82)＋SYSTEM_DIRS：把 WASM VM 當「裝置」、snapshotFS/restoreFS |
| `transports/index.js` | 5 | 匯出 Transport/WebSerial/WebBluetooth/WebSocketREPL/WebRTCTransport |
| `transports/node.mjs` / `node_serial.mjs` | — | Node 環境傳輸（serialport），供自動化/測試 |
| `viper_lib.js` | 33 | **程式庫入口**：把核心（transport/rawmode/VM/cache/utils/toastr）重新 export——供 `bridge.html` 等自訂頁嵌用 |
| `app_worker.js` | 97 | Service worker：離線快取（含 wasm）、更新通知（normalizeUrl 快取策略） |
| `webrepl_content.js` | 15 | 給裝置端 WebREPL 用的內容腳本 |
| `tools_vfs/` | — | **工具磁碟**（Python 源碼）：python-minifier 全套、argparse/ast/utokenize 等標準庫、mpy-tool.py → 打包 `assets/tools_vfs.tar.gz` |
| `vm_vfs/` | — | **虛擬裝置示範碟**：07 支範例（Mandelbrot/fireworks/JS FFI…）＋lib（含預編譯 argparse.mpy）→ `assets/vm_vfs.tar.gz` |
| `manifest.json` | 872 | PWA manifest（start_url/scope 仍為 `/`，iframe 使用不受影響） |

---

## 4. 建置流程（改完怎麼出貨）

```
rebuild.bat（VIPER_IDE_BASE_URL=.）
  └─ python -B build.py --skip-tests
       1. 刪 build/，重建 build/assets
       2. gen_translations：src/lang/*.json → build/translations.json
       3. gen_manifest：版本號注入
       4. vendor python-minifier→tools_vfs/lib（本機已修補：存在即跳過 pip，VIPER_FORCE_PIP=1 強制）
       5. gen_tar：tools_vfs / vm_vfs → assets/*.tar.gz（排除 __pycache__/*.pyc）
       6. npm run build（rollup --config）
       7. combine()：把 app.css＋app.js 內嵌回 index.html / bridge.html / benchmark.html（單檔 2.2MB）
       8. 複製 wasm：micropython.wasm、mpy-cross-v*.wasm（多 ABI）、ruff_wasm_bg.wasm → assets/
```

rollup 重點（`rollup.config.mjs`）：
- 三入口：`app.js`（IDE）、`viper_lib.js`（程式庫）、`app_worker.js`（SW）
- IIFE + `inlineDynamicImports`（mpy-cross 動態 import 全內聯）、terser（正式版）、sourcemap（--configDebug）
- 佔位替換：`VIPER_IDE_VERSION` / `VIPER_IDE_BUILD`（Date.now()）/ `VIPER_IDE_BASE_URL`
- 特製 plugin 切除 MicroPython WASM 檔內的 Node CLI 開機段（`strip-micropython-node-cli`）並把 `import.meta.url` 換成 `document.baseURI`
- copyHtml 階段把 `${VIPER_IDE_BASE_URL}` 寫入 HTML

開發模式：`npm start`（rollup watch + --configDebug + serve build）。測試：`npm test`（mocha）。

---

## 5. MCP 伺服器 =「AI 一起用」的關鍵入口

套件：`mcp/`（獨立 npm 專案 `viperIDE-mcp` 0.1.0）。`mcp/README.md` 定義四角色架構：

| 角色 | 連線 |
|---|---|
| AI 用戶端（Claude 等，MCP 規格） | stdio JSON-RPC |
| MCP 伺服器（Node） | localhost HTTP + WS（隨機埠） |
| 瀏覽器 ViperIDE | `/ws` 控制通道 |
| USB MicroPython 裝置 | serialport 橋（115200 baud、偽 WebREPL 握手） |

啟動流程（`index.js`）：ensureInit→IDEServer 伺服 `build/`→`open` 開瀏覽器→維持 Bridge→註冊工具。

**工具清單（24 個，實測於 `src/index.js` 與 mcp/README）：**

- 連線：get_status、connect_device（ws/vm/直接提示 USB/BLE）、connect_serial、list_serial_ports、disconnect_device
- 檔案（裝置端）：list_files、read_file、write_file、delete_file、delete_dir、mkdir、create_file
- 編輯器（IDE 端）：open_file、get_editor、set_editor、save_file、close_file
- 執行：run_file、stop、reboot（soft/hard/bootloader）
- 終端：read_terminal、write_terminal、clear_terminal
- 套件：install_package

限制（README 明載）：BLE/USB 需使用者手勢（瀏覽器資安）；WebSocket 與 VM 連線 AI 可全自動；run 為 fire-and-forget（用 read_terminal 收輸出）；同時只允許一個瀏覽器分頁。

安裝三式：
```bash
# Claude Code
claude mcp add viperIDE -- npx -y @andrewleech/viperide-mcp
# 本機源碼
cd mcp && npm install
claude mcp add viperIDE -- node $(pwd)/src/index.js
# Claude Desktop：自 Releases 下載 .mcpb 於 Extension 安裝
```
自家測試：`mcp/test/e2e.js`（499 行）以 MCP client 對 VM／WebREPL／serial-bridge 三種目標跑整輪工具測試。

---

## 6. 連線與資料協定細節

- **raw REPL 協定**（rawmode.js）：`MpRawMode` 進入 raw 模式 → `\x04` 執行、hex/repr 傳檔、以 `OK/ERR`＋banner 判定完成；`ReplMonitor` 監聽軟重置（`onSoftResetDetected`）。
- **BLE**：支援三種服務常數集（NUS 官方/Adafruit+版本+流程控制/CH9143 含控制通道），具 TX 限長與快裝置偵測。
- **WebSocket**：`WebSocketREPL` 對 relay/WebREPL 端點做密碼握手後雙向流通。
- **中繼**：`websocket_relay.cjs` 在 8080 埠以「房」轉發（`/new/ID`=裝置端、`/ID`=IDE 端）。
- **WebRTC**：`peerjs` 對等連線（遙控場景）。
- **VM**：WASM MicroPython 當成一台「裝置」（含 SYSTEM_DIRS 與 fs snapshot/restore）——**AI 或使用者無硬體也能完整練習**。
- **斷線處理**：toast 提示→自動重連迴圈（token 控管）→transient drop 緩衝。

---

## 7. 客製化入口速查（想改成「自己喜歡的樣子」）

| 想改什麼 | 去哪裡改 |
|---|---|
| 工具列按鈕／布局 | `src/ViperIDE.html`（btn-save/btn-run/btn-conn-*/app-expand、`#container`）＋對應 CSS |
| 品牌名／logo | `ViperIDE.html` `.logo`、`#menu-about`（about-header/logo 圖 `assets/logo_1024.png`）、`build.py` copyHtml、`manifest.json` |
| 側欄六分頁增刪 | `ViperIDE.html` `#menu-tabs`（data-target 對應 menu-files/pkg/tools/settings/about）+ `app.js` setupTabs |
| 設定項增刪 | `ViperIDE.html` `#menu-settings-list` checkbox/select ＋ `settings.js`（自動對應 id）＋ `lang/*.json` 加鍵 |
| 語系字串 | `src/lang/<code>.json`（33 鍵）；新增語系＝加檔＋`applyTranslation` 語言選單會自動列 |
| 編輯器主題 | `editor.js` import（monokai→material）與 `extraTheme`(358) |
| 預設值 | `settings.js` `_loadSettings` 的預設物件 |
| 精簡功能（移除 publish/Blynk/遙測） | `ViperIDE.html` 對應 menu 項目＋`app.js` 對應函數＋移除 Amplitude import（app.js:39）與初始化 |
| 打包行為 | `build.py`＋`rollup.config.mjs`（內嵌、路徑、版本） |

---

## 8. 本機修改紀錄（mp_Net-Core）

1. **LOCAL.md 記載**：vendor 於 `tools/WebMaster/viper-ide`；`build.py` 加「python-minifier 已存在即跳過 pip（`VIPER_FORCE_PIP=1` 強制）」本地修補；一律 `VIPER_IDE_BASE_URL=.` 建置以掛載於 `/viper/` 子路徑並與 WebMaster 同源（WebSerial/WebUSB 可用）。
2. **本次敏感內容清理（去地緣徽章化）**：移除 README 徽章列、`src/app.js` About 徽章、`src/ViperIDE.html` 頁尾連結與兩處旗幟表情符號（`ViperIDE.html:131/136` 一帶，清除紀錄存於 `.scrub.log.txt`）。
3. **重建與驗證**：`python -B build.py --skip-tests` 全量重建成功；以詞條清單複驗 `src/`、`build/`、`mcp/` 三區 **0 殘留**。建置過程分析輔助資料存於 `.digest/`（純 ASCII 結構摘錄，供日後升級 upstream 再比對時使用）。

---

## 9. 建議的下一步（選擇方向）

- **A. 立即接 AI 協作（最快見效）**：`npm install` + `python -B build.py --skip-tests`（已驗證可用）+ `cd mcp && npm install`，再把 `mcp/src/index.js` 註冊進你慣用的 MCP 用戶端（Claude Code / Cursor / 自建 agent）。AI 即可：連虛擬裝置/WebREPL/USB、列/讀/寫檔案、開檔編輯、執行與中斷、讀寫 REPL、裝套件。建議先跑 `mcp/test/e2e.js` 驗證。
- **B. WebMaster 深度整合**：WebMaster 是自家 Python Web 服務（`tools/WebMaster/server.py`）。可在 Python 端掛 MCP client（`mcp` 套件）或仿 `control_client.js` 直接開 `/ws` 控制通道，讓 WebMaster 的 AI 動作（例如「把這個專案燒進板子」）直接驅動 ViperIDE 分頁。
- **C. 品牌化與精簡**：依 §7 換名/換 logo/換主題/移除遙測與用不到的功能（Blynk 工具、publish、github ribbon），產出「你的版本」；改完 `rebuild.bat` 一次出單檔。
- **D. 升級上游**：新版本釋出時重新 vendor，套用本報告 §8 的清理流程（建議把 `.digest/` 產生腳本存成正式工具，升級後重新比對殘留）。
- **E. 自動化骨架**：以 `viper_lib.js`＋`transports/node.mjs` 在 Node 側做無 GUI 的裝置自動化（燒錄/跑測），與 WebMaster 既有 Python 流程互通。

---

## 10. 附錄：FS／工具鏈／VM 深層細節（給改造者）

（由對 `src/` 的逐模組符號級分析彙整，行號為 v0.6.5 實測值）

- **裝置端檔案操作＝raw REPL 注入 MicroPython 片段**（`rawmode.js`）：`readFile` hex 串流每 64B 一筆；`writeFile` 預設先寫 `.viper.tmp` 再 `os.rename`（近似原子），`direct` 模式供套件安裝/上傳；`walkFs` 用 listdir/stat 遞迴；`getDeviceInfo` 解析 uname＋`_mpy` header 得出 ABI（arch/ver/sub）。
- **FsCache 新鮮度模型**（`fs_cache.js`）：raw 協定無 hash/CRC，故用「世代計數＋listing size」判斷快取可信度；每次執行結束/重開機/REPL 活動後世代遞增；**同長度改寫偵測不到**（需手動 refresh）。存檔是唯一 write-through 路徑（避免存完被當外部變更）；其餘寫入一律 invalidate；`reconcileListing` 回傳 `{changed, gone}` 差異；**未存 buffer 絕不被外部覆寫、只標 conflict**（對 AI/MCP 寫入的內建防護）。草稿自動備份於 localStorage（`viper-drafts.v1.*`，每裝置分桶，異常時整場停用備份而非拋錯）。
- **zip**：手寫 `createZipSync`（`zip.js`，零依賴）僅 stored、≤65535 筆/≤4GiB（拖出 dragstart 需同步）；**無讀 zip 路徑**；匯入＝單檔/資料夾拖放。tar.gz 僅建置期打包 `tools_vfs`/`vm_vfs`，瀏覽器以 `DecompressionStream('gzip')`＋tarjs 解包（`python_utils.js:170`）。
- **工具鏈（全版本釘死）**：
  - mpy-cross-wasm **1.1.0**（GitHub tgz URL 釘版，`package.json:28`）＝語法驗證（編輯器 `mpyCrossLinter`＋存檔前 `app.js:1679`）與 `.py→.mpy` 編譯（依板 ABI 選檔，assets 六支 `mpy-cross-v4…v6.3.wasm` 全打包）；
  - Ruff wasm **0.16.2**＝lint＋format（`line-length:120`、內建 Viper 方言 builtins：const/uint/ptr…，`python_utils.js:208`）；
  - python-minifier **3.2.0**（build.py pip vendor 進 `tools_vfs/lib`）跑在「工具 VM」內；
  - mpy-tool.py（倉內 79KB）反組譯——advanced mode 下開 `.mpy` 自動變唯讀 `.mpy.dis` 分頁。
- **WASM 執行環境**：`@micropython/micropython-webassembly-pyscript` 1.27.0（無 ASYNCIFY）**單一 wasm 兩用**：①虛擬裝置 transport（`?vm=1` 自動連 `vm://default`；模擬器「重開機」＝重新實例化 wasm 但先 snapshotFS/restoreFS 保留檔案）；②工具 VM（minify/disasm）。全部在主執行緒（專案無任何 Web Worker；`app_worker.js` 是 Service Worker 離線快取）。WASM 資產換版或新增時須同步 `app_worker.js:14-29` 快取清單與 build.py 複製段。
- **AI/MCP 擴充點**：瀏覽器端 `control_client.js` 的 `dispatch()` switch 加 method；Node 端 `mcp/src/index.js` 的 tools 陣列加同名工具＋zod schema。MCP 以 `http://localhost:<port>/?mcp=1` 開 IDE 後走 `/ws` 控制通道。測試：`mcp/test/e2e.js`（499 行，MCP client 對 VM/WebREPL/serial-bridge 全工具鏈）；`fs_cache.js` 刻意零依賴以便測試 harness 載入。
- **擴充示範**：新 Python 工具 → 放 `src/tools_vfs/` 自動入 tar.gz，執行仿 `disassembleMPY`（FS.writeFile→runPython→readFile）；新 CodeMirror 診斷 → `editor.js` createNewEditor 的 `.py` 分支註冊（`ruffLinter`/`mpyCrossLinter` 即範例）；熱鍵區 `app.js` 約 2481-2493（Alt+Shift+F/M/D）。

---

## 11. 風險與注意事項

- **遙測**：`@amplitude/analytics-browser` 已列入正式依賴並在 app.js 初始化；若要對內部署／隱私敏感請移除並驗證無殘留呼叫。
- **瀏覽器 API 限制**：非 localhost / 非 https 下無 WebSerial/WebUSB（LOCAL.md「已知取捨」）；BLE/USB 連線永遠需要使用者手勢。
- **授權**：MIT，衍生需保留著作權聲明（各檔 SPDX 頭／LICENSE）；README「Used software」所列各元件皆 MIT 系。
- **PWA manifest**：start_url/scope 為 `/`，iframe 內使用不受影響，但獨立 PWA 安裝行為未調。
- **內容衛生**：upstream 檔案可能夾帶地緣旗幟/徽章；升級或引用任何檔案內容進 AI 對話前，先經 `.digest/` 式淨化，避免模型內容安全閘誤判。
