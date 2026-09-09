@echo off
setlocal
cd /d "%~dp0"
echo [WebMaster] 一鍵啟動...
python launch.py %*
