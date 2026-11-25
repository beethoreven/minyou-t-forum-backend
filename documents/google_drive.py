"""社區文檔的來源：Google Drive 的一個資料夾。正式環境用。

讀（清單、下載）用「服務帳號」：那是一個給程式用的 Google 帳號。資料夾的主人把資料夾
分享給它（檢視者），它就看得到那個資料夾，其他的都看不到。權限範圍只申請唯讀。
資料夾裡的檔案本身不公開，下載一律經過這裡，所以只有通過權限檢查的人拿得到。

寫（上傳、刪除）不能用服務帳號：服務帳號在 Google Drive 沒有自己的儲存空間，也不能擁有
檔案，就算把資料夾的編輯權限給它，它也沒辦法在個人的雲端硬碟裡建立檔案（Google 的規定）。
所以寫是請一支 Apps Script 代勞。那支指令碼由資料夾的主人部署、用主人的身分執行，檔案算
主人的。這裡跟它之間用一個只有兩邊知道的密語確認身分（指令碼在 tools/apps_script/documents.gs）。

資料夾是誰的、指令碼是誰部署的，這裡都不知道，也不需要知道：只認設定裡的資料夾代號、
指令碼網址與密語。之後要改由別的 Google 帳號存放文檔，換掉這三個設定就可以，程式不用改。
"""

import base64
import mimetypes
import re
from collections.abc import Iterator
from datetime import datetime

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from documents.base import DocumentFile, DocumentsUnavailable

FILES_URL = "https://www.googleapis.com/drive/v3/files"
FOLDER_URL = "https://drive.google.com/drive/folders"
SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)
TIMEOUT_SECONDS = 10
# 上傳要等 Apps Script 把整個檔案寫進 Google Drive，比一般的請求慢很多。
SCRIPT_TIMEOUT_SECONDS = 120
CHUNK_BYTES = 64 * 1024
PAGE_SIZE = 1000

# Google 文件、試算表、簡報、資料夾的類型都是這個開頭。它們沒有「原始檔」可以下載，所以不列。
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
# 檔案代號只會有這些字元。先檢查，瀏覽器傳來的東西才不會被拼進網址的其他位置。
FILE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{10,100}")
FILE_FIELDS = "id, name, mimeType, size, modifiedTime"

# Apps Script 說不行的時候，給後台的人看的說明：是哪一項設定要檢查。
SCRIPT_HINTS = {
    "forbidden": "密語對不上：APPS_SCRIPT_SECRET 要跟 Apps Script 指令碼屬性裡的 SECRET 一模一樣",
    "folder_unavailable": (
        "Apps Script 打不開文檔的資料夾：請確認指令碼屬性 FOLDER_ID 填的是那個資料夾的代號，"
        "而且部署 Apps Script 的帳號可以編輯那個資料夾"
    ),
    "busy": "同時有其他檔案正在上傳或刪除，請等一下再試一次",
}
SCRIPT_UNREADABLE_HINT = (
    "Apps Script 沒有照約定的格式回應：請確認 APPS_SCRIPT_URL 是部署後的網址（結尾是 /exec），"
    "而且部署時「誰可以存取」選的是「所有人」"
)
SCRIPT_OUTDATED_HINT = (
    "Apps Script 不認得這個要求：請確認貼進去的是最新版的指令碼，而且修改之後有重新部署新的版本"
)
HINT_DETAIL_MAX = 200


