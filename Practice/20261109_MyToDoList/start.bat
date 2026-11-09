@echo off
chcp 65001 > nul
title MyToDoList - 即時多人協作待辦清單

echo ========================================================
echo  啟動 MyToDoList (Firebase: my-to-do-list-ac553)
echo ========================================================
echo.

python server.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [提示] 啟動 Python 伺服器失敗，嘗試使用 py 執行...
    py server.py
)

pause
