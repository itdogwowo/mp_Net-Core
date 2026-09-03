# ViperIDE 模組化改造計劃 v2 —「拆碎、去上游依賴」（草案）

> 定位：ViperIDE v0.6.5 只是「取料來源」。最終產品不依賴 upstream 的單體結構。
> 方法：**Ports & Adapters**——先定義「自家介面（Port）」，把 ViperIDE 與其每個外部模組的互動封進「轉接器（Adapter）」；自家邏輯只依自家介面。之後任意替換/移除任何外部模組都不影響整體。
> 順序：模組化（本計劃）→ WebMaster 整合層 → 一起設計 UI/UX（最後階段）。
> 驗證契約：每批「綠燈＝lint＋test＋rollup 重建全過」，批次間不做人工冒煙，最後一次總驗收。

---

## 1. 原則（寫給 AI 協作者與人類）

1. **每顆第三方依賴只允許出現在 `src/adapters/`**；其餘程式碼一律不得直接 import 外部套件。
2. 自家領域邏輯（裝置協定、檔案模型、編輯流程）放 `src/` 對應子系統，只依賴 `src/ports/` 的介面。
3. 介面要「小到能換掉實作」：editor port 只暴露我們真的會用的十來個操作，而不是 CodeMirror 全 API。
4. 無版本控制：每批開工前把「該批會動的檔案」複製一份到 `tools/WebMaster/_wmtmp/bak-<批號>/`（輕量保險），綠燈後保留到下批。
5. 不引入新框架；UI 維持原生 DOM＋自家元件（之後 UI/UX 大改才不會被框架綁死）。

## 2. 外部模組使用點總表（誰在用、用什麼、怎麼脫鉤）

| 外部模組（版本） | 目前使用處 | 我們依賴它的什麼 | 脫鉤策略 |
|---|---|---|---|
| `codemirror`＋`@codemirror/lang-{python,json,markdown}`、`legacy-modes` | 僅 `src/editor.js` | 編輯器實體、模式、linter 介面 | **Port `editor`**＋Adapter；保留（MIT、成熟）但換主題/換殼零痛 |
| `@uiw/codemirror-theme-*` | 僅 `editor.js` | 主題 CSS | Adapter 內選主題；換配色只動 token |
| `@xterm/xterm`＋addon-fit/web-links | `app.js`（終端建置段）＋CSS | 終端模擬 | **Port `terminal`**＋Adapter |
| `@astral-sh/ruff-wasm-web` | 僅 `python_utils.js` | lint/format wasm | **Port `toolchain`**（validate/lint/format/minify/disasm 各自方法）；Ruff 只是其中一個 backend |
| `@vshymanskyy/mpy-cross-wasm` | 僅 `python_utils.js` | 語法驗證/編譯 per-ABI | 同上 Port；釘版 URL 記在 adapter 註解 |
| `python-minifier==3.2.0`（pip 進 tools_vfs） | build.py vendor＋工具 VM | minify | 同上 Port（backend＝工具 VM）；本機已修補跳 pip |
| `mpy-tool.py`（倉內 79KB） | tools_vfs＋工具 VM | disasm | 同上 Port；檔案已屬我們（MIT），可長期保留 |
| `@micropython/micropython-webassembly-pyscript` | `emulator.js`、`python_utils.js`、build.py 複製 wasm、rollup 剝離 plugin | MicroPython 直譯器（VM＋工具 VM） | **Port `pythonRuntime`**；同時是傳輸端「vm」的 backend |
| `@gera2ld/tarjs` | 僅 `python_utils.js` loadVFS | tar.gz 解包 | 小工具，可自寫或留 adapter |
| `peerjs` | 僅 `transports/webrtc.js` | WebRTC P2P | Transport 介面已存在（base.js）；PeerJS 只是 webrtc backend |
| `web-serial-polyfill` | `app.js` import | 舊瀏覽器序列 fallback | 能力旗標＋adapter；WebSerial 原生即可時移除 |
| `toastr` | `app.js`、`utils_browser.js`、`viper_lib.js` re-export | 通知 | **Port `notifier`**＋自家輕量 toast 元件（可第一個換掉） |
| `i18next`＋languagedetector | `app.js`、`emulator.js` | 翻譯 | **Port `i18n`**；鍵名與 21 語 JSON 是我們的資產 |
| FontAwesome svg-core＋3 icon 包 | `app.js`、`viper_lib.js` | 圖示 | Adapter 包成 `icon()` 幫手；日後可換自家 SVG sprite |
| `github-fork-ribbon-css` | `app.js` CSS＋ViperIDE.html | 緞帶 | 直接移除（品牌化不需要） |
| `marked` | 僅 `markdown.js` | README 渲染 | **Port `markdown`**＋sanitize（已是 inertHTML 模式） |
| `ua-parser-js` | `app.js` | 瀏覽器偵測 | 小工具；可自寫簡版（僅用 UA 品牌/OS？） |
| `is-standalone-pwa` | `app.js` | PWA 狀態 | 一行 navigator 判斷，可內化 |
| `@amplitude/analytics-browser` | `app.js` | 遙測 | **Port `telemetry`**＋預設關閉（brand 旗標） |
| `serialport`（dev＋node transports＋mcp） | `transports/node_serial.mjs`、mcp | Node 端序列 | 自家工具用到的話保留在 node 層 adapter |
| rollup/eslint/mocha/chai（dev） | 建置測試 | 工具鏈 | 屬我們自己的工具，與產品解耦無關 |