class GoogleDriveFolder:
    def __init__(
        self,
        folder_id: str,
        session,
        script_url: str | None = None,
        script_secret: str | None = None,
    ):
        """session：已經帶著服務帳號身分的 HTTP session。

        script_url、script_secret：代為上傳與刪除的 Apps Script。沒有設定就只能讀。
        """
        self.folder_id = folder_id
        self.session = session
        self.script_url = script_url
        self.script_secret = script_secret

    @classmethod
    def from_service_account(
        cls,
        info: dict,
        folder_id: str,
        script_url: str | None = None,
        script_secret: str | None = None,
    ) -> "GoogleDriveFolder":
        """info：服務帳號金鑰檔的內容。金鑰格式不對會丟 ValueError。"""
        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        return cls(folder_id, AuthorizedSession(credentials), script_url, script_secret)

    @property
    def can_upload(self) -> bool:
        return bool(self.script_url)

    @property
    def manage_url(self) -> str:
        """Google Drive 上那個資料夾的網址。還不能從後台上傳時，後台會請人直接去那裡放檔案。"""
        return f"{FOLDER_URL}/{self.folder_id}"

    def list_files(self) -> list[DocumentFile]:
        """資料夾裡的一般檔案，照檔名排序。"""
        files = []
        page_token = None
        # Google Drive 一次最多回 1000 筆。有下一頁就繼續拿，不能只拿第一頁就當成全部。
        while True:
            params = {
                "q": f"'{self.folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken, files({FILE_FIELDS})",
                "pageSize": PAGE_SIZE,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            page = self._get(FILES_URL, params).json()
            files.extend(_to_file(item) for item in page.get("files", []) if _is_regular(item))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        files.sort(key=lambda file: file.name)
        return files

    def open_file(self, file_id: str) -> tuple[DocumentFile, Iterator[bytes]] | None:
        if not FILE_ID_PATTERN.fullmatch(file_id):
            return None
        url = f"{FILES_URL}/{file_id}"

        # 先確認這個檔案真的在指定的資料夾裡。服務帳號看得到的檔案不一定只有這個資料夾的，
        # 不檢查的話，知道代號的人就能下載到別的檔案。
        response = self._get(
            url,
            {"fields": f"{FILE_FIELDS}, parents, trashed", "supportsAllDrives": "true"},
            missing_ok=True,
        )
        if response is None:
            return None
        item = response.json()
        in_folder = self.folder_id in item.get("parents", [])
        if not in_folder or item.get("trashed") or not _is_regular(item):
            return None

        download = self._get(
            url, {"alt": "media", "supportsAllDrives": "true"}, missing_ok=True, stream=True
        )
        if download is None:
            return None
        return _to_file(item), _read_chunks(download)

    def save(self, name: str, content: bytes) -> None:
        """把檔案放進資料夾。已經有同名的檔案時，指令碼會把舊的丟進垃圾桶。"""
        self._call_script(
            {
                "action": "upload",
                "name": name,
                # 檔案類型從副檔名判斷，不用瀏覽器說的：Google Drive 靠它決定怎麼預覽這個檔案。
                "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "data": base64.b64encode(content).decode("ascii"),
            }
        )

    def delete(self, file_id: str) -> bool:
        """把檔案丟進 Google Drive 的垃圾桶（30 天內主人還救得回來）。回傳有沒有這個檔案。"""
        if not FILE_ID_PATTERN.fullmatch(file_id):
            return False
        return self._call_script({"action": "trash", "id": file_id}, missing_ok=True)

    def _get(self, url: str, params: dict, *, missing_ok: bool = False, stream: bool = False):
        """呼叫 Google Drive。404 而且 missing_ok 時回傳 None，其他失敗都算暫時讀不到。"""
        try:
            response = self.session.get(url, params=params, stream=stream, timeout=TIMEOUT_SECONDS)
        except (requests.RequestException, OSError) as error:
            raise DocumentsUnavailable() from error
        if response.status_code == 404 and missing_ok:
            response.close()
            return None
        if response.status_code != 200:
            response.close()
            raise DocumentsUnavailable(f"Google Drive 回應 {response.status_code}")
        return response

    def _call_script(self, payload: dict, *, missing_ok: bool = False) -> bool:
        """請 Apps Script 做一件事。做到了回傳 True；它說找不到檔案、而且 missing_ok 時回傳 False。"""
        if not self.script_url:
            raise DocumentsUnavailable("沒有設定 Apps Script")
        try:
            response = requests.post(
                self.script_url,
                json=payload | {"secret": self.script_secret},
                timeout=SCRIPT_TIMEOUT_SECONDS,
            )
        except (requests.RequestException, OSError) as error:
            raise DocumentsUnavailable("連不上 Apps Script") from error
        if response.status_code >= 500:
            raise DocumentsUnavailable(f"Apps Script 回應 {response.status_code}")

        # Apps Script 的網頁應用程式不管成功失敗都回 200，成不成功要看內容裡的 ok。
        # 回來的如果不是 JSON，通常是 Google 的登入頁或「找不到網頁」：網址或部署的設定不對。
        try:
            result = response.json() if response.status_code == 200 else None
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise DocumentsUnavailable(
                f"Apps Script 回應 {response.status_code}，內容不是約定的格式",
                hint=SCRIPT_UNREADABLE_HINT,
            )

        if result.get("ok") is True:
            return True
        problem = result.get("error")
        if problem == "not_found" and missing_ok:
            return False
        if problem == "failed":
            detail = str(result.get("message", ""))[:HINT_DETAIL_MAX]
            raise DocumentsUnavailable("Apps Script 執行時出錯", hint=f"Apps Script 執行時出錯：{detail}")
        raise DocumentsUnavailable(
            f"Apps Script 回報 {str(problem)[:HINT_DETAIL_MAX]}",
            hint=SCRIPT_HINTS.get(problem, SCRIPT_OUTDATED_HINT),
        )


def _is_regular(item: dict) -> bool:
    return not item.get("mimeType", "").startswith(GOOGLE_NATIVE_PREFIX)


def _to_file(item: dict) -> DocumentFile:
    return DocumentFile(
        id=item["id"],
        name=item["name"],
        size=int(item.get("size", 0)),
        modified_time=datetime.fromisoformat(item["modifiedTime"]),
    )


def _read_chunks(response) -> Iterator[bytes]:
    try:
        yield from response.iter_content(CHUNK_BYTES)
    finally:
        response.close()
