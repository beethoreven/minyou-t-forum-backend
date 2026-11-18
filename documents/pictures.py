"""文章附圖存在哪裡。

三種存放的地方，介面相同，路由不用知道現在用的是哪一種：

    save(images)   把幾張圖片存起來，回傳每一張的代號
    open(代號)     回傳圖片的內容（一段一段的 bytes），找不到回傳 None
    delete(代號們)  刪掉圖片。盡力而為，失敗不算錯

    DatabasePictures  存在資料庫。正式環境預設用它：不需要任何額外的服務或設定。
    DrivePictures     存在 Google Drive 文件資料夾底下的 Pic。要另外部署一支 Apps Script 才能用。
    LocalPictures     存在本機文件資料夾底下的 Pic。本機開發與測試用。

圖片的代號只存在資料庫（post_images 表）。瀏覽器要看圖時給的是資料庫裡那一筆的編號，
不是這裡的代號，所以沒辦法用猜的方式拿到別的檔案。
"""

import base64
import re
import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import requests

from db.connection import connect
from documents.google_drive import FILE_ID_PATTERN, FILES_URL, TIMEOUT_SECONDS

PIC_FOLDER_NAME = "Pic"
# 存在資料庫的圖片，代號都是這個開頭。冒號不會出現在 Google Drive 的檔案代號裡，
# 所以看代號就知道一張圖在哪裡。
DATABASE_ID_PREFIX = "db:"
CHUNK_BYTES = 64 * 1024
# 上傳要等 Apps Script 把檔案寫進 Google Drive，比一般的請求慢，所以等久一點。
UPLOAD_TIMEOUT_SECONDS = 60
LOCAL_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class PicturesUnavailable(Exception):
    """暫時存不了或讀不到圖片。"""


class LocalPictures:
    """本機開發與測試用：圖片放在文件資料夾底下的 Pic 資料夾。"""

    def __init__(self, documents_dir: Path):
        self.folder = documents_dir / PIC_FOLDER_NAME

    def save(self, images: list[bytes]) -> list[str]:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            ids = []
            for content in images:
                picture_id = secrets.token_hex(16)
                (self.folder / f"{picture_id}.jpg").write_bytes(content)
                ids.append(picture_id)
            return ids
        except OSError as error:
            raise PicturesUnavailable() from error

    def open(self, picture_id: str) -> Iterator[bytes] | None:
        if not LOCAL_ID_PATTERN.fullmatch(picture_id):
            return None
        try:
            handle = (self.folder / f"{picture_id}.jpg").open("rb")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise PicturesUnavailable() from error
        return _read_file(handle)

    def delete(self, picture_ids: list[str]) -> None:
        for picture_id in picture_ids:
            if not LOCAL_ID_PATTERN.fullmatch(picture_id):
                continue
            try:
                (self.folder / f"{picture_id}.jpg").unlink(missing_ok=True)
            except OSError:
                pass  # 刪不掉不算錯，見上面的說明


class DatabasePictures:
    """圖片存在資料庫的 stored_pictures 表。

    圖片上傳前已經縮到 480P，一張只有幾十 KB，放資料庫負擔不大，而且不用另外申請、
    設定任何儲存服務。每次操作自己開一條連線，用完就關。
    """

    def __init__(self, database_url: str):
        self.database_url = database_url

    def save(self, images: list[bytes]) -> list[str]:
        ids = []
        try:
            # 同一筆交易：要嘛每一張都存進去，要嘛一張都沒有。
            with connect(self.database_url) as conn:
                for content in images:
                    picture_id = DATABASE_ID_PREFIX + secrets.token_hex(16)
                    conn.execute(
                        "INSERT INTO stored_pictures (id, content) VALUES (%s, %s)",
                        (picture_id, content),
                    )
                    ids.append(picture_id)
        except psycopg.Error as error:
            raise PicturesUnavailable() from error
        return ids

    def open(self, picture_id: str) -> Iterator[bytes] | None:
        try:
            with connect(self.database_url) as conn:
                row = conn.execute(
                    "SELECT content FROM stored_pictures WHERE id = %s",
                    (picture_id,),
                ).fetchone()
        except psycopg.Error as error:
            raise PicturesUnavailable() from error
        return iter([bytes(row["content"])]) if row else None

    def delete(self, picture_ids: list[str]) -> None:
        try:
            with connect(self.database_url) as conn:
                conn.execute("DELETE FROM stored_pictures WHERE id = ANY(%s)", (picture_ids,))
        except psycopg.Error:
            pass  # 刪不掉不算錯，只是多留幾張沒人用的圖