「自家、與 upstream 無關、已可直接留用」的模組：`rawmode.js`（MicroPython 協定）、`fs_cache.js`、`tree_view.js`、`zip.js`、`transports/base.js`＋各 transport（瀏覽器 API 直連）、`connection_uid.js`、`settings.js`、`utils*.js`、`webrepl_content.js`。**這些日後可整包搬去新家，不欠 upstream。**

## 3. 目標目錄（細顆粒度 v2）

```
src/
├─ ports/                     # ★自家契約（純 JS＋註解型別，零第三方）
│  ├─ editor.js               #   create/destroy/set/get/lang/setTheme/onChange/lintHooks/undo/redo/cursor…
│  ├─ terminal.js             #   open/write/clear/reset/fit/onData/onResize/theme
│  ├─ toolchain.js            #   validate/compile/lint/format/minify/disasm/getAbi
│  ├─ pythonRuntime.js        #   load(url)/runPython/fs(js⇄vm)/replChar/restart
│  ├─ notifier.js             #   info/warn/error/toast
│  ├─ i18n.js                 #   t(key)/setLang/getLangs/onLangChange
│  ├─ telemetry.js            #   init/track(…)/setEnabled
│  ├─ markdown.js             #   render(md, baseUrl) → safeHTML
│  └─ icon.js                 #   icon(name) → svg string
├─ core/                      # 自家核心（薄、可測）
│  ├─ bus.js                  #   pub/sub
│  ├─ registry.js             #   commands/settings/tools/sideTabs/transports 註冊
│  ├─ config.js               #   VIPER_IDE_*／版本／BASE_URL（rollup 注入）
│  ├─ brand.js                #   ★品牌：appName/logo/連結/遙測開關/版權文案鍵
│  ├─ settingsCore.js         #   設定 schema 引擎＋localStorage＋變更事件（UI 自動渲染）
│  └─ i18nCore.js             #   i18next 初始化（唯一可碰 i18next 的地方，除 adapter）
├─ adapters/                  # ★所有第三方 import 的唯一合法住所（一個檔案對一顆依賴）
│  ├─ editor_codemirror6.js   ├─ terminal_xterm.js
│  ├─ toolchain_ruffWasm.js   ├─ toolchain_mpyCrossWasm.js
│  ├─ toolchain_toolsVM.js    ├─ runtime_micropythonWasm.js
│  ├─ i18n_i18next.js         ├─ notify_toastr.js（將被自家元件取代）
│  ├─ icons_fontawesome.js    ├─ md_marked.js
│  ├─ telemetry_amplitude.js  ├─ polyfill_webserial.js
│  └─ tar_tarjs.js
├─ device/                    # 裝置域（自家）
│  ├─ session.js              #   連線狀態機（connect/wire/init/disconnect/reconnect/事件）
│  ├─ catalog.js              #   transport 目錄（id/icon/label 鍵/能力）→ 工具列按鈕自動渲染
│  ├─ rawfs.js                #   raw REPL 高階 FS API（read/write/walk/mkdir/rm/move/info/atomic-write）
│  ├─ fs_cache.js  rawmode.js  repl_monitor.js  connection_uid.js  emulator.js   # 現況保留
│  └─ transports/             #   現況保留（base/web_serial/bluetooth/websocket/webrtc/vm/node_*）
├─ editor/                    # 編輯域（自家）
│  ├─ host.js                 #   開/存/調和/草稿/衝突流程（tab⇄fsCache⇄editorPort）
│  ├─ actions.js              #   open/close/save/saveAndCompile/run 指令封裝
│  └─ editor_tabs.js          #   現況保留（tab DOM 模型）
├─ tools/                     # Python 工具與套件（自家）
│  ├─ python_utils.js         #   改寫為只呼叫 toolchain/runtime ports（不再直接 import wasm 套件）
│  ├─ py_tools.js             #   prettify/minify/compile/disasm/validate 指令
│  ├─ package_mgr.js  pkg_ui.js
├─ terminal/
│  └─ terminal_host.js        #   xterm 建置移入 adapter；這裡管 tab/快捷/輸出訂閱
├─ ui/                        # 自家元件（原生 DOM、data-command 宣告式）
│  ├─ toolbar.js  sidemenu.js  tabs.js  tree_host.js  settings_panel.js
│  ├─ dlg.js  hexviewer.js  statusbar.js  toasts.js  splitter.js  dropzone.js
│  └─ theme.css（token 集中）+ app.css + app_common.css
├─ files/                     # 自家純邏輯：zip.js  tree_view.js  utils*.js  markdown.js(改走 port)
├─ bootstrap.js               #  組裝點（app.js 拆完的殘骸；目標 <300 行）
├─ viper_lib.js               #  對外 library 介面（embedders 用 ports＋device）
└─ app_worker.js  ViperIDE.html  lang/  manifest.json …  # 依現況
```

