/**
 * 代替論壇把「社區文檔」的檔案放進 Google Drive 的資料夾，或把資料夾裡的檔案丟進垃圾桶。
 *
 * 為什麼需要這支指令碼：論壇的後端是用「服務帳號」讀 Google Drive 的，而服務帳號在
 * Google Drive 沒有自己的儲存空間，不能在別人的資料夾裡建立檔案。這支指令碼由資料夾的
 * 主人部署、用主人的身分執行，所以放進去的檔案算主人的。
 *
 * 部署方式（詳細步驟見這個 repo 的 README）：
 *   1. 用資料夾主人的 Google 帳號到 https://script.google.com/home 建立新專案，
 *      把這個檔案的內容整個貼進去。
 *   2. 專案設定 → 指令碼屬性，加兩個：
 *        SECRET     自己取一串夠長的亂碼。後端的 APPS_SCRIPT_SECRET 要填同一個。
 *        FOLDER_ID  社區文檔那個資料夾的代號（跟後端的 DRIVE_FOLDER_ID 一樣）。
 *   3. 部署 → 新增部署作業 → 類型選「網頁應用程式」，
 *      執行身分選「我」，誰可以存取選「所有人」。
 *   4. 把部署後的網址（結尾是 /exec）填進後端的 APPS_SCRIPT_URL。
 *
 * 「所有人」都連得到這個網址，但沒有 SECRET 的請求一律拒絕，而且它只會動 FOLDER_ID
 * 那一個資料夾裡的檔案。
 *
 * 這支指令碼裡沒有寫死任何帳號或資料夾。之後要改由別的 Google 帳號存放文檔，
 * 由那個帳號照同樣的步驟部署一份，再把後端的三個設定換成新的就可以。
 *
 * 跟後端的約定：請求是 JSON，一定回 JSON。成功是 { ok: true }，
 * 失敗是 { ok: false, error: "代碼" }。
 */

// 檔名的規則是後端在管的（最多 100 個字）。這裡只擋長得離譜的，所以抓得比較寬：
// 這裡的長度是用 UTF-16 的單位算的，一個表情符號算 2。
const MAX_NAME_LENGTH = 200;

// 只收這些副檔名（大小寫不分），跟後端的 DOCUMENT_EXTENSIONS 一樣。後端已經擋過一次；
// 這裡再擋一次，是為了 SECRET 萬一外洩時，別人也放不進住戶下載後會被直接執行的檔案。
const ALLOWED_EXTENSIONS = [
  ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
  ".odt", ".ods", ".odp", ".txt", ".csv", ".jpg", ".jpeg", ".png",
];

// 等鎖最多幾毫秒。一次上傳通常幾秒內做完。
const LOCK_WAIT_MS = 30000;

function doPost(e) {
  try {
    return reply(handle(e));
  } catch (error) {
    // 沒有預期到的錯誤（例如雲端硬碟滿了）也照約定的格式回報，後端才看得懂。
    return reply({ ok: false, error: "failed", message: String(error) });
  }
}

function handle(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (error) {
    return { ok: false, error: "bad_request" };
  }

  const properties = PropertiesService.getScriptProperties();
  const secret = properties.getProperty("SECRET");
  if (!secret || !body || body.secret !== secret) {
    return { ok: false, error: "forbidden" };
  }

  let folder;
  try {
    folder = DriveApp.getFolderById(properties.getProperty("FOLDER_ID"));
  } catch (error) {
    return { ok: false, error: "folder_unavailable" };
  }

  if (body.action !== "upload" && body.action !== "trash") return { ok: false, error: "unknown_action" };

  // 一次只做一件事：兩個同名的檔案同時上傳時，都會先看到「還沒有同名的舊檔」，
  // 結果兩份並存。拿到鎖才動手，後面那一個就看得到前面剛放進去的檔案，會把它換掉。
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(LOCK_WAIT_MS)) return { ok: false, error: "busy" };
  try {
    return body.action === "upload" ? upload(folder, body) : trash(folder, body);
  } finally {
    lock.releaseLock();
  }
}

function hasAllowedExtension(name) {
  const lower = name.toLowerCase();
  return ALLOWED_EXTENSIONS.some(function (extension) {
    return lower.slice(-extension.length) === extension;
  });
}

/** 把一個檔案放進資料夾。body：{ name: 檔名, content_type: 檔案類型, data: base64 編碼的內容 } */
function upload(folder, body) {
  const nameOk = typeof body.name === "string" && body.name.length > 0 && body.name.length <= MAX_NAME_LENGTH;
  if (!nameOk || typeof body.data !== "string" || typeof body.content_type !== "string") {
    return { ok: false, error: "bad_request" };
  }
  if (!hasAllowedExtension(body.name)) return { ok: false, error: "file_type_not_allowed" };

  // 先記下資料夾裡同名的舊檔案。
  const replaced = [];
  const sameName = folder.getFilesByName(body.name);
  while (sameName.hasNext()) replaced.push(sameName.next());

  const blob = Utilities.newBlob(Utilities.base64Decode(body.data), body.content_type, body.name);
  folder.createFile(blob);

  // 新的確定放進去之後，才把舊的丟進垃圾桶：中途失敗的話，舊的還在。
  replaced.forEach(function (file) {
    try {
      file.setTrashed(true);
    } catch (error) {
      // 別人放進來的檔案，主人不一定丟得掉。留著不處理，資料夾裡會有兩個同名的檔案。
    }
  });
  return { ok: true };
}

/** 把資料夾裡的一個檔案丟進垃圾桶（30 天內還救得回來）。body：{ id: 檔案的代號 } */
function trash(folder, body) {
  if (typeof body.id !== "string") return { ok: false, error: "bad_request" };
  let file;
  try {
    file = DriveApp.getFileById(body.id);
  } catch (error) {
    return { ok: false, error: "not_found" };
  }
  // 只動這個資料夾裡的檔案，其他地方的不碰。
  if (file.isTrashed() || !isInFolder(file, folder)) return { ok: false, error: "not_found" };
  file.setTrashed(true);
  return { ok: true };
}

function isInFolder(file, folder) {
  const parents = file.getParents();
  while (parents.hasNext()) {
    if (parents.next().getId() === folder.getId()) return true;
  }
  return false;
}

function reply(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
