# 公開程式前的檢查

`public-files.txt` 是預計公開的來源清單，不包含本機文章、診斷或下載成果。

1. 執行 `.venv\Scripts\python.exe check_public_files.py`，檢查允許清單及常見個人路徑、IP、非虛構測試資料。
2. 閱讀實際差異；自動檢查無法保證不存在敏感內容。
3. Git 加入檔案後，執行 `.venv\Scripts\python.exe check_public_files.py --staged`，檢查待提交檔名是否都在清單中。
4. `.gitignore` 不會移除已追蹤檔案或歷史提交；首次公開前仍需另外檢查儲存庫歷史。

請勿上傳 `log/`、`test-downloads/`、正式備份、資料庫及附屬檔、人工 ANSI 原檔、生成的文章 HTML、搜尋資料、含真實文章的測試資料、個人設定、簡報工作目錄或未審閱的簡報成果。

偏好設定存於 `%LOCALAPPDATA%\BBS Downloader`，可能含帳號、看板及本機路徑，不在公開清單內。程式不把密碼寫入這份偏好設定。

`tests/fixtures/scroll_v223.json` 已改為虛構字元，保留行寬、配色與重疊關係。原始診斷保留在本機排除的 `log/` 中。

## 檔案用途

- `README.md`：GitHub 專案與開發說明。
- `downloader-guide.html`：下載器操作說明，舊 `/ReadMe.html`、`/help.html` 入口繼續相容。
- `archive-guide.template.html`：離線時光庫說明模板；每份備份仍產生 `ReadMe.html`，避免既有連結失效。
- `log/`：私人診斷與本機驗證文件。
- `test-downloads/`：單篇測試輸出。
- `tests/fixtures/`：可公開的虛構回歸資料。
- `.presentation-build/`、`output/`：簡報工作與成果，下載器執行不需要。

未刪除歷史下載、人工原檔、簡報或私人診斷資料。