## 4. 批次路線（每批綠燈；綠燈＝lint＋test＋build 全過）

| 批 | 內容 | 產出 |
|---|---|---|
| B1 | `core/`＋`ports/`＋`registry` 骨架；HTML 開始 data-command 化（工具列按鈕先行） | 行為零變化（純搬家＋綁定） |
| B2 | `device/session.js`＋`device/catalog.js`（自 app.js 抽出連線家族） | app.js -~400 行 |
| B3 | `device/rawfs.js`＋`editor/host.js`（開/存/調和/草稿） | app.js -~500 行 |
| B4 | `editor/actions.js`＋`tools/py_tools.js`＋`tools/pkg_ui.js` | app.js -~450 行 |
| B5 | `terminal/terminal_host.js`＋`ui/` 元件群（tree_host/sidemenu/settings_panel/toasts/dlg/hexviewer…） | app.js -~400 行 |
| B6 | **adapter 化大掃除**：editor/terminal/i18n/notify/icons/md/telemetry/toolchain/runtime 全部收進 `adapters/`；`python_utils.js` 只剩 port 呼叫 | 第三方 import 只存在 adapters/（grep 驗證） |
| B7 | bootstrap 收尾＋brand 接入＋移除 ribbon；app.js 若還在就改名；補 ports/registry 單元測試 | 拆解完成 |
| B8 | WebMaster 整合層（見 §6） | bridge 可用 |
| B9 | **總驗收**：你在 WebMaster GUI 一輪人工冒煙（vm 連線/開檔/存/執行/prettify/套件/斷線重連） | 可進入 UI/UX 階段 |

每批先做 `_wmtmp/bak-<批號>/` 備份→改碼→三關綠燈→下一批。

