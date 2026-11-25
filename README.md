# 中文

## 老爺華廈論壇 — 後端 API

小社區住戶論壇的後端 API：登入、註冊審核、公告、文章與回覆、文件的上傳與下載、成員管理。這份文件分成兩個獨立的部分，請依需求閱讀：

- **[專案報告](#專案報告)**：這個系統是什麼、怎麼串起來的、每個決定的理由 —— 給想了解「這是什麼」的人看。
- **[架設 SOP](#架設-sop)**：一步一步的操作說明 —— 給想「動手把它跑起來」的人看。

這兩部分刻意分開，不要交叉閱讀；報告是背景知識，SOP 是操作手冊。

---

## 專案報告

### 這是什麼

一個給社區住戶用的小論壇，規模以一百人左右為準。住戶用 Google 帳號登入，註冊後要經過審核才能使用。功能刻意保持精簡：公告、文章與回覆（都可以附圖）、文件下載，以及新增公告、上傳文件、管理成員的後台。

這支 repo 是後端，用 Flask 寫，資料存在 PostgreSQL。前端在另一支 repo [`minyou-t-forum-web`](https://github.com/minyou-t-forum/minyou-t-forum-web)，兩者只透過這裡的 JSON API 溝通。

### 系統架構

```
minyou-t-forum/                 總覽用的 repo，把下面兩支掛在一起，本身不部署
├── minyou-t-forum-backend/     ← 這支 repo：Flask API，部署在 Render
└── minyou-t-forum-web/         前端：靜態網頁，部署在 GitHub Pages
```

```
瀏覽器（前端）
   │  ① 用 Google 登入，拿到 Google 的 ID Token
   │  ② POST /auth/login：用 ID Token 換論壇自己的登入憑證
   │  ③ 之後每個請求都帶 Authorization: Bearer <登入憑證>
   ▼
後端（這支 repo）──── PostgreSQL：成員、登入憑證、公告、文章、回覆、附圖
   │
   ├──── Google Drive：社區文檔的檔案（用服務帳號讀一個資料夾）
   └──── Apps Script：代替後端把檔案放進那個資料夾（用資料夾主人的身分執行）
```

每個要保護的路由，進來的第一件事都是確認「這個請求是誰發的、他能不能做這件事」，規則集中在 `auth_utils/access.py`。

### 登入憑證為什麼不直接用 Google 的

Google 登入給的 ID Token 效期只有一小時左右。它的用途是證明「這個人剛剛用這個 Google 帳號登入了」，不是拿來當長期的通行證。直接把它當成每個請求都帶的憑證，使用者每小時就會被登出一次。

所以 Google 的憑證只在 `POST /auth/login` 用一次。驗證通過後，後端發一張自己的登入憑證（`auth_utils/sessions.py`）：

- 憑證是一串隨機的字，本身不帶任何資訊。它代表誰，要查 `sessions` 表才知道。
- 資料庫只存憑證的 SHA-256，不存原文。資料庫外洩時，別人拿不到能用的憑證。
- 有效期是滑動的：有在用就一直有效，連續 90 天完全沒用才失效。
- 最後使用時間超過 60 秒才更新一次，不會每個請求都寫資料庫。
- 帳號被停用時，他所有的憑證一起作廢：之後就算恢復，停用前的舊憑證（包含遺失的手機上那一張）也不會跟著復活，要重新登入。被停用的人還開著網頁的話，下一個動作會先被請他重新登入，登入之後才看到「帳號無法使用」。
- 後台可以讓一個人「登出他所有的裝置」（`DELETE /admin/users/<id>/sessions`），例如他的手機掉了。帳號不會被停用。

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
| 後台：上傳、刪除社區文檔 | | ✓ | ✓ |
| 後台：一般使用者的審核註冊、停用與恢復、改名稱與戶號 | | ✓ | ✓ |
| 改任何人的角色；修改版主與管理員的帳號 | | | ✓ |

後台另外有兩條保護：

- 誰都不能改自己的角色與狀態。唯一的管理員如果把自己降級或停用，就沒有人能改回來了。
- 設定 `FIRST_ADMIN_EMAIL` 指定的帳號，只在論壇裡還沒有任何管理員時，登入就成為已審核的管理員。已經有管理員之後，它跟其他帳號一樣：別的管理員停用或降級它，它重新登入也不會自己恢復。這個帳號被盜時，才鎖得住它。代價是：還有其他管理員、但所有管理員都進不來的時候（例如另一位管理員把它降級後就不管了），論壇裡沒有辦法救回來，要直接在資料庫把角色改回去。
- 版主與管理員一定是已審核的帳號。後端改帳號時會檢查，資料庫也有一條跨欄位的限制（`role = 'member' OR status = 'approved'`），直接改資料庫也做不出「被停用的版主」。這條限制是服務啟動時補上的：先只檢查之後寫入的資料，再另外檢查既有的資料；既有的資料不符合時只寫 log，不讓服務起不來。

### 後台改帳號為什麼要帶「改之前的樣子」

後台每一列都是載入那一刻的資料。兩個人同時開著後台時，舊的畫面按下儲存，會把別人剛改好的內容蓋回去，例如把剛被停用的人改回已審核。

所以 `PUT /admin/users/<id>` 要帶 `expected`：畫面載入時看到的名稱、戶號、角色、狀態。後端先鎖住那一筆（`SELECT … FOR UPDATE`），跟現在的資料比對，對不上就回 409 `member_changed`，畫面會重新載入清單。鎖住再檢查，也讓「檢查能不能改」與「寫入」之間沒有空檔：版主儲存的同時，管理員剛好把同一個人升成管理員，版主也不能把他改回去。沒帶 `expected` 的是改版前的舊畫面，一樣回 409，請他重新整理。

### 編輯公告與文章為什麼要帶版本號

公告與文章各有一個版本號（`version`），每改一次加一。編輯時要帶著打開編輯頁時看到的版本號；這段期間被別人改過（例如作者與版主同時在改），版本號對不上，回 409 `edit_conflict`，不會直接蓋掉先存的那一份。畫面上打的字會留著，使用者可以先複製起來再重新整理。

### 怎麼擋住狂打 API、狂發文章

限制都是每個帳號各算各的（`auth_utils/guard.py`）。全站一起算的話，一個人亂來會害所有人一起被擋。

| 限制 | 數字 | 怎麼算 |
|---|---|---|
| 請求的頻率 | 每分鐘 120 個請求 | 記在記憶體，每個帶登入憑證的請求算一次 |
| 發文章 | 10 分鐘 5 篇、一天 30 篇 | 數資料庫裡他最近發了幾篇 |
| 回覆 | 10 分鐘 20 則、一天 200 則 | 同上 |
| 發公告（版主） | 10 分鐘 5 則、一天 20 則 | 同上 |
| 上傳文檔（版主） | 10 分鐘 10 個、一天 50 個，一天合計最多 200 MB | 數 `document_uploads` 表 |
| 登入憑證 | 一個帳號最多留 10 張 | 登入時把最舊的作廢 |
| 登入 | 同一個來源每分鐘 30 次；同一個 Google 帳號 10 分鐘 10 次 | 記在記憶體 |

- 超過就回 429，並說明多久之後可以再試。被限制只是暫時不能操作，不會被停用；要停用一個帳號，由版主或管理員在後台做。
- 請求的頻率記在記憶體，不進資料庫：每個請求都會經過這一關，為了省資源而每次寫一筆資料，反而花更多資源。服務重啟時計數歸零，那只是重新開始算一分鐘。代價是服務只能跑一個行程。
- 發文的數量直接數資料庫，不另外記帳，所以服務重啟也不會被重設。數之前先拿這個人的鎖（PostgreSQL 的 advisory lock），同一個人同時送出好幾篇時一個一個來；不然大家都數到「還沒超過」，就會一起寫進去。鎖是在讀完請求內容之後才拿：讀請求要等瀏覽器傳完，手機網路可能要好幾秒，不能拿著鎖等。讀之前會先不拿鎖粗查一次，已經超過的人不用把內容傳完。實測：上限 2 篇、同時送 8 篇，沒有鎖的時候 8 篇全部寫進去，有鎖之後只進去 2 篇。
- 公告與上傳文檔的上限是給版主的。沒有這兩道限制時，一位版主幾分鐘就能用公告的附圖塞滿 Neon 免費的 0.5 GB，或用上傳塞滿存放文檔那個帳號的雲端硬碟（空間跟他的信箱共用）。上傳是先在 `document_uploads` 記一筆、佔好名額，再去存檔；存檔失敗就把那一筆拿掉。
- 管理員不受請求頻率與寫入數量的限制：唯一的管理員如果被系統擋在外面，就沒有人能處理了。
- 沒有登入的請求只有 `/auth/login` 碰得到資料庫，所以它另外有次數限制。服務放在代理伺服器後面，「同一個來源」看的是代理接在 `X-Forwarded-For` 最後面的位址；前面的部分是請求自己帶的，可以亂填，所以不採用。

### 內文為什麼只存純文字

公告、文章、回覆的內文原樣存進資料庫，後端不轉換、不跳脫。前端顯示時一律當成文字放進頁面，不當成 HTML，所以使用者打的 `<script>` 只會原樣顯示出來，不會被執行。

這是最簡單、也最不容易出錯的做法。之後要支援粗體、清單這類格式，舊的內容不用轉換。

字數算的是 Unicode 字元數（一個中文字、一個表情符號都算 1）。前端要用同一種算法，兩邊才不會一個說可以、一個說太長。

### 文件為什麼經過後端下載

社區文檔放在 Google Drive 的一個資料夾。後端用服務帳號讀它：資料夾的主人把資料夾分享給服務帳號（檢視者），它就看得到這個資料夾，其他的都看不到，權限範圍也只申請唯讀。

檔案本身不公開，下載一律經過後端，所以只有已審核的成員拿得到。下載前先確認這個檔案真的在指定的資料夾裡：服務帳號看得到的檔案不一定只有這個資料夾的，不檢查的話，知道檔案代號的人就能下載到別的檔案。

只列一般檔案（PDF、Word、圖片等）。Google 文件、試算表這類線上格式沒有「原始檔」可以下載，所以不列。

文件有哪些，一律以資料夾裡現在有什麼為準，資料庫裡不記。所以直接在 Google Drive 增減檔案，清單也會跟著變。

文件的來源有兩種，介面相同（`documents/`）：正式環境用 Google Drive，本機開發與測試用一個本機資料夾。本機資料夾的版本不接受瀏覽器傳來的檔名或路徑，只接受清單裡出現過的代號，所以不管對方傳什麼，都碰不到資料夾以外的檔案。

### 文檔怎麼從後台傳進個人的 Google Drive

版主與管理員在後台選檔案上傳，檔案最後要進資料夾主人的 Google Drive。難的地方是後端自己寫不進去：服務帳號在 Google Drive 沒有自己的儲存空間，也不能擁有檔案，就算把資料夾的編輯權限給它，它也沒辦法在個人的雲端硬碟裡建立檔案。這是 Google 的規定，[官方文件](https://developers.google.com/workspace/drive/api/guides/about-shareddrives)的建議是改用共用雲端硬碟（要 Google Workspace），或用真人的身分上傳。

讓上傳的人用自己的 Google 帳號授權也不行：他的授權只能寫進他自己的雲端硬碟，而這裡要的是不管誰上傳，檔案都進同一個資料夾。

所以寫入是請一支 Apps Script 代勞（`tools/apps_script/documents.gs`）。它由資料夾的主人部署成網頁應用程式、用主人的身分執行，檔案就算主人的：

```
瀏覽器 ──（表單上傳，帶登入憑證）──▶ 後端 ──（JSON，帶密語）──▶ Apps Script ──▶ Google Drive 的資料夾
```

- 那支指令碼的網址任何人都連得到，所以後端跟它之間用一個只有兩邊知道的密語確認身分，沒有密語的請求一律拒絕。它只會動設定裡那一個資料夾的檔案。
- 瀏覽器不直接連 Apps Script：密語只放在後端，「是不是版主或管理員」、檔名與大小的檢查也都在後端做。
- 上傳同名的檔案時，新的確定放進去之後，舊的才丟進垃圾桶。刪除也是丟進垃圾桶。Google Drive 的垃圾桶會留 30 天，刪錯了主人還救得回來。
- 一個檔案最多 10 MB，只收 `validation.py` 的 `DOCUMENT_EXTENSIONS` 列的副檔名（常見的文件與圖片），擋掉住戶下載後可能被直接執行的檔案；Apps Script 也再擋一次。檔名保留中文，只拿掉路徑、擋掉隱藏檔與控制字元。不用 Werkzeug 的 `secure_filename`：它只留英數字，中文的檔名會整個被拿掉。
- 沒有設定這支指令碼時，文檔照樣讀得到，只是後台不能上傳，會改成請人直接到 Google Drive 的資料夾放檔案。
- 設定不對的時候（密語對不上、資料夾代號不對、網址或部署方式不對），後台看到的錯誤訊息會寫出該檢查哪一項設定，不用去翻伺服器的 log。訊息裡只有設定的名稱，沒有設定的值。

### 換一個 Google 帳號放文檔時要改什麼

存放文檔的帳號沒有寫死在任何地方。後端只認三個設定：資料夾的代號（`DRIVE_FOLDER_ID`）、指令碼的網址（`APPS_SCRIPT_URL`）與密語（`APPS_SCRIPT_SECRET`）。指令碼自己也只認它的兩個指令碼屬性。資料庫裡沒有記任何一個 Google Drive 的檔案代號。

所以要改由另一個 Google 帳號存放文檔時，新的主人準備一個資料夾、部署一份同樣的指令碼，再把那三個設定換成新的就好，程式與資料庫都不用動。步驟在架設 SOP 的 Part B。

### 附圖存在哪裡

公告與文章都可以附圖，最多五張。圖片在瀏覽器裡先縮小成 480P 的 JPEG 才上傳。

後端不假設圖片已經處理過：任何人都可以不經過網頁，直接呼叫 API 上傳帶著拍攝位置的原始照片。所以每一張都在伺服器解開、照 EXIF 的方向標記轉正，再只用畫素重新存一次 JPEG（`imaging.py`，用 Pillow）。存進去的圖不帶 EXIF、註解或色彩描述檔。解開之前先看圖片宣稱的尺寸，超過 400 萬畫素或任一邊超過 8000 的直接拒絕：檔案很小、尺寸卻很大的圖，解開時會吃掉好幾 GB 的記憶體；服務只有 512 MB，同時又會有好幾個請求，上限不能抓太寬（480P 的全景照 480 × 8000 是 384 萬畫素，還在範圍內）。

看圖的回應讓瀏覽器留一小時（`Cache-Control: private, max-age=3600`），並加上 `Vary: Authorization`：文章刪掉、帳號停用之後，留在瀏覽器裡的圖最多一小時就失效；同一台電腦換另一個帳號登入，也要重新向伺服器要，伺服器會重新檢查權限。

圖片存在資料庫（`stored_pictures` 表），不放 Google Drive。附圖是每一位住戶發文時都會用到的功能：放資料庫的話，發文不用等外面的服務回應，也不會因為外面的服務出狀況而發不出去。縮小後的圖片一張只有幾十 KB，資料庫放得下。

`announcement_images` 與 `post_images` 兩張表記的是「哪一張圖屬於哪一則公告、哪一篇文章」。瀏覽器看圖時給的是這兩張表裡的編號，而且要同時說出是哪一則公告或哪一篇文章的圖，所以沒辦法用猜的拿到別的圖。看圖跟看內文一樣，要是已審核的成員。

新增時，內文、圖片、兩者的對應在同一筆資料庫交易裡寫入：要嘛全部成功，要嘛什麼都沒有留下，不會有「文章沒發成功，圖片卻留在資料庫」的情況。刪除也是同一筆交易。

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
| `imaging.py` | 附圖在伺服器重新存檔：轉正、去掉中繼資料、擋掉尺寸太大的圖 |
| `timeutil.py` | 時間的格式：API 一律回 UTC 的 ISO 8601 |
| `auth_utils/google_auth.py` | 驗證 Google 的 ID Token |
| `auth_utils/sessions.py` | 論壇自己的登入憑證 |
| `auth_utils/access.py` | 權限規則 |
| `auth_utils/guard.py` | 防濫用：請求頻率、寫入數量、登入次數、登入憑證數量的限制 |
| `db/` | 連線、建表與補欄位（`schema.py`）、各資料表的讀寫；附圖的內容在 `pictures.py`，上傳紀錄在 `uploads.py` |
| `documents/` | 文件的清單、下載、上傳、刪除：`google_drive.py`、`local_folder.py` |
| `tools/apps_script/documents.gs` | 代替後端把文檔放進 Google Drive 的 Apps Script |
| `tools/local_db.sh` | 本機開發用的 PostgreSQL |
| `tools/dev_session.py` | 本機開發時不經過 Google，直接發登入憑證 |
| `tests/` | pytest 測試 |

### 已知限制

- 任何 Google 帳號都能登入並送出註冊。有人亂送時，版主或管理員把他停用即可。
- 沒登入的大量請求只有 `/auth/login` 有次數限制，其他的要靠主機前面的防護。如果代理接在 `X-Forwarded-For` 最後面的其實是另一層代理的位址，很多人會被算成同一個來源；每分鐘 30 次是照這個情況抓得比較寬的。
- 沒有資料庫遷移工具。服務啟動時會建立還不存在的表，並執行 `db/schema.py` 裡可以重複執行的 `MIGRATIONS`（補欄位、補限制）；改型別這類修改還是要另外寫 SQL 手動執行。
- 請求頻率與登入次數記在記憶體，服務重啟或休眠之後歸零。
- 文件清單每次都向 Google Drive 查，沒有快取。
- 沒有通知（審核通過不會寄信）、沒有搜尋、沒有操作紀錄。
- 公告與文章的附圖只能在新增時選，之後不能更換；要換圖只能刪掉重新發。
- 上傳的文檔一個最多 10 MB，而且整個檔案會先讀進後端的記憶體再轉交出去，不適合放很大的檔案。
- 讀寫 Google Drive 的部分，自動測試用的是假的回應，不會真的連到 Google。Apps Script 是在 Google 那邊執行的，不在自動測試的範圍內。

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

第一次執行會在 `.localdb/` 建立一個本機的 PostgreSQL（port 5442），以及 `minyou_t_forum_dev`、`minyou_t_forum_test` 兩個資料庫（資料庫的名稱不能有連字號，所以用底線）。它不是系統服務，電腦重開機後要再執行一次；要停止就執行 `tools/local_db.sh stop`。

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

服務在 `http://localhost:5180`。資料表會在啟動時自動建好。用瀏覽器打開 `http://localhost:5180/health`，看到 `{"status":"ok","version":"…"}` 就是起來了。接著照 [`minyou-t-forum-web`](https://github.com/minyou-t-forum/minyou-t-forum-web) 的說明啟動前端。

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
5. 部署完成後打開 `https://<你的服務>.onrender.com/health`，看到 `{"status":"ok","version":"…"}` 就是起來了。設定有錯的話服務起不來，Logs 裡會寫是哪一個設定。
6. **開啟後台的文檔上傳**。不做這一步，文檔照樣讀得到，但後台不能上傳，只能直接到 Google Drive 的資料夾放檔案。
   1. 用文件資料夾主人的 Google 帳號打開 `https://script.google.com/home`，建立新專案，把 `tools/apps_script/documents.gs` 的內容整個貼進去，取代原本的內容。
   2. 專案設定 → 指令碼屬性，加兩個：`SECRET`（自己取一串夠長的亂碼）、`FOLDER_ID`（跟 `DRIVE_FOLDER_ID` 一樣）。
   3. 部署 → 新增部署作業 → 類型選「網頁應用程式」，執行身分選「我」，誰可以存取選「所有人」。第一次會要求授權存取 Google Drive，畫面會警告這個應用程式未經 Google 驗證：它是你自己寫給自己用的，所以沒有送審，按「進階」繼續即可。
   4. 把部署後的網址（結尾是 `/exec`）填進 Render 的 `APPS_SCRIPT_URL`，剛剛那串亂碼填進 `APPS_SCRIPT_SECRET`。
   5. 到後台的「文檔」上傳一個檔案試試看。失敗的話，畫面上的訊息會寫該檢查哪一項設定。

   之後如果修改了指令碼的內容，要到「管理部署作業」把那個部署更新成新的版本，原本的網址才會執行新的內容。

### 之後要換一個 Google 帳號放文檔

程式與資料庫都不用改，只換設定：

1. 還要用的舊檔案，先從「社區文檔」下載下來。
2. 新的主人在自己的 Google Drive 建一個資料夾，分享給服務帳號的 email（檢視者）。
3. 新的主人用自己的帳號照上面第 6 步部署一份 Apps Script：`FOLDER_ID` 填新資料夾的代號，`SECRET` 另外取一串新的。
4. 到 Render 把 `DRIVE_FOLDER_ID`、`APPS_SCRIPT_URL`、`APPS_SCRIPT_SECRET` 換成新的三個值。儲存後服務會自動重新部署。
5. 到後台把第 1 步下載的檔案重新上傳，再到「社區文檔」確認都列得出來。從後台上傳的檔案，主人就是新的帳號。
6. 舊的主人到 Apps Script 的「管理部署作業」把舊的部署封存，舊的網址就失效了。

### 讓服務保持醒著

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
| `APPS_SCRIPT_URL`、`APPS_SCRIPT_SECRET` | 選填，兩個要一起填 | 代為把文檔放進 Google Drive 的 Apps Script 的網址與密語。有填，後台才能上傳、刪除文檔；沒填，文檔照樣讀得到，但要直接到 Google Drive 的資料夾放檔案 |
| `DOCUMENTS_DIR` | 本機必填 | 本機的文件資料夾，後台上傳的檔案直接放進去。跟上面四個只能擇一 |
| `SESSION_IDLE_DAYS` | 選填，預設 90 | 登入憑證連續幾天沒用就失效 |
| `PORT` | 選填，預設 5180 | 本機啟動用的 port |

## Part D. API 一覽

錯誤一律回 `{"error": "給人看的訊息", "code": "代碼"}`。

| 方法與路徑 | 誰可以用 | 做什麼 |
|---|---|---|
| `GET /health` | 任何人 | 服務是否活著，以及現在跑的是哪一版（git commit 的前七碼；本機是 `local`）。不碰資料庫。推了新版之後看這個值換了沒，就知道部署完成了沒有 |
| `POST /auth/login` | 任何人 | 用 Google 的 ID Token 換登入憑證 |
| `POST /auth/logout` | 任何人 | 讓帶來的憑證失效 |
| `GET /auth/status` | 已登入 | 目前的狀態與角色 |
| `POST /auth/register` | 已登入、還沒有帳號 | 送出顯示名稱與戶號，建立審核中的帳號 |
| `GET /announcements`、`GET /announcements/<id>` | 已審核 | 公告列表、公告內容。內容帶 `author_role`（作者現在的角色）與 `version`（編輯時要帶回來的版本號） |
| `GET /announcements/<id>/images/<圖片編號>` | 已審核 | 公告的一張附圖 |
| `POST /announcements` | 版主、管理員 | 新增公告，可以附最多五張圖 |
| `PUT /announcements/<id>`、`DELETE /announcements/<id>` | 版主、管理員 | 編輯、刪除公告。編輯要帶 `version`，對不上回 409 `edit_conflict` |
| `GET /posts`、`GET /posts/<id>` | 已審核 | 文章列表、文章內容與回覆 |
| `POST /posts` | 已審核 | 新增文章，可以附最多五張圖 |
| `GET /posts/<id>/images/<圖片編號>` | 已審核 | 文章的一張附圖 |
| `PUT /posts/<id>`、`DELETE /posts/<id>` | 作者本人、版主、管理員 | 編輯、刪除文章。編輯要帶 `version`，對不上回 409 `edit_conflict` |
| `POST /posts/<id>/replies` | 已審核 | 回覆 |
| `DELETE /replies/<id>` | 作者本人、版主、管理員 | 刪除回覆 |
| `GET /documents`、`GET /documents/<file_id>/download` | 已審核 | 文件清單、下載 |
| `GET /admin/users` | 版主、管理員 | 成員列表 |
| `PUT /admin/users/<id>` | 版主、管理員 | 改顯示名稱、戶號、角色、狀態。要帶 `expected`（畫面載入時的四個欄位），對不上回 409 `member_changed`。停用時會作廢他所有的登入憑證 |
| `DELETE /admin/users/<id>/sessions` | 版主、管理員（版主只能對一般使用者用） | 讓這個人在所有裝置上登出，回應 `{"revoked": 作廢的張數}` |
| `GET /admin/documents` | 版主、管理員 | 能不能從後台上傳文檔、一個檔案最大多少、收哪些副檔名 |
| `POST /admin/documents` | 版主、管理員 | 上傳一份文檔：`multipart/form-data`，欄位名稱 `file`，最多 10 MB，只收常見的文件與圖片（pdf、Office、OpenDocument、txt、csv、jpg、png）。同名的檔案會被取代 |
| `DELETE /admin/documents/<file_id>` | 版主、管理員 | 刪除一份文檔 |

---

# English

## 老爺華廈論壇 — Backend API

The backend API of a small residents' forum: sign-in, registration review, announcements, posts and replies, document uploads and downloads, and member management. This document has two independent parts; read whichever you need:

- **[Project Report](#project-report)**: what this system is, how it fits together, and the reason behind each decision — for readers who want to understand it.
- **[Setup Guide](#setup-guide)**: step-by-step instructions — for readers who want to get it running.

The two parts are deliberately separate; don't cross-read them. The report is background, the guide is an operating manual.

---

## Project Report

### What This Is

A small forum for the residents of one community, sized for about a hundred people. Residents sign in with a Google account and must be reviewed after registering before they can use it. The feature set is deliberately small: announcements, posts and replies (both can carry pictures), document downloads, and an admin page for adding announcements, uploading documents and managing members.

This repo is the backend, written with Flask and storing data in PostgreSQL. The frontend lives in a separate repo, [`minyou-t-forum-web`](https://github.com/minyou-t-forum/minyou-t-forum-web); the two talk only through the JSON API here.

### System Architecture

```
minyou-t-forum/                 Overview repo that mounts the two below; never deployed itself
├── minyou-t-forum-backend/     ← this repo: Flask API, deployed on Render
└── minyou-t-forum-web/         Frontend: static pages, deployed on GitHub Pages
```

```
Browser (frontend)
   │  ① Sign in with Google and receive Google's ID Token
   │  ② POST /auth/login: exchange the ID Token for the forum's own session token
   │  ③ Every later request carries Authorization: Bearer <session token>
   ▼
Backend (this repo) ──── PostgreSQL: members, sessions, announcements, posts, replies, pictures
   │
   ├──── Google Drive: the community documents (one folder, read with a service account)
   └──── Apps Script: puts files into that folder on the backend's behalf (runs as the folder's owner)
```

The first thing every protected route does is establish who sent the request and whether they may do this. The rules live in `auth_utils/access.py`.

### Why Not Use Google's Token as the Session

The ID Token from Google sign-in lasts about an hour. Its job is to prove "this person just signed in with this Google account", not to serve as a long-lived pass. Sending it on every request would sign the user out once an hour.

So Google's token is used exactly once, in `POST /auth/login`. After it verifies, the backend issues its own session token (`auth_utils/sessions.py`):

- The token is a random string that carries no information. Who it stands for is found by looking it up in the `sessions` table.
- The database stores only the token's SHA-256, never the token itself. If the database leaks, nobody gets a usable token.
- Expiry is sliding: the token stays valid as long as it is used, and expires only after 90 consecutive days of no use.
- The last-used time is updated at most once every 60 seconds, so not every request writes to the database.
- When an account is disabled, all of its tokens are invalidated together: even if it is restored later, the tokens from before (including the one on a lost phone) don't come back, and the person signs in again. A disabled person who still has the page open is first asked to sign in again on their next action, and only then sees "account unavailable".
- The admin page can sign a person out of every device (`DELETE /admin/users/<id>/sessions`), for example when their phone is lost. The account is not disabled.

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
| Admin page: upload and delete community documents | | ✓ | ✓ |
| Admin page: review regular users' registrations, disable and restore them, change their name and unit number | | ✓ | ✓ |
| Change anyone's role; change a moderator's or administrator's account | | | ✓ |

The admin page has two more safeguards:

- Nobody can change their own role or status. If the only administrator demoted or disabled themselves, nobody could undo it.
- The account named by the `FIRST_ADMIN_EMAIL` setting becomes an approved administrator at sign-in only while the forum has no administrator at all. Once there is one, it is like any other account: if another administrator disables or demotes it, signing in again does not restore it. That is what makes it possible to lock the account out if it is ever stolen. The cost: if other administrators exist but none of them can get in (say another administrator demoted it and then walked away), the forum itself offers no way back, and the role has to be fixed directly in the database.
- Moderators and administrators are always approved accounts. The backend checks this when an account is changed, and the database has a cross-column constraint too (`role = 'member' OR status = 'approved'`), so not even a direct database edit can produce "a disabled moderator". The constraint is added at start-up: first it applies only to rows written from then on, then the existing rows are checked separately; if existing rows don't satisfy it, only a log line is written, so the service still starts.

### Why Changing an Account Sends "What It Looked Like Before"

Each row on the admin page is the data as of the moment it loaded. With two people on the admin page at once, saving from a stale page would write back over someone else's change, such as setting a just-disabled person back to approved.

So `PUT /admin/users/<id>` carries `expected`: the name, unit number, role and status the page loaded. The backend locks that row first (`SELECT … FOR UPDATE`) and compares; if they differ it answers 409 `member_changed`, and the page reloads the list. Locking before checking also leaves no gap between "may this be changed" and the write: if a moderator saves at the very moment an administrator makes the same person an administrator, the moderator can't change them back. A request without `expected` comes from a page older than this check and also gets 409, asking for a reload.

### Why Editing Announcements and Posts Sends a Version Number

Announcements and posts each have a version number (`version`) that goes up by one with every edit. An edit carries the version the editor saw when opening the edit page; if someone else changed it meanwhile (say the author and a moderator editing at once), the versions don't match and the answer is 409 `edit_conflict`, instead of silently overwriting the copy saved first. The text typed on the page stays, so the user can copy it before reloading.

### How Hammering the API and Flooding Posts Are Stopped

Every limit is counted per account (`auth_utils/guard.py`). Counted site-wide, one person misbehaving would get everyone blocked.

| Limit | Number | How it is counted |
|---|---|---|
| Request rate | 120 requests per minute | Kept in memory; every request carrying a session token counts once |
| Posts | 5 per 10 minutes, 30 per day | Counts the person's recent posts in the database |
| Replies | 20 per 10 minutes, 200 per day | Same |
| Announcements (moderators) | 5 per 10 minutes, 20 per day | Same |
| Document uploads (moderators) | 10 per 10 minutes, 50 per day, at most 200 MB in total per day | Counts the `document_uploads` table |
| Session tokens | At most 10 per account | The oldest are invalidated at sign-in |
| Sign-ins | 30 per minute from one source; 10 per 10 minutes per Google account | Kept in memory |

- Going over returns 429 and says how long to wait. Being limited only blocks the action for a while; it never disables the account. Disabling an account is done by a moderator or administrator on the admin page.
- The request rate is kept in memory, not in the database: every request passes through this gate, and writing a row each time to save resources would cost more. A restart resets the counts, which only starts a fresh minute. The cost is that the service can run only one process.
- Post counts come straight from the database with no separate bookkeeping, so a restart doesn't reset them. Before counting, the backend takes that person's lock (a PostgreSQL advisory lock), so several posts sent by one person at the same moment go one at a time; otherwise each would count "not over yet" and all would be written. The lock is taken only after the request body has been read: reading it means waiting for the browser to finish sending, which can take seconds on mobile data, and that wait must not happen while holding the lock. A quick check without the lock runs before reading, so someone already over the limit doesn't have to finish sending. Measured: with a limit of 2 and 8 sent at once, all 8 got in without the lock and only 2 with it.
- The announcement and upload limits are for moderators. Without them, one moderator could fill Neon's free 0.5 GB with announcement pictures within minutes, or fill the Google Drive of the account that holds the documents (whose storage is shared with that person's email). An upload first records a row in `document_uploads` to claim its place, then saves the file; if saving fails, the row is removed.
- Administrators are exempt from the request-rate and write limits: if the only administrator were locked out by the system, nobody could deal with it.
- Of the requests without a session, only `/auth/login` reaches the database, so it has its own limit. The service sits behind a proxy, so "one source" means the address the proxy appends at the end of `X-Forwarded-For`; the earlier entries come from the request itself and can be made up, so they are not used.

### Why Bodies Are Stored as Plain Text

The body of an announcement, post or reply is stored exactly as typed; the backend neither converts nor escapes it. The frontend always inserts it into the page as text, never as HTML, so a `<script>` typed by a user is simply displayed, not executed.

This is the simplest approach and the hardest to get wrong. If formatting such as bold or lists is added later, existing content needs no conversion.

Lengths are counted in Unicode characters (one Chinese character or one emoji counts as 1). The frontend must count the same way, or one side will accept what the other rejects.

### Why Documents Are Downloaded Through the Backend

The community documents sit in one Google Drive folder. The backend reads it with a service account: the folder's owner shares the folder with the service account (as a viewer), which can then see that folder and nothing else, and only the read-only scope is requested.

The files themselves are not public; every download goes through the backend, so only approved members can get them. Before downloading, the backend confirms the file really is in the configured folder: the service account may be able to see files outside it, and without the check anyone who knew a file's id could download it.

Only ordinary files (PDF, Word, images and so on) are listed. Online formats such as Google Docs and Sheets have no "original file" to download, so they are left out.

Which documents exist is always whatever the folder holds right now; the database keeps no record of them. So adding or removing a file directly in Google Drive changes the list too.

There are two document sources with the same interface (`documents/`): production uses Google Drive, local development and tests use a local folder. The local-folder version accepts no file name or path from the browser, only an id that appeared in the listing, so whatever is sent, nothing outside the folder can be touched.

### How Documents Get from the Admin Page into a Personal Google Drive

Moderators and administrators pick a file on the admin page, and the file has to end up in the folder owner's Google Drive. The hard part is that the backend cannot write there itself: a service account has no storage of its own in Google Drive and cannot own files, so even with edit access to the folder it cannot create a file in a personal drive. This is Google's rule; the [official documentation](https://developers.google.com/workspace/drive/api/guides/about-shareddrives) advises using a shared drive instead (which needs Google Workspace) or uploading on behalf of a human user.

Having the uploader authorize with their own Google account doesn't work either: that authorization can only write to their own drive, and what is needed here is that every upload, whoever makes it, lands in the same folder.

So writing is delegated to an Apps Script (`tools/apps_script/documents.gs`). The folder's owner deploys it as a web app that runs as the owner, so the files belong to the owner:

```
Browser ──(form upload, with the session token)──▶ Backend ──(JSON, with the secret)──▶ Apps Script ──▶ the Google Drive folder
```

- Anyone can reach the script's URL, so the backend and the script recognise each other by a secret only the two of them know, and a request without it is refused. The script touches only the files in the one configured folder.
- The browser never talks to the Apps Script directly: the secret lives only on the backend, and the checks — is this a moderator or administrator, is the file name and size acceptable — are all done there.
- When a file with the same name is uploaded, the old one goes to the trash only after the new one is safely in place. Deleting also moves the file to the trash. Google Drive keeps the trash for 30 days, so the owner can still recover a mistaken delete.
- One file may be at most 10 MB, and only the extensions listed in `DOCUMENT_EXTENSIONS` in `validation.py` (common documents and images) are accepted, which keeps out files a resident might run straight after downloading; the Apps Script checks again. File names keep their Chinese characters; only the path is dropped, and hidden files and control characters are refused. Werkzeug's `secure_filename` isn't used: it keeps ASCII letters and digits only, so a Chinese file name would be stripped away entirely.
- Without the script configured, documents can still be read; the admin page just can't upload, and instead asks people to put files into the Google Drive folder directly.
- When a setting is wrong (the secret doesn't match, the folder id is wrong, the URL or the deployment is wrong), the error message shown on the admin page says which setting to check, so nobody has to dig through the server log. The message names settings and never includes their values.

### What Changes When Another Google Account Takes Over the Documents

The account that holds the documents isn't hard-coded anywhere. The backend knows only three settings: the folder's id (`DRIVE_FOLDER_ID`), the script's URL (`APPS_SCRIPT_URL`) and the secret (`APPS_SCRIPT_SECRET`). The script itself knows only its two script properties. The database records no Google Drive file id at all.

So to have a different Google account hold the documents, the new owner prepares a folder and deploys a copy of the same script, and those three settings are replaced with the new values; neither the code nor the database changes. The steps are in Part B of the Setup Guide.

### Where Pictures Are Stored

Announcements and posts can each carry up to five pictures. Each is shrunk to a 480p JPEG in the browser before upload.

The backend doesn't assume a picture has been processed: anyone can skip the web page and call the API directly with an original photo that carries its location. So every picture is decoded on the server, turned upright according to its EXIF orientation tag, and saved again as a JPEG from its pixels alone (`imaging.py`, using Pillow). What is stored carries no EXIF, comments or colour profile. Before decoding, the size the picture claims is checked, and anything over 4 megapixels or 8000 pixels on either side is refused: a picture with a tiny file but enormous dimensions would take gigabytes of memory to decode, and with only 512 MB and several requests at a time the limit can't be generous (a 480p panorama at 480 × 8000 is 3.84 megapixels, still within it).

Picture responses may be kept by the browser for an hour (`Cache-Control: private, max-age=3600`) and carry `Vary: Authorization`: after a post is deleted or an account disabled, a picture left in the browser expires within an hour, and when another account signs in on the same computer the browser has to ask the server again, which checks permission again.

Pictures are stored in the database (the `stored_pictures` table), not in Google Drive. Attaching a picture is something every resident does when posting: with the pictures in the database, posting doesn't wait on an outside service and can't fail because that service is having trouble. A shrunk picture is only a few dozen KB, which the database carries easily.

The `announcement_images` and `post_images` tables record which picture belongs to which announcement or post. A browser asks for a picture by its number in those tables, and must also say which announcement or post it belongs to, so other pictures can't be reached by guessing. Seeing a picture needs the same thing as reading the text: being an approved member.

On creation, the text, the pictures and the links between them are written in one database transaction: either everything succeeds or nothing is left behind, so a failed post can never leave its pictures in the database. Deletion is one transaction too.

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
| `imaging.py` | Re-saving pictures on the server: turning them upright, dropping metadata, refusing oversized dimensions |
| `timeutil.py` | Time formatting: the API always returns UTC in ISO 8601 |
| `auth_utils/google_auth.py` | Verifies Google's ID Token |
| `auth_utils/sessions.py` | The forum's own session tokens |
| `auth_utils/access.py` | Permission rules |
| `auth_utils/guard.py` | Abuse limits: request rate, write counts, sign-in counts, session token count |
| `db/` | Connection, table creation and added columns (`schema.py`), and reads and writes per table; picture contents are in `pictures.py`, the upload records in `uploads.py` |
| `documents/` | Listing, downloading, uploading and deleting documents: `google_drive.py`, `local_folder.py` |
| `tools/apps_script/documents.gs` | The Apps Script that puts documents into Google Drive on the backend's behalf |
| `tools/local_db.sh` | PostgreSQL for local development |
| `tools/dev_session.py` | Issues a session token directly during local development, bypassing Google |
| `tests/` | pytest tests |

### Known Limitations

- Any Google account can sign in and submit a registration. If someone abuses it, a moderator or administrator disables them.
- Of the requests without a session, only `/auth/login` is limited; the rest relies on whatever protection sits in front of the host. If the address the proxy appends at the end of `X-Forwarded-For` is actually another proxy's, many people count as one source; 30 per minute is deliberately generous for that case.
- There is no database migration tool. On start-up the service creates the tables that don't exist yet and runs the repeatable `MIGRATIONS` in `db/schema.py` (added columns and constraints); changes such as altering a column type still mean writing and running SQL by hand.
- The request rate and sign-in counts are kept in memory and reset when the service restarts or sleeps.
- The document list is fetched from Google Drive every time, with no cache.
- There are no notifications (no email when a registration is approved), no search, and no audit log.
- The pictures of an announcement or post can only be chosen when it is created and can't be replaced afterwards; to change them, delete it and write it again.
- An uploaded document may be at most 10 MB, and the whole file is read into the backend's memory before being passed on, so this isn't suited to very large files.
- The code that reads and writes Google Drive is tested with fake responses; the automated tests never contact Google. The Apps Script runs on Google's side and is outside the automated tests.

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

The first run creates a local PostgreSQL in `.localdb/` (port 5442) with two databases, `minyou_t_forum_dev` and `minyou_t_forum_test` (database names can't contain hyphens, so they use underscores). It is not a system service, so run it again after a reboot; stop it with `tools/local_db.sh stop`.

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

The service runs at `http://localhost:5180`. Tables are created automatically on start-up. Open `http://localhost:5180/health` in a browser; `{"status":"ok","version":"…"}` means it is up. Then follow [`minyou-t-forum-web`](https://github.com/minyou-t-forum/minyou-t-forum-web) to start the frontend.

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
5. Once deployed, open `https://<your-service>.onrender.com/health`; `{"status":"ok","version":"…"}` means it is up. If a setting is wrong the service won't start, and the Logs say which setting.
6. **Turn on document uploads on the admin page**. Skip this and documents can still be read, but the admin page can't upload; files have to be put into the Google Drive folder directly.
   1. Signed in as the owner of the documents folder, open `https://script.google.com/home`, create a new project, and paste in the whole of `tools/apps_script/documents.gs`, replacing what is there.
   2. In Project Settings → Script Properties, add two: `SECRET` (a long random string of your own) and `FOLDER_ID` (the same as `DRIVE_FOLDER_ID`).
   3. Deploy → New deployment → type "Web app", execute as "Me", who has access "Anyone". The first time, it asks you to authorize access to Google Drive and warns that the app isn't verified by Google: it is a script you wrote for your own use and was never submitted for review, so choose "Advanced" and continue.
   4. Put the deployment's URL (it ends in `/exec`) into Render's `APPS_SCRIPT_URL` and the random string into `APPS_SCRIPT_SECRET`.
   5. Upload a file from "Documents" on the admin page to try it. If it fails, the message on screen says which setting to check.

   If you edit the script later, open "Manage deployments" and update that deployment to a new version; only then does the same URL run the new code.

### Moving the Documents to Another Google Account Later

Neither the code nor the database changes, only settings:

1. Download the old files that are still needed from "Community Documents".
2. The new owner creates a folder in their own Google Drive and shares it with the service account's email (as a viewer).
3. The new owner, signed in as themselves, deploys an Apps Script as in step 6 above: `FOLDER_ID` is the new folder's id, and `SECRET` is a fresh random string.
4. In Render, replace `DRIVE_FOLDER_ID`, `APPS_SCRIPT_URL` and `APPS_SCRIPT_SECRET` with the three new values. Saving redeploys the service automatically.
5. Upload the files from step 1 again on the admin page, then check that "Community Documents" lists them all. Files uploaded from the admin page belong to the new account.
6. The old owner opens "Manage deployments" in Apps Script and archives the old deployment, which makes the old URL stop working.

### Keeping the Service Awake

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
| `APPS_SCRIPT_URL`, `APPS_SCRIPT_SECRET` | Optional, set both together | The URL and secret of the Apps Script that puts documents into Google Drive. With them, the admin page can upload and delete documents; without them, documents can still be read, but files have to be put into the Google Drive folder directly |
| `DOCUMENTS_DIR` | Required locally | A local documents folder; files uploaded from the admin page go straight into it. Mutually exclusive with the four above |
| `SESSION_IDLE_DAYS` | Optional, default 90 | Days of no use after which a session token expires |
| `PORT` | Optional, default 5180 | The port used when running locally |

## Part D. API Overview

Errors are always `{"error": "message for people", "code": "code"}`.

| Method and path | Who may use it | What it does |
|---|---|---|
| `GET /health` | Anyone | Whether the service is alive, and which version is running (the first seven characters of the git commit; `local` on your own machine). Doesn't touch the database. After pushing a new version, watch this value change to know the deploy has finished |
| `POST /auth/login` | Anyone | Exchanges Google's ID Token for a session token |
| `POST /auth/logout` | Anyone | Invalidates the token sent with the request |
| `GET /auth/status` | Signed in | The current status and role |
| `POST /auth/register` | Signed in, no account yet | Submits a display name and unit number, creating an account under review |
| `GET /announcements`, `GET /announcements/<id>` | Approved | The announcement list; one announcement. An announcement carries `author_role` (the author's current role) and `version` (to be sent back when editing) |
| `GET /announcements/<id>/images/<picture number>` | Approved | One picture of an announcement |
| `POST /announcements` | Moderator, administrator | Create an announcement, with up to five pictures |
| `PUT /announcements/<id>`, `DELETE /announcements/<id>` | Moderator, administrator | Edit and delete an announcement. An edit carries `version`; a mismatch answers 409 `edit_conflict` |
| `GET /posts`, `GET /posts/<id>` | Approved | The post list; one post with its replies |
| `POST /posts` | Approved | Create a post, with up to five pictures |
| `GET /posts/<id>/images/<picture number>` | Approved | One picture of a post |
| `PUT /posts/<id>`, `DELETE /posts/<id>` | The author, moderator, administrator | Edit and delete a post. An edit carries `version`; a mismatch answers 409 `edit_conflict` |
| `POST /posts/<id>/replies` | Approved | Reply |
| `DELETE /replies/<id>` | The author, moderator, administrator | Delete a reply |
| `GET /documents`, `GET /documents/<file_id>/download` | Approved | The document list; download |
| `GET /admin/users` | Moderator, administrator | The member list |
| `PUT /admin/users/<id>` | Moderator, administrator | Change display name, unit number, role and status. Carries `expected` (the four fields as the page loaded them); a mismatch answers 409 `member_changed`. Disabling invalidates all of the person's session tokens |
| `DELETE /admin/users/<id>/sessions` | Moderator, administrator (moderators can use it on regular users only) | Signs the person out of every device; answers `{"revoked": number of tokens invalidated}` |
| `GET /admin/documents` | Moderator, administrator | Whether the admin page can upload documents, the size limit per file, and the accepted file extensions |
| `POST /admin/documents` | Moderator, administrator | Upload a document: `multipart/form-data`, field name `file`, at most 10 MB, common documents and images only (pdf, Office, OpenDocument, txt, csv, jpg, png). A file with the same name is replaced |
| `DELETE /admin/documents/<file_id>` | Moderator, administrator | Delete a document |
