# 中文

## 老爺華廈論壇 — 後端 API

小社區住戶論壇的後端 API：登入、註冊審核、公告、文章與回覆、文件下載、成員管理。這份文件分成兩個獨立的部分，請依需求閱讀：

- **[專案報告](#專案報告)**：這個系統是什麼、怎麼串起來的、每個決定的理由 —— 給想了解「這是什麼」的人看。
- **[架設 SOP](#架設-sop)**：一步一步的操作說明 —— 給想「動手把它跑起來」的人看。

這兩部分刻意分開，不要交叉閱讀；報告是背景知識，SOP 是操作手冊。

---

## 專案報告

### 這是什麼

一個給社區住戶用的小論壇，規模以一百人左右為準。住戶用 Google 帳號登入，註冊後要經過審核才能使用。功能刻意保持精簡：公告、文章與回覆、文件下載，以及管理成員的後台。

這支 repo 是後端，用 Flask 寫，資料存在 PostgreSQL。前端在另一支 repo [`minyou_t_forum-web`](https://github.com/beethoreven/minyou_t_forum-web)，兩者只透過這裡的 JSON API 溝通。

### 系統架構

```
minyou_t_forum/                 總覽用的 repo，把下面兩支掛在一起，本身不部署
├── minyou_t_forum-backend/     ← 這支 repo：Flask API，部署在 Render
└── minyou_t_forum-web/         前端：靜態網頁，部署在 GitHub Pages
```

```
瀏覽器（前端）
   │  ① 用 Google 登入，拿到 Google 的 ID Token
   │  ② POST /auth/login：用 ID Token 換論壇自己的登入憑證
   │  ③ 之後每個請求都帶 Authorization: Bearer <登入憑證>
   ▼
後端（這支 repo）──── PostgreSQL：成員、登入憑證、公告、文章、回覆
   │
   └──── Google Drive：社區文檔的檔案（用服務帳號讀一個資料夾）
```

每個要保護的路由，進來的第一件事都是確認「這個請求是誰發的、他能不能做這件事」，規則集中在 `auth_utils/access.py`。

### 登入憑證為什麼不直接用 Google 的

Google 登入給的 ID Token 效期只有一小時左右。它的用途是證明「這個人剛剛用這個 Google 帳號登入了」，不是拿來當長期的通行證。直接把它當成每個請求都帶的憑證，使用者每小時就會被登出一次。

所以 Google 的憑證只在 `POST /auth/login` 用一次。驗證通過後，後端發一張自己的登入憑證（`auth_utils/sessions.py`）：

- 憑證是一串隨機的字，本身不帶任何資訊。它代表誰，要查 `sessions` 表才知道。
- 資料庫只存憑證的 SHA-256，不存原文。資料庫外洩時，別人拿不到能用的憑證。
- 有效期是滑動的：有在用就一直有效，連續 90 天完全沒用才失效。
- 最後使用時間超過 60 秒才更新一次，不會每個請求都寫資料庫。

連不上 Google 而無法驗證時回 503，不回 401：那不代表憑證是假的，不該讓前端把人登出。

### 權限為什麼每個請求都重查

登入憑證只回答「這是誰」。這個人現在能不能用論壇、是什麼角色，每個請求都另外查 `users` 表，不寫進憑證，也不做快取。

這樣停用一個帳號、調整一個人的角色，都是他的下一個請求就生效，不用等憑證過期。這也是憑證敢活 90 天的原因：安全性由「當下的帳號狀態」保證，不是靠憑證早點過期。代價是每個請求多一次用 email 查表，以這個規模可以忽略。

檢查分三層，前一層過了才看下一層：

| 層 | 條件 | 不通過時 |
|---|---|---|
| 已登入 | 憑證查得到、沒過期 | 401，代碼 `not_signed_in` |
| 已審核 | `users` 表裡有他，而且狀態是已審核 | 403，代碼 `unregistered`、`pending` 或 `disabled` |
| 角色 | 夠不夠格做這件事 | 403，代碼 `forbidden` |

三種「已登入但不能用」用不同的代碼，前端才知道該顯示註冊表單、審核中，還是帳號無法使用。

### 為什麼任何 Google 帳號都能登入

登入不檢查白名單。還沒註冊的人要先登入，才能填註冊表單、看到自己的審核狀態；所以登入只代表「知道你是誰」，不代表能用論壇。

註冊（`POST /auth/register`）有兩條寫死的規則：

- 角色一律是一般使用者、狀態一律是審核中，請求裡多送的值不採用。開放的話，任何人都能把自己註冊成管理員。
- 已經有帳號的人不能再註冊，不管他是什麼狀態。否則被停用的人重新註冊一次就復活了。

帳號就是 Google 驗證過的 email，一律存小寫。email 取自登入憑證，不看請求裡送來的任何 email。

### 三種角色怎麼分

權限一層包一層：管理員 ⊃ 版主 ⊃ 一般使用者。

| 動作 | 一般使用者 | 版主 | 管理員 |
|---|---|---|---|
| 看公告、看文章與回覆、下載文件 | ✓ | ✓ | ✓ |
| 發文章、回覆；改或刪自己寫的 | ✓ | ✓ | ✓ |
| 新增、編輯、刪除公告 | | ✓ | ✓ |
| 編輯、刪除別人的文章與回覆 | | ✓ | ✓ |
| 後台：審核註冊、停用與恢復帳號、設定版主 | | ✓ | ✓ |
| 修改管理員的帳號、把別人設成管理員 | | | ✓ |

後台另外有兩條保護：

- 誰都不能改自己的角色與狀態。唯一的管理員如果把自己降級或停用，就沒有人能改回來了。
- 設定 `FIRST_ADMIN_EMAIL` 指定的帳號，每次登入都會被設回已審核的管理員。它不經過審核，也不會被別人鎖在外面。

### 內文為什麼只存純文字

公告、文章、回覆的內文原樣存進資料庫，後端不轉換、不跳脫。前端顯示時一律當成文字放進頁面，不當成 HTML，所以使用者打的 `<script>` 只會原樣顯示出來，不會被執行。

這是最簡單、也最不容易出錯的做法。之後要支援粗體、清單這類格式，舊的內容不用轉換。

字數算的是 Unicode 字元數（一個中文字、一個表情符號都算 1）。前端要用同一種算法，兩邊才不會一個說可以、一個說太長。

### 文件為什麼經過後端下載

社區文檔放在 Google Drive 的一個資料夾。後端用服務帳號讀它：資料夾的主人把資料夾分享給服務帳號（檢視者），它就看得到這個資料夾，其他的都看不到，權限範圍也只申請唯讀。上架、下架、改名都直接在 Google Drive 做，論壇不需要上傳介面。

檔案本身不公開，下載一律經過後端，所以只有已審核的成員拿得到。下載前先確認這個檔案真的在指定的資料夾裡：服務帳號看得到的檔案不一定只有這個資料夾的，不檢查的話，知道檔案代號的人就能下載到別的檔案。

只列一般檔案（PDF、Word、圖片等）。Google 文件、試算表這類線上格式沒有「原始檔」可以下載，所以不列。

文件的來源有兩種，介面相同（`documents/`）：正式環境讀 Google Drive，本機開發與測試讀一個本機資料夾。本機資料夾的版本不接受瀏覽器傳來的檔名或路徑，只接受清單裡出現過的代號，所以不管對方傳什麼，都拿不到資料夾以外的檔案。

### 文章的附圖存在哪裡

發文章可以附圖，最多五張。圖片在瀏覽器裡先縮小成 480P 的 JPEG 才上傳，後端只檢查張數、大小與檔頭，不做影像處理。

圖片預設存在資料庫（`stored_pictures` 表）。照理應該跟文件一樣放 Google Drive，但做不到：服務帳號在 Google Drive 沒有自己的儲存空間，也不能擁有檔案，就算把資料夾的編輯權限給它，它也沒辦法在個人的雲端硬碟裡建立檔案。這是 Google 的規定，[官方文件](https://developers.google.com/workspace/drive/api/guides/about-shareddrives)的建議是改用共用雲端硬碟（要 Google Workspace），或用真人的身分上傳。縮小後的圖片一張只有幾十 KB，放資料庫負擔不大，也不用另外申請任何儲存服務。

想把圖片放進 Google Drive 有一條路：部署一支 Apps Script 代為存檔（`tools/apps_script/pictures.gs`）。它由資料夾的主人部署、用主人的身分執行，所以檔案算主人的，會放進文件資料夾底下的 `Pic`。後端跟它之間用一個只有兩邊知道的密語確認身分，而且它只會動 `Pic` 資料夾。設定了它之後，新的圖才改存 Google Drive；之前存在資料庫的圖照樣看得到，因為從每張圖的代號就看得出它在哪裡。

`post_images` 表記的是每張圖存放處的代號。瀏覽器看圖時給的是資料庫那一筆的編號，不是存放處的代號，所以沒辦法用猜的拿到別的檔案。存圖要連到外面的服務，可能要好幾秒，所以是在兩次資料庫連線之間做的，不會佔著連線等。

同樣的限制也是後台的「新增文檔」不經過論壇上傳的原因：它只是把 Google Drive 的那個資料夾打開，由有編輯權限的人直接把檔案放進去。

### 列表為什麼用 before，不用頁碼

列表一次回 20 筆，要下一批時帶上一批最後一筆的 id（`?before=<id>`），只拿 id 比它小的。

用頁碼的話，使用者翻頁的同時有人發了新文章，後面每一筆都會往後擠一格，第二頁的第一筆會跟第一頁的最後一筆重複。用 id 當界線就不會。每次多拿一筆，用來判斷後面還有沒有資料，不用另外算總數。

### 為什麼不用連線池

每個請求開一條新的資料庫連線，用完就關。免費方案的主機會把閒置的服務凍結，連線池裡的連線醒來時可能已經斷了，下一個請求就卡在那裡。這個論壇的流量很小，每次重新連線的成本可以忽略。

資料庫要跟後端開在同一個區域。不同區的話，建立一條連線要跨區往返好幾趟，每個請求都會慢上一大截。

### 刪除為什麼是真的刪掉

刪除公告、文章、回覆是直接從資料庫刪除，不是標記成「已刪除」。這樣每一句查詢都不用記得排除已刪除的資料。代價是刪錯了救不回來，所以前端在刪除前都會再確認一次。

成員不會被刪除，只會被停用，所以舊文章的作者永遠查得到。

### 設定有問題就不啟動

必填的設定沒設、格式不對、值被引號包住，服務直接起不來，並說明是哪一個設定有問題（`config.py`）。帶著錯的設定跑起來，問題會拖到某一個請求才出現，更難找。錯誤訊息只說是哪一個設定，不印出它的值。

### 檔案結構

| 位置 | 負責 |
|---|---|
| `app.py` | 路由 |
| `wsgi.py` | 正式環境的進入點 |
| `config.py` | 設定的唯一讀取點 |
| `errors.py` | 錯誤的統一格式：`{"error": 訊息, "code": 代碼}` |
| `validation.py` | 輸入檢查 |
| `auth_utils/google_auth.py` | 驗證 Google 的 ID Token |
| `auth_utils/sessions.py` | 論壇自己的登入憑證 |
| `auth_utils/access.py` | 權限規則 |
| `db/` | 連線、建表（`schema.py`）、各資料表的讀寫 |
| `documents/` | 文件清單與下載：`google_drive.py`、`local_folder.py`；文章附圖的存放：`pictures.py` |
| `tools/apps_script/pictures.gs` | 代替後端把附圖存進 Google Drive 的 Apps Script |
| `tools/local_db.sh` | 本機開發用的 PostgreSQL |
| `tools/dev_session.py` | 本機開發時不經過 Google，直接發登入憑證 |
| `tests/` | pytest 測試 |

### 已知限制

- 任何 Google 帳號都能登入並送出註冊。有人亂送時，版主或管理員把他停用即可，沒有另外做防濫用，也沒有頻率限制。
- 沒有資料庫遷移工具。服務啟動時只會建立還不存在的表；要改既有的表，要另外寫 SQL 手動執行。
- 文件清單每次都向 Google Drive 查，沒有快取。
- 沒有通知（審核通過不會寄信）、沒有搜尋、沒有操作紀錄。公告不能附圖。
- 文章的附圖只能在發文時選，之後不能更換；要換圖只能刪掉文章重新發。
- 讀寫 Google Drive 的部分，自動測試用的是假的回應，不會真的連到 Google。

---

# 架設 SOP / Setup Guide

## Part A. 本機執行

### 1. 確認 Python 與 PostgreSQL

需要 Python 3.15（版本寫在 `.python-version`）與 PostgreSQL 18。在 macOS 用 Homebrew 安裝 PostgreSQL：

```bash
brew install postgresql@18
```

### 2. 建立虛擬環境、安裝套件

虛擬環境是一個只屬於這個專案的 Python 環境，裝在 `.venv/` 資料夾裡，不會影響電腦上其他的專案。

```bash
python3.15 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

### 3. 啟動本機資料庫

```bash
tools/local_db.sh start
```

第一次執行會在 `.localdb/` 建立一個本機的 PostgreSQL（port 5442），以及 `minyou_t_forum_dev`、`minyou_t_forum_test` 兩個資料庫。它不是系統服務，電腦重開機後要再執行一次；要停止就執行 `tools/local_db.sh stop`。

### 4. 設定環境變數

`.env` 是放本機設定的檔案，不會進版本控制。

```bash
cp .env.example .env
```

打開 `.env` 填兩個值，其他的用預設值就可以：

- `GOOGLE_CLIENT_ID`：Google 登入用的 OAuth 用戶端 ID。到 Google Cloud Console 建立，類型選「網頁應用程式」，把 `http://localhost:8180` 加進「已授權的 JavaScript 來源」。
- `FIRST_ADMIN_EMAIL`：你自己的 Google 帳號。用它登入就是管理員。

### 5. 啟動

```bash
.venv/bin/python app.py
```

服務在 `http://localhost:5180`。資料表會在啟動時自動建好。用瀏覽器打開 `http://localhost:5180/health`，看到 `{"status":"ok"}` 就是起來了。接著照 [`minyou_t_forum-web`](https://github.com/beethoreven/minyou_t_forum-web) 的說明啟動前端。

### 6. 跑測試

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost:5442/minyou_t_forum_test .venv/bin/python -m pytest
```

每個測試開始前都會清空測試資料庫，所以它的名稱一定要以 `_test` 結尾，測試會檢查這一點。測試不會連到 Google。

### 7. 換身分測試

要測一般使用者、版主、還沒註冊的人，不用準備好幾個 Google 帳號：

```bash
.venv/bin/python tools/dev_session.py someone@example.com
```

它會直接替這個 email 發一張登入憑證，並印出一行 JavaScript。在瀏覽器打開前端的頁面，把那一行貼到開發者工具的主控台，就會以那個 email 登入。這支工具要連得到資料庫才能執行，所以不是後門。

## Part B. 部署到 Render

資料庫用 Neon，後端用 Render。**兩邊要選同一個區域。**

1. **Neon**：建立專案，PostgreSQL 選 18，選好區域（建好之後不能改）。複製連線字串。資料表不用自己建。
2. **Render**：New → Web Service，選這支 repo。

   | 欄位 | 值 |
   |---|---|
   | Language | Python 3 |
   | Region | 跟 Neon 同一區 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `gunicorn wsgi:app --workers 1 --threads 8 --timeout 120` |
   | Health Check Path | `/health` |

   Python 的版本不用設定，Render 會讀 `.python-version`。

3. **環境變數**：照 Part C 的表填。值的前後不要加引號。
4. **Google Drive**：在 Google Cloud Console 啟用 Google Drive API、建立服務帳號並下載 JSON 金鑰；把放文件的資料夾分享給服務帳號的 email（結尾是 `.iam.gserviceaccount.com`），權限選「檢視者」。
5. 部署完成後打開 `https://<你的服務>.onrender.com/health`，看到 `{"status":"ok"}` 就是起來了。設定有錯的話服務起不來，Logs 裡會寫是哪一個設定。
6. **把附圖放進 Google Drive（選用）**。不做這一步，附圖存在資料庫，功能完全一樣。想讓圖片出現在文件資料夾底下的 `Pic` 才需要：
   1. 用文件資料夾主人的 Google 帳號打開 `https://script.google.com/home`，建立新專案，把 `tools/apps_script/pictures.gs` 的內容整個貼進去。
   2. 專案設定 → 指令碼屬性，加兩個：`SECRET`（自己取一串夠長的亂碼）、`FOLDER_ID`（跟 `DRIVE_FOLDER_ID` 一樣）。
   3. 部署 → 新增部署作業 → 類型選「網頁應用程式」，執行身分選「我」，誰可以存取選「所有人」。第一次會要求授權存取 Google Drive。
   4. 把部署後的網址填進 Render 的 `APPS_SCRIPT_URL`，剛剛那串亂碼填進 `APPS_SCRIPT_SECRET`。

免費方案的服務閒置一陣子就會休息，下一個請求要等它醒來。想讓它保持醒著，可以用外部的排程服務每 10 分鐘打一次 `/health`；這支不碰資料庫。

## Part C. 環境變數總覽

| 名稱 | 必填 | 說明 |
|---|---|---|
| `DATABASE_URL` | 必填 | PostgreSQL 的連線字串 |
| `ALLOWED_ORIGINS` | 必填 | 允許呼叫這個 API 的前端來源，逗號分隔。只有網域與 port，不含路徑，結尾沒有斜線。前端放在 GitHub Pages 的話是 `https://<帳號>.github.io` |
| `GOOGLE_CLIENT_ID` | 必填 | Google 登入用的 OAuth 用戶端 ID。前端的 `js/config.js` 要填同一個 |
| `FIRST_ADMIN_EMAIL` | 必填 | 第一位管理員的 Google 帳號 |
| `SERVICE_ACCOUNT_JSON` | 正式環境必填 | 服務帳號金鑰檔的完整內容 |
| `DRIVE_FOLDER_ID` | 正式環境必填 | 文件資料夾的代號，就是資料夾網址最後那一段 |
| `APPS_SCRIPT_URL`、`APPS_SCRIPT_SECRET` | 選填，兩個要一起填 | 代為存圖的 Apps Script 的網址與密語。有填，新的附圖存進 Google Drive 的 `Pic`；沒填，附圖存在資料庫 |
| `DOCUMENTS_DIR` | 本機必填 | 本機的文件資料夾，附圖放在它底下的 `Pic`。跟上面四個只能擇一 |
| `SESSION_IDLE_DAYS` | 選填，預設 90 | 登入憑證連續幾天沒用就失效 |
| `PORT` | 選填，預設 5180 | 本機啟動用的 port |

## Part D. API 一覽

錯誤一律回 `{"error": "給人看的訊息", "code": "代碼"}`。

| 方法與路徑 | 誰可以用 | 做什麼 |
|---|---|---|
| `GET /health` | 任何人 | 服務是否活著，不碰資料庫 |
| `POST /auth/login` | 任何人 | 用 Google 的 ID Token 換登入憑證 |
| `POST /auth/logout` | 任何人 | 讓帶來的憑證失效 |
| `GET /auth/status` | 已登入 | 目前的狀態與角色 |
| `POST /auth/register` | 已登入、還沒有帳號 | 送出顯示名稱與戶號，建立審核中的帳號 |
| `GET /announcements`、`GET /announcements/<id>` | 已審核 | 公告列表、公告內容 |
| `POST /announcements`、`PUT`、`DELETE /announcements/<id>` | 版主、管理員 | 新增、編輯、刪除公告 |
| `GET /posts`、`GET /posts/<id>` | 已審核 | 文章列表、文章內容與回覆 |
| `POST /posts` | 已審核 | 新增文章，可以附最多五張圖 |
| `GET /posts/<id>/images/<圖片編號>` | 已審核 | 文章的一張附圖 |
| `PUT /posts/<id>`、`DELETE /posts/<id>` | 作者本人、版主、管理員 | 編輯、刪除文章 |
| `POST /posts/<id>/replies` | 已審核 | 回覆 |
| `DELETE /replies/<id>` | 作者本人、版主、管理員 | 刪除回覆 |
| `GET /documents`、`GET /documents/<file_id>/download` | 已審核 | 文件清單、下載 |
| `GET /admin/users` | 版主、管理員 | 成員列表 |
| `PUT /admin/users/<id>` | 版主、管理員 | 改顯示名稱、戶號、角色、狀態 |
| `GET /admin/documents` | 版主、管理員 | 文件放在哪裡、要去哪裡新增 |

---

# English

## 老爺華廈論壇 — Backend API

The backend API of a small residents' forum: sign-in, registration review, announcements, posts and replies, document downloads, and member management. This document has two independent parts; read whichever you need:

- **[Project Report](#project-report)**: what this system is, how it fits together, and the reason behind each decision — for readers who want to understand it.
- **[Setup Guide](#setup-guide)**: step-by-step instructions — for readers who want to get it running.

The two parts are deliberately separate; don't cross-read them. The report is background, the guide is an operating manual.

---

## Project Report

### What This Is

A small forum for the residents of one community, sized for about a hundred people. Residents sign in with a Google account and must be reviewed after registering before they can use it. The feature set is deliberately small: announcements, posts and replies, document downloads, and an admin page for managing members.

This repo is the backend, written with Flask and storing data in PostgreSQL. The frontend lives in a separate repo, [`minyou_t_forum-web`](https://github.com/beethoreven/minyou_t_forum-web); the two talk only through the JSON API here.

### System Architecture

```
minyou_t_forum/                 Overview repo that mounts the two below; never deployed itself
├── minyou_t_forum-backend/     ← this repo: Flask API, deployed on Render
└── minyou_t_forum-web/         Frontend: static pages, deployed on GitHub Pages
```

```
Browser (frontend)
   │  ① Sign in with Google and receive Google's ID Token
   │  ② POST /auth/login: exchange the ID Token for the forum's own session token
   │  ③ Every later request carries Authorization: Bearer <session token>
   ▼
Backend (this repo) ──── PostgreSQL: members, sessions, announcements, posts, replies
   │
   └──── Google Drive: the community documents (one folder, read with a service account)
```

The first thing every protected route does is establish who sent the request and whether they may do this. The rules live in `auth_utils/access.py`.

### Why Not Use Google's Token as the Session

The ID Token from Google sign-in lasts about an hour. Its job is to prove "this person just signed in with this Google account", not to serve as a long-lived pass. Sending it on every request would sign the user out once an hour.

So Google's token is used exactly once, in `POST /auth/login`. After it verifies, the backend issues its own session token (`auth_utils/sessions.py`):

- The token is a random string that carries no information. Who it stands for is found by looking it up in the `sessions` table.
- The database stores only the token's SHA-256, never the token itself. If the database leaks, nobody gets a usable token.
- Expiry is sliding: the token stays valid as long as it is used, and expires only after 90 consecutive days of no use.
- The last-used time is updated at most once every 60 seconds, so not every request writes to the database.

When Google can't be reached and verification is impossible, the answer is 503, not 401: that doesn't mean the token is forged, and the frontend shouldn't sign the user out.

### Why Permissions Are Re-checked on Every Request

The session token only answers "who is this". Whether that person may use the forum right now, and in what role, is looked up in the `users` table on every request. It is not written into the token and not cached.

This way, disabling an account or changing someone's role takes effect on their very next request, without waiting for a token to expire. It is also why a token can safely live for 90 days: safety comes from the account's current state, not from tokens expiring early. The cost is one extra lookup by email per request, negligible at this size.

The check has three layers; each is evaluated only if the previous one passed:

| Layer | Condition | When it fails |
|---|---|---|
| Signed in | The token exists and hasn't expired | 401, code `not_signed_in` |
| Approved | The person is in `users` and their status is approved | 403, code `unregistered`, `pending` or `disabled` |
| Role | The role is high enough for this action | 403, code `forbidden` |

The three "signed in but not allowed" cases use different codes so the frontend knows whether to show the registration form, the under-review notice, or the account-unavailable notice.

### Why Any Google Account Can Sign In

Sign-in does not check an allow-list. Someone who hasn't registered must sign in first in order to fill in the registration form and see their review status; so signing in only means "we know who you are", not "you may use the forum".

Registration (`POST /auth/register`) has two hard-coded rules:

- The role is always regular user and the status is always under review; extra values in the request are ignored. Otherwise anyone could register themselves as an administrator.
- Someone who already has an account cannot register again, whatever its status. Otherwise a disabled person could come back by registering once more.

The account is the Google-verified email, always stored in lowercase. The email is taken from the session token, never from anything sent in the request.

### How the Three Roles Divide

Permissions nest: administrator ⊃ moderator ⊃ regular user.

| Action | Regular user | Moderator | Administrator |
|---|---|---|---|
| Read announcements, posts and replies; download documents | ✓ | ✓ | ✓ |
| Write posts and replies; edit or delete their own | ✓ | ✓ | ✓ |
| Create, edit and delete announcements | | ✓ | ✓ |
| Edit and delete other people's posts and replies | | ✓ | ✓ |
| Admin page: review registrations, disable and restore accounts, appoint moderators | | ✓ | ✓ |
| Change an administrator's account; make someone an administrator | | | ✓ |

The admin page has two more safeguards:

- Nobody can change their own role or status. If the only administrator demoted or disabled themselves, nobody could undo it.
- The account named by the `FIRST_ADMIN_EMAIL` setting is put back to an approved administrator every time it signs in. It skips review and can never be locked out by someone else.

### Why Bodies Are Stored as Plain Text

The body of an announcement, post or reply is stored exactly as typed; the backend neither converts nor escapes it. The frontend always inserts it into the page as text, never as HTML, so a `<script>` typed by a user is simply displayed, not executed.

This is the simplest approach and the hardest to get wrong. If formatting such as bold or lists is added later, existing content needs no conversion.

Lengths are counted in Unicode characters (one Chinese character or one emoji counts as 1). The frontend must count the same way, or one side will accept what the other rejects.

### Why Documents Are Downloaded Through the Backend

The community documents sit in one Google Drive folder. The backend reads it with a service account: the folder's owner shares the folder with the service account (as a viewer), which can then see that folder and nothing else, and only the read-only scope is requested. Adding, removing and renaming files is done directly in Google Drive, so the forum needs no upload page.

The files themselves are not public; every download goes through the backend, so only approved members can get them. Before downloading, the backend confirms the file really is in the configured folder: the service account may be able to see files outside it, and without the check anyone who knew a file's id could download it.

Only ordinary files (PDF, Word, images and so on) are listed. Online formats such as Google Docs and Sheets have no "original file" to download, so they are left out.

There are two document sources with the same interface (`documents/`): production reads Google Drive, local development and tests read a local folder. The local-folder version accepts no file name or path from the browser, only an id that appeared in the listing, so whatever is sent, nothing outside the folder can be reached.

### Where Post Pictures Are Stored

A post can carry up to five pictures. Each is shrunk to a 480p JPEG in the browser before upload; the backend only checks the count, the size and the file signature, and does no image processing.

By default pictures are stored in the database (the `stored_pictures` table). It would seem natural to keep them in Google Drive like the documents, but that isn't possible: a service account has no storage of its own in Google Drive and cannot own files, so even with edit access to the folder it cannot create a file in a personal drive. This is Google's rule; the [official documentation](https://developers.google.com/workspace/drive/api/guides/about-shareddrives) advises using a shared drive instead (which needs Google Workspace) or uploading on behalf of a human user. A shrunk picture is only a few dozen KB, so the database carries it easily and no extra storage service has to be set up.

There is one way to keep pictures in Google Drive: deploy an Apps Script that saves them (`tools/apps_script/pictures.gs`). The folder's owner deploys it and it runs as the owner, so the files belong to the owner and land in `Pic` under the documents folder. The backend and the script recognise each other by a secret only the two of them know, and the script touches nothing but the `Pic` folder. Once it is configured, new pictures go to Google Drive; pictures saved in the database earlier remain visible, because each picture's id shows where it lives.

The `post_images` table records where each picture is stored. A browser asks for a picture by that row's number, not by the storage id, so other files can't be reached by guessing. Saving has to call an outside service and may take several seconds, so it happens between two database connections rather than while holding one.

The same limitation is why "add document" on the admin page doesn't upload through the forum: it just opens that Google Drive folder, and someone with edit access puts the file in directly.

### Why Lists Use `before` Instead of Page Numbers

A list returns 20 items at a time. To get the next batch, send the id of the last item of the previous one (`?before=<id>`), and only items with a smaller id are returned.

With page numbers, if someone posts while a reader is paging, every later item shifts down by one and the first item of page two repeats the last item of page one. Using an id as the boundary avoids that. One extra row is fetched each time to tell whether more data follows, so no separate count is needed.

### Why There Is No Connection Pool

Each request opens a new database connection and closes it afterwards. Free-tier hosts freeze idle services, and a pooled connection may already be dead when the service wakes, leaving the next request stuck. This forum's traffic is tiny, so the cost of reconnecting each time is negligible.

The database must be in the same region as the backend. Across regions, establishing one connection takes several long round trips, and every request becomes much slower.

### Why Deleting Really Deletes

Deleting an announcement, post or reply removes it from the database rather than marking it "deleted". That way no query has to remember to exclude deleted rows. The cost is that a mistaken delete can't be undone, so the frontend asks for confirmation before every delete.

Members are never deleted, only disabled, so the author of an old post can always be found.

### A Broken Setting Stops the Service from Starting

If a required setting is missing, malformed, or wrapped in quotes, the service refuses to start and says which setting is at fault (`config.py`). Running with a broken setting only postpones the problem to some later request, where it is harder to find. The error message names the setting and never prints its value.

### File Structure

| Location | Responsibility |
|---|---|
| `app.py` | Routes |
| `wsgi.py` | Production entry point |
| `config.py` | The single place settings are read |
| `errors.py` | The uniform error format: `{"error": message, "code": code}` |
| `validation.py` | Input checks |
| `auth_utils/google_auth.py` | Verifies Google's ID Token |
| `auth_utils/sessions.py` | The forum's own session tokens |
| `auth_utils/access.py` | Permission rules |
| `db/` | Connection, table creation (`schema.py`), and reads and writes per table |
| `documents/` | Document listing and download: `google_drive.py`, `local_folder.py`; post picture storage: `pictures.py` |
| `tools/apps_script/pictures.gs` | The Apps Script that saves pictures into Google Drive on the backend's behalf |
| `tools/local_db.sh` | PostgreSQL for local development |
| `tools/dev_session.py` | Issues a session token directly during local development, bypassing Google |
| `tests/` | pytest tests |

### Known Limitations

- Any Google account can sign in and submit a registration. If someone abuses it, a moderator or administrator disables them; there is no further abuse protection and no rate limiting.
- There is no database migration tool. On start-up the service only creates tables that don't exist yet; changing an existing table means writing and running SQL by hand.
- The document list is fetched from Google Drive every time, with no cache.
- There are no notifications (no email when a registration is approved), no search, and no audit log. Announcements can't carry pictures.
- A post's pictures can only be chosen when the post is created and can't be replaced afterwards; to change them, delete the post and write it again.
- The code that reads and writes Google Drive is tested with fake responses; the automated tests never contact Google.

---

# Setup Guide

## Part A. Running Locally

### 1. Confirm Python and PostgreSQL

You need Python 3.15 (the version is in `.python-version`) and PostgreSQL 18. On macOS, install PostgreSQL with Homebrew:

```bash
brew install postgresql@18
```

### 2. Create a Virtual Environment, Install Packages

A virtual environment is a Python environment that belongs to this project alone. It lives in the `.venv/` folder and doesn't affect other projects on the machine.

```bash
python3.15 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

### 3. Start the Local Database

```bash
tools/local_db.sh start
```

The first run creates a local PostgreSQL in `.localdb/` (port 5442) with two databases, `minyou_t_forum_dev` and `minyou_t_forum_test`. It is not a system service, so run it again after a reboot; stop it with `tools/local_db.sh stop`.

### 4. Set Environment Variables

`.env` holds local settings and is never committed.

```bash
cp .env.example .env
```

Open `.env` and fill in two values; the defaults are fine for the rest:

- `GOOGLE_CLIENT_ID`: the OAuth client ID used for Google sign-in. Create it in Google Cloud Console with the type "Web application", and add `http://localhost:8180` to "Authorized JavaScript origins".
- `FIRST_ADMIN_EMAIL`: your own Google account. Signing in with it makes you the administrator.

### 5. Start It

```bash
.venv/bin/python app.py
```

The service runs at `http://localhost:5180`. Tables are created automatically on start-up. Open `http://localhost:5180/health` in a browser; `{"status":"ok"}` means it is up. Then follow [`minyou_t_forum-web`](https://github.com/beethoreven/minyou_t_forum-web) to start the frontend.

### 6. Run the Tests

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost:5442/minyou_t_forum_test .venv/bin/python -m pytest
```

Every test starts by emptying the test database, so its name must end in `_test`; the tests check this. The tests never contact Google.

### 7. Test as Different People

To test a regular user, a moderator, or someone who hasn't registered, you don't need several Google accounts:

```bash
.venv/bin/python tools/dev_session.py someone@example.com
```

It issues a session token for that email directly and prints one line of JavaScript. Open the frontend in a browser, paste the line into the developer tools console, and you are signed in as that email. The tool needs database access to run, so it is not a back door.

## Part B. Deploying to Render

The database runs on Neon and the backend on Render. **Pick the same region for both.**

1. **Neon**: create a project, choose PostgreSQL 18, and choose the region (it cannot be changed later). Copy the connection string. You don't need to create any tables.
2. **Render**: New → Web Service, and select this repo.

   | Field | Value |
   |---|---|
   | Language | Python 3 |
   | Region | The same region as Neon |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `gunicorn wsgi:app --workers 1 --threads 8 --timeout 120` |
   | Health Check Path | `/health` |

   The Python version needs no setting; Render reads `.python-version`.

3. **Environment variables**: fill them in from the table in Part C. Don't wrap values in quotes.
4. **Google Drive**: in Google Cloud Console, enable the Google Drive API, create a service account and download its JSON key; share the documents folder with the service account's email (it ends in `.iam.gserviceaccount.com`) as a viewer.
5. Once deployed, open `https://<your-service>.onrender.com/health`; `{"status":"ok"}` means it is up. If a setting is wrong the service won't start, and the Logs say which setting.
6. **Keep pictures in Google Drive (optional)**. Skip this and pictures are stored in the database, with exactly the same behaviour. Do it only if you want the pictures to appear in `Pic` under the documents folder:
   1. Signed in as the owner of the documents folder, open `https://script.google.com/home`, create a new project, and paste in the whole of `tools/apps_script/pictures.gs`.
   2. In Project Settings → Script Properties, add two: `SECRET` (a long random string of your own) and `FOLDER_ID` (the same as `DRIVE_FOLDER_ID`).
   3. Deploy → New deployment → type "Web app", execute as "Me", who has access "Anyone". The first time, it asks you to authorize access to Google Drive.
   4. Put the deployment's URL into Render's `APPS_SCRIPT_URL` and the random string into `APPS_SCRIPT_SECRET`.

A free-tier service goes to sleep after being idle for a while, and the next request waits for it to wake. To keep it awake, have an external scheduler request `/health` every 10 minutes; that route doesn't touch the database.

## Part C. Environment Variables Overview

| Name | Required | Description |
|---|---|---|
| `DATABASE_URL` | Required | The PostgreSQL connection string |
| `ALLOWED_ORIGINS` | Required | Frontend origins allowed to call this API, comma-separated. Domain and port only, no path, no trailing slash. For a frontend on GitHub Pages it is `https://<account>.github.io` |
| `GOOGLE_CLIENT_ID` | Required | The OAuth client ID used for Google sign-in. The frontend's `js/config.js` must hold the same value |
| `FIRST_ADMIN_EMAIL` | Required | The first administrator's Google account |
| `SERVICE_ACCOUNT_JSON` | Required in production | The full contents of the service account key file |
| `DRIVE_FOLDER_ID` | Required in production | The documents folder's id, the last segment of the folder's URL |
| `APPS_SCRIPT_URL`, `APPS_SCRIPT_SECRET` | Optional, set both together | The URL and secret of the Apps Script that saves pictures. With them, new pictures go to `Pic` in Google Drive; without them, pictures are stored in the database |
| `DOCUMENTS_DIR` | Required locally | A local documents folder; pictures go in `Pic` beneath it. Mutually exclusive with the four above |
| `SESSION_IDLE_DAYS` | Optional, default 90 | Days of no use after which a session token expires |
| `PORT` | Optional, default 5180 | The port used when running locally |

## Part D. API Overview

Errors are always `{"error": "message for people", "code": "code"}`.

| Method and path | Who may use it | What it does |
|---|---|---|
| `GET /health` | Anyone | Whether the service is alive; doesn't touch the database |
| `POST /auth/login` | Anyone | Exchanges Google's ID Token for a session token |
| `POST /auth/logout` | Anyone | Invalidates the token sent with the request |
| `GET /auth/status` | Signed in | The current status and role |
| `POST /auth/register` | Signed in, no account yet | Submits a display name and unit number, creating an account under review |
| `GET /announcements`, `GET /announcements/<id>` | Approved | The announcement list; one announcement |
| `POST /announcements`, `PUT`, `DELETE /announcements/<id>` | Moderator, administrator | Create, edit and delete announcements |
| `GET /posts`, `GET /posts/<id>` | Approved | The post list; one post with its replies |
| `POST /posts` | Approved | Create a post, with up to five pictures |
| `GET /posts/<id>/images/<picture number>` | Approved | One picture of a post |
| `PUT /posts/<id>`, `DELETE /posts/<id>` | The author, moderator, administrator | Edit and delete a post |
| `POST /posts/<id>/replies` | Approved | Reply |
| `DELETE /replies/<id>` | The author, moderator, administrator | Delete a reply |
| `GET /documents`, `GET /documents/<file_id>/download` | Approved | The document list; download |
| `GET /admin/users` | Moderator, administrator | The member list |
| `PUT /admin/users/<id>` | Moderator, administrator | Change display name, unit number, role and status |
| `GET /admin/documents` | Moderator, administrator | Where documents are kept and where to add one |