## 5. 「去依賴」驗收標準（B7 檢查清單）

- [ ] `grep -r "from '" src --include=*.js`（排除 adapters/、core/i18nCore、transports/node_*）不再出現第三方套件名
- [ ] `src/ports/` 每個介面有「換實作的測試」：至少一顆假實作（fake）通過 host 層測試
- [ ] brand.js 切遙測 off 後，network 無 amplitude 呼叫（人工或 proxy 驗證）
- [ ] 移除任一 adapter（例 toastr→自家 toasts）不需改 host/領域碼
- [ ] `viper_lib.js` 對外只 export ports＋自家 API（embedders 不再依賴 upstream 結構）

## 6. WebMaster 整合層（B8 草案）

- Python 端新模組 `viper_bridge.py`：WS 用戶端（連 ViperIDE 控制通道）＋高層 API（status/list/read/write/run/term/install）＋給 WebMaster AI 動作模組呼叫。
- 瀏覽器端：`control_client.js` dispatch method 表**註冊化**（`device.*`/`editor.*`/`fs.*`/`pkg.*`），兩邊 method 名對齊。
- iframe 同源情境優先（現行 /viper/ 掛載不變）；`mcp/` 保留為通用外接，不綁定自家 UI。

## 7. 之後：UI/UX 階段（B9 通過才開始）

1. 品牌資料（brand.js＋theme token）先行，你給方向/參考圖，AI 出佈局草稿與多版配色。
2. 元件層重排（工具列分組、面板、狀態列）以 registry 資料渲染，不動領域碼。
3. 每版 `rebuild.bat` 出單檔 → WebMaster 重整即預覽 → 你回饋迭代。

## 8. 需你拍板的點

1. B6 移除緞帶/關遙測 OK？Blynk/publish 等用不到的功能同批拿掉？
2. 21 語翻譯：留全語系還是先收斂成 繁中/簡中/英文？
3. B8 的 WebMaster bridge 是否也在本次一起做，還是 B7 後先 UI/UX？

---

## 9. 決議與修訂紀錄 v2.1（2026-09-03，由成員拍板）

| 決議 | 內容 | 影響 |
|---|---|---|
| 功能取捨 | **先全保留**：遙測、Blynk、publish、緞帶等一律原封不動搬完，另立「精簡階段」再決定 | 拆解期間行為零刪減 |
| 語系 | **收斂成 zh-TW / zh-CN / en**；其餘 18 語已移入 `tools/WebMaster/_wmtmp/lang-archive-20260903/`（可隨時復原） | `src/lang/` 只剩 3 檔；重建後語系選單自動剩 3 項 |
| WebMaster bridge | **拆解＋總驗收＋UI/UX 定稿後再接**（viper_bridge.py） | 本計劃只剩拆解；bridge 移到第三階段 |
| 版本控制 | 不用 git；每批前備份 `_wmtmp/bak-<批號>/`；已建一次性全源快照 `_wmtmp/viper-ide-src-snapshot-20260903-163017.zip` | 回滾靠備份 |

**批次線修訂**（取代 §4 舊表）：
- B1–B7：拆解（B1 見下）
- B8：總驗收（你於 WebMaster GUI 一輪人工冒煙）→ 綠燈後才准進入 UI/UX
- 階段二：UI/UX 共同設計（brand.js＋theme token 先行）
- 階段三：WebMaster bridge（viper_bridge.py＋控制通道註冊化）

**B1 執行範圍修訂**（2026-09-03 完成）：
- 新增 `src/core/`：`bus.js`（pub/sub）、`registry.js`（commands/settings/tools/transports/sideTabs 註冊表）、`config.js`（VIPER_IDE_* 注入環境）、`brand.js`（品牌與開關）
- 新增單元測試 `test/suites/core.js`（純 Node、無板子無 DOM）
- 語系收斂（見上表）
- HTML 的 data-command 化**延後**至對應 UI 批次（B4/B5，需 HTML＋JS 同步遷移）
- 基線記錄：lint 0 錯誤；mocha 203 通過／14 pending（2026-09-03，target=vm）
