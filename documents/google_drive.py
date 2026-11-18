"""社區文檔的來源：Google Drive 的一個資料夾。正式環境用。

用「服務帳號」讀：那是一個給程式用的 Google 帳號。資料夾的主人把資料夾分享給它（檢視者），
它就看得到那個資料夾，其他的都看不到。權限範圍只申請唯讀。

資料夾裡的檔案本身不公開，下載一律經過這裡，所以只有通過權限檢查的人拿得到。
"""

import re
from collections.abc import Iterator
from datetime import datetime

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from documents.base import DocumentFile, DocumentsUnavailable

FILES_URL = "https://www.googleapis.com/drive/v3/files"
SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)
TIMEOUT_SECONDS = 10
CHUNK_BYTES = 64 * 1024
PAGE_SIZE = 1000

# Google 文件、試算表、簡報、資料夾的類型都是這個開頭。它們沒有「原始檔」可以下載，所以不列。
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
# 檔案代號只會有這些字元。先檢查，瀏覽器傳來的東西才不會被拼進網址的其他位置。
FILE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{10,100}")
FILE_FIELDS = "id, name, mimeType, size, modifiedTime"


class GoogleDriveFolder:
    def __init__(self, folder_id: str, session):
        """session：已經帶著服務帳號身分的 HTTP session。"""
        self.folder_id = folder_id
        self.session = session

    @classmethod
    def from_service_account(cls, info: dict, folder_id: str) -> "GoogleDriveFolder":
        """info：服務帳號金鑰檔的內容。金鑰格式不對會丟 ValueError。"""
        credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        return cls(folder_id, AuthorizedSession(credentials))

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

    def manage_info(self) -> dict:
        """後台的「新增文檔」會開這個網址：Google Drive 上的那個資料夾。

        不經過論壇上傳，是因為服務帳號在 Google Drive 沒有自己的儲存空間，沒辦法把檔案
        放進別人的資料夾。由資料夾的主人（或他給了編輯權限的人）直接放進去，檔案算他的。
        """
        return {
            "source": "google_drive",
            "manage_url": f"https://drive.google.com/drive/folders/{self.folder_id}",
            "location": None,
        }

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
