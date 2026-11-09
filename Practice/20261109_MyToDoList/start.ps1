# PowerShell Startup Script for MyToDoList
$Host.UI.RawUI.WindowTitle = "MyToDoList - 即時多人協作待辦清單"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 啟動 MyToDoList (Firebase: my-to-do-list-ac553)" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Cyan

Set-Location $PSScriptRoot
python server.py
