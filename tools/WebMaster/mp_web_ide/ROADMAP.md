# mp_web_ide — 白室重建路線圖

> 決策（2026-09-03，成員拍板）：**完全捨棄 ViperIDE 程式碼**，只以
> `tools/WebMaster/viper-ide/TECHNICAL_REPORT.md` 與公開規格為 spec，從零重建；
> 產品型態＝**WebMaster 內嵌**；MVP 涵蓋：編輯＋REPL＋檔案管理、USB 連線/燒錄、
> 虛擬裝置、AI 協作通道。
> 技術決議：**TypeScript**；專案名 **mp_web_ide**。
> viper-ide 目錄**凍結**：不再追上游、不再拆解；僅作參考樣本與行為對照（其 build
> 持續可用，直到本專案 M2 驗收通過再退役）。

---

## 0. 為何可行、為何不能天真

- 可行：ViperIDE 是「外殼＋整合層」，重量級能力全來自獨立 MIT 引擎；自家領域邏輯
  是公開協議的實作（MicroPython raw REPL、WebREPL、BLE NUS 等），可依公開規格重寫。
- 不能天真：真機相容性（mpy ABI 矩陣、boot 時序、各家 BLE profile）是數年真機磨出來
  的；重寫必須**以虛擬裝置先通、真機逐板驗收**的方式推進，不能一次梭哈。

## 1. 邊界與紀律（Clean-room）

| 允許 | 禁止 |
|---|---|
| 公開協議規格：MicroPython raw REPL、WebREPL、.mpy/ABI、BLE NUS/Adafruit/CH9143 | 複製/改寫 ViperIDE 任一原始碼檔案（含 app.js、rawmode.js、fs_cache.js…） |
| 獨立 MIT 引擎套件：CodeMirror 6、Xterm.js、ruff-wasm、micropython-webassembly-pyscript 等（**它們不是 ViperIDE**，只是它也用） | 沿用其目錄結構、命名、註解語氣 |
| 技術報告作為「要做出什麼行為」的 spec 參考 | 直接搬 UI 文字/文案（自己寫） |
| 本機現有 WebMaster Python 服務（自家資產） | — |

審查方式：每批 diff 對照 viper-ide 同功能原始碼比對，若出現結構性雷同即重寫。
（授權上 MIT 本就允許複製，此紀律是**你選擇的乾淨度**，不是法律下限。）

## 2. 目標架構（三層＋後端）

```
┌─ 瀏覽器（新專案，同源 iframe 掛 WebMaster）────────────────────┐
│ webide-ui（自家外殼：版面/元件/主題/多語 zh-TW·en）              │
│    └─ core（自家領域：session 狀態機/fs 模型/REPL 協定/transports ports）
│          └─ engines（第三方 MIT：CM6/xterm/wasm 執行器…）        │
└───────────────┬──────────────────────────────────────────────────┘
                │ WS/bridge（自家協定）
┌───────────────▼──────────────────────────────────────────────────┐
│ WebMaster Python 後端（既有 server.py/firmware.py/…＋新 bridge 模組）│
│  燒錄/裝置協定/AI 動作端點                                        │
└───────────────────────────────────────────────────────────────────┘
```

- 四項 MVP 對應：**編輯＋REPL＋檔案管理**＝editor/terminal/fs 模組（直連 VM 或真板）；
  **USB 連線/燒錄**＝WebSerial transport（瀏覽器）＋燒錄走 WebMaster 後端（自家協議，
  不重造）；**虛擬裝置**＝micropython-wasm 當 transport；**AI 通道**＝bridge WS＋動作端點
  （後續可再加 MCP 外接）。

## 3. 目錄草案（新 repo：`tools/WebMaster/mp_web_ide/`）

```
mp_web_ide/
├─ docs/specs/          # 協議筆記：raw REPL、WebREPL、.mpy/ABI、燒錄對接、bridge 協定
├─ src/
│  ├─ shell/            # 外殼：app 組裝、版面元件、theme tokens、i18n
│  ├─ core/             # session/fs/repl/transports 之 port（自家契約＋實作）
│  ├─ editors/          # CM6 整合（引擎 adapter）
│  ├─ terminals/        # xterm 整合
│  ├─ devices/          # transports：vm、webserial、webrepl(ws)、ble（依 MVP 順序）
│  ├─ backend/          # WebMaster bridge 用戶端
│  └─ ai/               # AI 動作端點呼叫器（M3）
├─ test/                # 單元＋協定測試（vm 為主要測試目標）
├─ webmaster/           # Python 側新增：bridge.py 等（放回 WebMaster 套件結構）
└─ package.json         # dev: vite；test: vitest 或 mocha（M0 定）
```

## 4. 里程碑（每站可測、可回滾）

| 站 | 內容 | 驗收方式 |
|---|---|---|
| **M0** | 規格書＋骨架：raw REPL/WebREPL/.mpy 筆記；repo/lint/test 骨架；健康頁 | 文件審閱＋CI 綠 |
| **M1** | **垂直切片＝虛擬裝置全鏈**：wasm 載入→vm transport→session→raw REPL→xterm→CM6 編輯→執行→VM 碟檔案樹與最小檔案管理 | **你直接在瀏覽器玩**（無需硬體） |
| **M2** | 真板：WebSerial transport→raw REPL 真機；真板檔案管理；**燒錄對接**（先盤點 WebMaster firmware.py/device 現況，定義後端 API） | 你在 ESP32S3 等真板逐項驗 |
| **M3** | WebMaster 整合換血：新 IDE 取代 viper-ide 分頁；bridge＋AI 動作端點（含「列出/讀/寫/執行/燒錄」） | WebMaster 實測＋AI 實作任務 |
| **M4** | 打磨：離線/PWA、多語、主題品牌、多板矩陣 | 總驗收 |

順序理由：M1 只碰 wasm（無硬體風險）即可驗證「編輯→執行→檔案」主迴路；M2 才碰
真機相容性；viper-ide 分頁在 M2 通過前**並存不刪**（你有備援工具）。

## 5. 誠實時程與風險

- M0–M1：數次工作輪（純軟體，無真機風險）
- M2：真機除錯不可預期；板子 boot 行為（你的 ESP32S3 自訂韌體開機 log 很長）會是重點
- M3：bridge 協定要與 WebMaster 既有架構磨合
- 最大風險：真機協定邊角（raw REPL 時序、mpy ABI）——緩解＝M1 協定測試打底＋逐板驗收表
- 第二風險：範圍蔓延——MVP 四項全包的誘惑是把每一項都做深；紀律：**每站先做「最小可用」再加深**

## 6. 決議紀錄（M0 開工前）

1. **語言 → 已決：TypeScript**（Vite＋Vitest）
2. **命名 → 已決：mp_web_ide**（工作名，正式品牌之後再議）
3. **燒錄定義已釐清（2026-09-03 盤點）**：`.bin`＝韌體檔；燒錄走 WebMaster 既有
   `POST /api/firmware/{slave_id}`（`firmware.py:firmware_update`、`server.py:304`），
   瀏覽器不重造；「上傳項目檔案」（.py 等）＝mp_web_ide 自身 raw REPL 檔案管理職責。
   剩餘工作：M0 讀 `firmware.py`/`transfer.py` 定「進度回報/錯誤對應」的 UI contract。
