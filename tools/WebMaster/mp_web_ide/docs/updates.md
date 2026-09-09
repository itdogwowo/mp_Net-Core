# mp_web_ide — 依賴更新操作說明

> 目的：esptool-js（或將來其他執行期依賴）有新版本時，安全地保持更新。

## 現況

- 唯一執行期依賴：`esptool-js`（^0.6.1，Apache-2.0，Espressif 官方 WebSerial 移植）
- 我哋嘅 code 只用到穩定 API 面：`Transport`、`ESPLoader`（`main()`/`writeFlash()`/`after()`）、
  `terminal` 介面（clean/write/writeLine）→ 升級通常 drop-in
- 工具鏈：vite / typescript / vitest（devDependencies，可獨立更新）

## 檢查有冇新版本

```powershell
# 注意：本機 npm 快取目錄不可寫（EPERM），要用 --cache 導向工作區
npm view esptool-js version --cache ..\..\..\_wmtmp\npm-cache
# 或睇 GitHub Releases: https://github.com/espressif/esptool-js/releases
```

## 升級步驟

```powershell
cd tools\WebMaster\mp_web_ide
# 1) 更新到最新（會自動寫入 package.json）
npm install esptool-js@latest --cache ..\..\..\_wmtmp\npm-cache
# 2) 三關驗證
npm run build      # TypeScript strict + Vite 打包
npm test           # 單元測試（位址/映像偵測）
# 3) 實機驗證（人手）：/flash/ 連 ESP32-S3 → 偵測 → 燒錄一次成功
```

## 升級注意

1. **API 破壞性變更**：升級後先 `npm run build`；若型別錯誤，睇
   [esptool-js CHANGELOG/Releases](https://github.com/espressif/esptool-js/releases)
   有冇 breaking change，再改 `src/flash/esptool.ts`（所有 esptool 互動都收喺呢個檔案，
   通常只改呢處）
2. **stub 檔案**：esptool-js 每粒晶片嘅 flasher stub 會跟版本更新——build 會自動包含，
   唔使手動處理
3. **行為驗證**：版本跳大（如 0.x → 1.x）時，除咗三關，建議實機測「失敗後重連唔使 F5」
   嘅情境
4. 唔好追「每日最新」：esptool-js 屬活躍開發，穩定後再升；我哋 pin 喺 package-lock.json

## 記錄（每次升級填一行）

| 日期 | 舊版 | 新版 | 結果 |
|---|---|---|---|
| 2026-09-03 | — | 0.6.1 | 首次安裝；修 double-open（勿手動 transport.connect，loader.main() 內部會 open） |
