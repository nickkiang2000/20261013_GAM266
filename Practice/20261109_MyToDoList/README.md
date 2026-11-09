# MyToDoList - 即時多人協作待辦清單

專為團隊、家庭與群組打造的現代化即時協作待辦清單網頁應用程式，整合 **Google Firebase (專案：`my-to-do-list-ac553`)**，支援官方 **Google 帳號登入**、**多人即時雙向同步**、**任務指派**、**邀請機制** 與 **進度統計**。

---

## 🌟 主要功能特色

1. **Google 帳號快速登入**
   - 整合 Firebase Authentication 官方 Google 登入，免去傳統繁瑣註冊流程。
   - 自動帶入 Google 頭像與顯示名稱。

2. **多人即時雙向協作 (Real-time Cloud Sync)**
   - 使用 Cloud Firestore `onSnapshot` 即時監聽，任何成員新增、編輯、勾選或刪除事項，所有人的畫面**零延遲即時更新**。
   - 支援建立多張不同主題的協作清單（如：專案衝刺、家庭採買、旅遊計畫）。

3. **靈活的成員邀請機制**
   - **Email 邀請**：直接輸入成員的 Google 信箱加入協作者名單。
   - **一鍵專屬邀請連結**：生成如 `http://localhost:5173/?join=LIST_ID` 的專屬連結，任何人點擊登入後自動加入該清單。

4. **精細的任務分工與管理**
   - **指派負責人**：可將任一事項指派給清單內的協作成員。
   - **優先等級**：高 🔴 / 中 🟡 / 低 🔵，快速標註重要事項。
   - **到期日與逾期警示**：清楚標記到期日期，逾期或今日到期時自動醒目標註。
   - **詳細備註**：支援多行補充說明與子項目。

5. **現代化 UI/UX 設計**
   - 採用 Tailwind CSS 精美排版，支援響應式手機、平板與桌面操作。
   - 即時關鍵字搜尋與狀態篩選（全部 / 未完成 / 已完成）。
   - 側邊欄快速過濾：指派給我、今日到期、所有事項。
   - 動態進度條計算完成百分比。

---

## 🚀 快速啟動方法

### 方法一：Windows 一鍵啟動 (推薦)
直接在資料夾中雙擊執行：
```bat
start.bat
```
或在 PowerShell 執行：
```powershell
.\start.ps1
```

### 方法二：使用 Python 啟動
本專案已包含自帶的輕量伺服器指令碼：
```bash
python server.py
```
啟動後會自動開啟瀏覽器並訪問 `http://localhost:5173`。

---

## 🔥 Firebase 專案 (`my-to-do-list-ac553`) 設定指引

此專案已預先設定好 Project ID 為 `my-to-do-list-ac553`。若為首次使用，請在 Firebase 控制台完成以下三項快速設定：

### 1. Firebase Web App 設定
本專案已將專案 `my-to-do-list-ac553` 的 Web App 金鑰預先內建於 `js/firebase-config.js`，任何訪客打開網頁即可直接使用，無需額外輸入設定。

### 2. 啟用 Google 登入提供者
1. 前往 Firebase 控制台 > **Build** > **Authentication** > **Sign-in method**。
2. 點擊 **Google** 提供者，切換為「啟用 (Enable)」。
3. 填寫公開的專案支援電子郵件，然後點擊「儲存」。
4. 切換到 **Settings** > **Authorized domains (已授權網域)**，確認清單中包含 `localhost`（通常預設已包含）。

### 3. 建立 Firestore 資料庫與規則
1. 前往 Firebase 控制台 > **Build** > **Firestore Database**。
2. 若尚未建立，點擊「建立資料庫」，位置可選擇 `asia-east1` (台灣) 或預設位置。
3. 切換至 **Rules (規則)** 分頁，將專案中的 `firestore.rules` 檔案內容完整貼上並點擊 **發布 (Publish)**。

---

## 📁 檔案結構說明

```text
MyToDoList/
├── index.html              # 應用程式主頁面 (SPA UI、模態視窗、現代化佈局)
├── css/
│   └── style.css           # 自訂樣式、捲軸美化與動畫過渡效果
├── js/
│   ├── firebase-config.js  # Firebase 初始化、專案配置與動態金鑰管理器
│   ├── auth.js             # Google 帳號登入、登出與狀態監聽模組
│   ├── db.js               # Cloud Firestore 即時監聽與清單/待辦 CRUD 核心邏輯
│   └── app.js              # 主控制器：UI 渲染、事件綁定、搜尋篩選與 Toast 通知
├── firestore.rules         # 建議的 Cloud Firestore 安全防護規則
├── server.py               # 本地輕量 HTTP 伺服器與自動開啟瀏覽器腳本
├── start.bat               # Windows 一鍵啟動批次檔
├── start.ps1               # PowerShell 一鍵啟動腳本
└── README.md               # 專案詳細說明與教學文件
```

---

## 💡 常見問題 (FAQ)

**Q: 點擊 Google 登入時出現 `auth/unauthorized-domain` 錯誤？**  
A: 請到 Firebase 控制台 > Authentication > Settings > Authorized domains，確認有加入 `localhost`。

**Q: 建立清單或待辦事項時出現 `Missing or insufficient permissions` 錯誤？**  
A: 請確認已將專案中的 `firestore.rules` 貼入 Firebase 控制台的 Firestore Database > Rules 並發布。

**Q: 如何邀請朋友一起協作同一張待辦清單？**  
A: 在清單頂部點擊「**👥 協作成員與分享**」，輸入對方的 Google Email 點擊「送出邀請」；或者點擊「複製連結」傳送給對方，對方點擊連結登入後即可自動加入。
