/**
 * 代替論壇把文章的附圖存進 Google Drive 的「Pic」資料夾。
 *
 * 為什麼需要這支指令碼：論壇的後端是用「服務帳號」讀 Google Drive 的，而服務帳號在
 * Google Drive 沒有自己的儲存空間，不能在別人的資料夾裡建立檔案。這支指令碼由資料夾的
 * 主人部署、用主人的身分執行，所以存進去的檔案算主人的。
 *
 * 部署方式（詳細步驟見這個 repo 的 README）：
 *   1. 到 https://script.google.com/home 建立新專案，把這個檔案的內容整個貼進去。
 *   2. 專案設定 → 指令碼屬性，加兩個：
 *        SECRET     自己取一串夠長的亂碼。後端的 APPS_SCRIPT_SECRET 要填同一個。
 *        FOLDER_ID  社區文檔那個資料夾的代號（跟後端的 DRIVE_FOLDER_ID 一樣）。
 *   3. 部署 → 新增部署作業 → 類型選「網頁應用程式」，
 *      執行身分選「我」，誰可以存取選「所有人」。
 *   4. 把部署後的網址填進後端的 APPS_SCRIPT_URL。
 *
 * 「所有人」可以連到這個網址，但沒有 SECRET 的請求一律拒絕，而且它只會動 Pic 資料夾。
 */

const PIC_FOLDER_NAME = "Pic";
const MAX_FILES = 5;

function doPost(e) {
  const properties = PropertiesService.getScriptProperties();
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (error) {
    return reply({ ok: false, error: "bad_request" });
  }

  const secret = properties.getProperty("SECRET");
  if (!secret || !body || body.secret !== secret) {
    return reply({ ok: false, error: "forbidden" });
  }

  const folder = picFolder(properties.getProperty("FOLDER_ID"));

  if (body.action === "upload") {
    if (!Array.isArray(body.files) || body.files.length === 0 || body.files.length > MAX_FILES) {
      return reply({ ok: false, error: "bad_request" });
    }
    const ids = body.files.map(function (file) {
      const blob = Utilities.newBlob(Utilities.base64Decode(file.data), "image/jpeg", file.name);
      return folder.createFile(blob).getId();
    });
    return reply({ ok: true, ids: ids });
  }

  if (body.action === "trash") {
    (body.ids || []).forEach(function (id) {
      try {
        const file = DriveApp.getFileById(id);
        // 只丟 Pic 資料夾裡的檔案，其他地方的不碰。
        if (isInFolder(file, folder)) file.setTrashed(true);
      } catch (error) {
        // 檔案已經不在了，不用處理。
      }
    });
    return reply({ ok: true });
  }

  return reply({ ok: false, error: "unknown_action" });
}

/** 文件資料夾底下的 Pic 資料夾。沒有就建立一個。 */
function picFolder(rootFolderId) {
  const root = DriveApp.getFolderById(rootFolderId);
  const found = root.getFoldersByName(PIC_FOLDER_NAME);
  return found.hasNext() ? found.next() : root.createFolder(PIC_FOLDER_NAME);
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
