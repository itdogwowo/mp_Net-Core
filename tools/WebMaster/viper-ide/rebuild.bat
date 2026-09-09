@echo off
rem ═══════════════════════════════════════════════════════════════
rem  rebuild.bat — 重建 ViperIDE → build/（WebMaster「🐍 ViperIDE」分頁用）
rem
rem  - VIPER_IDE_BASE_URL=.  → 產出「相對路徑」版，才能掛在 /viper/ 子路徑
rem    （同源 iframe，WebSerial/USB 燒錄才可用）
rem  - python-minifier 若已存在於 src/tools_vfs/lib 會自動跳過 pip
rem    （mp_Net-Core 修補；要強制更新設 VIPER_FORCE_PIP=1）
rem  - 魔改 src/ 之後跑一次本檔即可重新打包
rem ═══════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"
set VIPER_IDE_BASE_URL=.
if exist "..\.venv\Scripts\python.exe" (
    set PY=..\.venv\Scripts\python.exe
) else (
    set PY=python
)
echo [ViperIDE] using: %PY%
%PY% -B build.py --skip-tests
if errorlevel 1 (
    echo [ViperIDE] build FAILED
    exit /b 1
)
echo.
echo [ViperIDE] 完成 → build\index.html（重新整理 WebMaster 頁面即可看到新版本）
endlocal