class PictureStores:
    """正式環境用：決定一張圖該找哪一個存放處。

    新的圖存進 writer。讀與刪看代號：db: 開頭的在資料庫，其他的在 Google Drive。
    所以存放處換過之後（例如後來才設定了 Apps Script），以前存的圖還是找得到。
    """

    def __init__(self, writer, database: DatabasePictures, drive: "DrivePictures"):
        self.writer = writer
        self.database = database
        self.drive = drive

    def save(self, images: list[bytes]) -> list[str]:
        return self.writer.save(images)

    def open(self, picture_id: str) -> Iterator[bytes] | None:
        return self._store_for(picture_id).open(picture_id)

    def delete(self, picture_ids: list[str]) -> None:
        in_database = [item for item in picture_ids if item.startswith(DATABASE_ID_PREFIX)]
        in_drive = [item for item in picture_ids if not item.startswith(DATABASE_ID_PREFIX)]
        if in_database:
            self.database.delete(in_database)
        if in_drive:
            self.drive.delete(in_drive)

    def _store_for(self, picture_id: str):
        return self.database if picture_id.startswith(DATABASE_ID_PREFIX) else self.drive


class DrivePictures:
    """圖片放在 Google Drive 文件資料夾底下的 Pic 資料夾。

    讀圖用服務帳號，跟讀文件一樣。

    存圖不能用服務帳號：服務帳號在 Google Drive 沒有自己的儲存空間，也不能擁有檔案，
    就算把資料夾的編輯權限給它，它也沒辦法在個人的雲端硬碟裡建立檔案（Google 的規定）。
    所以存圖是請一支 Apps Script 代勞。那支指令碼由資料夾的主人部署、用主人的身分執行，
    檔案算主人的。這裡跟它之間用一個只有兩邊知道的密語確認身分
    （指令碼的內容在 tools/apps_script/pictures.gs）。

    沒有設定那支指令碼時（script_url 是 None），這裡只能讀與刪不了、存不了；
    新的圖會改存資料庫，見 PictureStores。
    """

    def __init__(self, session, script_url: str | None, script_secret: str | None):
        """session：已經帶著服務帳號身分的 HTTP session。"""
        self.session = session
        self.script_url = script_url
        self.script_secret = script_secret

    def save(self, images: list[bytes]) -> list[str]:
        files = [
            {
                "name": f"{secrets.token_hex(16)}.jpg",
                "content_type": "image/jpeg",
                "data": base64.b64encode(content).decode("ascii"),
            }
            for content in images
        ]
        ids = self._call_script({"action": "upload", "files": files}).get("ids")
        is_valid = (
            isinstance(ids, list)
            and len(ids) == len(images)
            and all(isinstance(item, str) and FILE_ID_PATTERN.fullmatch(item) for item in ids)
        )
        if not is_valid:
            raise PicturesUnavailable("Apps Script 回傳的內容不對")
        return ids

    def open(self, picture_id: str) -> Iterator[bytes] | None:
        if not FILE_ID_PATTERN.fullmatch(picture_id):
            return None
        try:
            response = self.session.get(
                f"{FILES_URL}/{picture_id}",
                params={"alt": "media", "supportsAllDrives": "true"},
                stream=True,
                timeout=TIMEOUT_SECONDS,
            )
        except (requests.RequestException, OSError) as error:
            raise PicturesUnavailable() from error
        if response.status_code == 404:
            response.close()
            return None
        if response.status_code != 200:
            response.close()
            raise PicturesUnavailable(f"Google Drive 回應 {response.status_code}")
        return _read_response(response)

    def delete(self, picture_ids: list[str]) -> None:
        # 文章已經刪掉了，圖片刪不掉只是在 Google Drive 多留幾個沒人用的檔案，不值得讓刪文失敗。
        try:
            self._call_script({"action": "trash", "ids": picture_ids})
        except PicturesUnavailable:
            pass

    def _call_script(self, payload: dict) -> dict:
        if not self.script_url:
            raise PicturesUnavailable("沒有設定 Apps Script")
        try:
            response = requests.post(
                self.script_url,
                json=payload | {"secret": self.script_secret},
                timeout=UPLOAD_TIMEOUT_SECONDS,
            )
            result = response.json() if response.status_code == 200 else None
        except (requests.RequestException, ValueError, OSError) as error:
            raise PicturesUnavailable() from error
        # Apps Script 的網頁應用程式不管成功失敗都回 200，成不成功要看內容裡的 ok。
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise PicturesUnavailable("Apps Script 沒有回報成功")
        return result


def _read_file(handle) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk


def _read_response(response) -> Iterator[bytes]:
    try:
        yield from response.iter_content(CHUNK_BYTES)
    finally:
        response.close()
