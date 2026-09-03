# mp_web_ide — 第一步：燒錄 (.bin) 體驗規格（flash-ui）

> 日期：2026-09-03｜狀態：草案待審
> 動機：現有流程（esptool-js 官方 demo 或舊介面）UI 差、要新手記位址；
> 目標＝**乾淨的燒錄 UI＋自動偵測晶片＋自動預設位址**，新手不盲選。
> 來源：[esptool-js npm](https://www.npmjs.com/package/esptool-js)、
> [esptool-js docs](https://espressif.github.io/esptool-js/docs/)、
> [espressif/esptool-js](https://github.com/espressif/esptool-js)、
> [issue #192 預設位址 0x1000→0x10000](https://github.com/espressif/esptool-js/issues/192)

## 1. 使用者流程（單頁三步）

```
① 選檔 ── 拖放或點選 .bin（顯示檔名/大小/可選 CRC 後算）
      │
② 連線 ── 「選擇裝置」按鈕 → 瀏覽器 WebSerial 選單 → 自動偵測晶片
      │      （偵測中 spinner；成功顯示：晶片型號/晶片 MAC/flash 大小）
      │
③ 燒錄 ── 位址欄自動帶入「依晶片的預設值」（可展開「進階」手動改）
      │      按鈕「開始燒錄」→ 進度條（％＋速度＋階段）→ 完成 ✓ / 錯誤 ✗
```

新手視角：**只做「選檔 → 選裝置 → 開始」**；位址/baud/erase 全部自動或隱藏在進階區。

## 2. 自動化規則（本專案的核心改善）

| 項目 | 自動行為 | 進階可覆寫 |
|---|---|---|
| 晶片偵測 | esptool-js 連上即讀（chip description/MAC/flash size） | — |
| 預設位址 | 依偵測晶片給建議值（下表）；UI 明示「自動（建議 0x10000）」 | 手動輸入（hex 驗證） |
| 建議位址表 | ESP32/ESP32-S2/S3/C3 系 app 映像：`0x10000`；ESP8266：`0x0000`；整包 merged bin（含 bootloader）：`0x0000`（需偵測：檔頭 0xE9 magic 且長度≈整包） | 每晶片預設可於 config 調整 |
| 整包偵測（待驗） | 讀 bin 前 4 bytes：ESP image magic `0xE9` 開頭 → 傾向「單一 app」；另提供「bootloader+partition+app 合包」判斷（M1 實作時以 esptool 文件核對） | — |
| erase | 預設「只擦要寫的區段」；進階可選「整片清除」 | checkbox |
| baud | 預設 921600，失敗自動降 115200 重試一次 | 進階可選 |

> ⚠️ 位址表在 M1 實作時以 esp-idf 預設分區表＋esptool 文件逐項核對後寫死進
> `src/devices/addresses.ts`（含註解來源），不靠印象。

## 3. 錯誤與狀態（新手可讀的中文訊息）

| 情境 | UI |
|---|---|
| 非 Chromium / 非 secure context | 首頁直接提示（WebSerial 限制），不進流程 |
| 連線被取消/失敗 | 回②可重選，原檔保留 |
| 板子在 ROM bootloader 模式之外（應用程式在跑） | esptool 會自動嘗試進入 bootloader（DTR/RTS）；失敗給「按住 BOOT 重插 USB」提示 |
| 中途斷線/取消 | 進度中止、明確狀態、可重來 |
| 完成 | 「燒錄完成 ✓ 可拔線/按重置」 |

## 4. 技術與整合

- 位置：`mp_web_ide/`（Vite＋TypeScript 單頁 app，產物 `dist/`）
- 依賴：`esptool-js`（MIT，Espressif 官方 WebSerial 移植）
- WebMaster 整合：仿 `server.py` 的 `/viper` 區塊，build 存在時掛
  `app.mount("/flash", StaticFiles(...))`＋`/api/flash` 狀態端點（同源 → WebSerial 可用）
- 本頁與未來模組（編輯/REPL/檔案）共用同一外殼；此步先獨立可測
- 測試：純邏輯（位址建議、hex 驗證、檔頭偵測）用 Vitest；真機燒錄＝你實測

## 5. 開放問題（實作前請你確認）

1. 你們的韌體 .bin 是哪種？(a) 單一 app 映像（0x10000） (b) 整包 merged（0x0，
   含 bootloader/partition） (c) 兩者都有 → 需要「自動判斷＋可改」
2. erase 預設：只擦區段 vs 整片？（燒錄速度與安全性取捨）
3. 掛載路徑用 `/flash` 可以嗎？還是要放進 mp_web_ide 主分頁之後再做？（建議先獨立 `/flash` 快速可用）
4. 你的 ESP32S3 板 WebSerial 連線時需要手動進 bootloader 嗎？（esptool 自動 DTR/RTS 若無效需要提示文字）
