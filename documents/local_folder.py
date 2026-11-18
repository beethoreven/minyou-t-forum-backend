"""社區文檔的來源：一個本機資料夾。本機開發與測試用，放進資料夾的檔案就是清單上的文件。

下載時不接受瀏覽器傳來的檔名或路徑，只接受清單裡出現過的代號（id）。這樣不管對方傳什麼，
都拿不到資料夾以外的檔案。
"""

import hashlib
import unicodedata
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from documents.base import DocumentFile, DocumentsUnavailable

CHUNK_BYTES = 64 * 1024


class LocalFolder:
    def __init__(self, folder: Path):
        self.folder = folder

    def list_files(self) -> list[DocumentFile]:
        """資料夾裡的檔案，照檔名排序。子資料夾、隱藏檔（例如 .DS_Store）、捷徑都不列。"""
        return [file for file, _ in self._scan()]

    def open_file(self, file_id: str) -> tuple[DocumentFile, Iterator[bytes]] | None:
        for file, path in self._scan():
            if file.id != file_id:
                continue
            try:
                handle = path.open("rb")
            except FileNotFoundError:
                return None  # 查到之後、打開之前，檔案剛好被移走
            except OSError as error:
                raise DocumentsUnavailable() from error
            return file, _read_chunks(handle)
        return None

    def manage_info(self) -> dict:
        """本機資料夾沒有網址可以開，只告訴後台資料夾在哪裡。"""
        return {"source": "local_folder", "manage_url": None, "location": str(self.folder)}

    def _scan(self) -> list[tuple[DocumentFile, Path]]:
        found = []
        try:
            for entry in self.folder.iterdir():
                if entry.name.startswith(".") or entry.is_symlink() or not entry.is_file():
                    continue
                # macOS 存檔名時可能把一個字拆成「字母＋附加符號」兩個字元。統一成合起來的
                # 形式，顯示與下載的檔名才會一致。
                name = unicodedata.normalize("NFC", entry.name)
                stat = entry.stat()
                file = DocumentFile(
                    id=_file_id(name),
                    name=name,
                    size=stat.st_size,
                    modified_time=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
                found.append((file, entry))
        except OSError as error:
            raise DocumentsUnavailable() from error
        found.sort(key=lambda pair: pair[0].name)
        return found


def _file_id(name: str) -> str:
    """檔名的代號。同一個檔名永遠得到同一個代號，而且只有英數字，可以直接放在網址裡。"""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def _read_chunks(handle) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk
