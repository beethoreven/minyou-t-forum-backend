"""社區文檔的來源共用的東西。

每一種來源（本機資料夾、Google Drive）都提供同樣兩個方法，路由不用知道現在用的是哪一種：

    list_files()        文件清單
    open_file(file_id)  回傳（文件, 內容），找不到回傳 None。內容是一段一段的 bytes，
                        邊讀邊送給瀏覽器，不用整個檔案先讀進記憶體。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DocumentFile:
    id: str
    name: str
    size: int
    modified_time: datetime


class DocumentsUnavailable(Exception):
    """暫時讀不到文件：資料夾不見了，或 Google Drive 連不上。"""
