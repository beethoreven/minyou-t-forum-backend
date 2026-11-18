"""社區文檔的來源共用的東西。

每一種來源（本機資料夾、Google Drive）都提供同樣的介面，路由不用知道現在用的是哪一種：

    list_files()         文件清單
    open_file(file_id)   回傳（文件, 內容），找不到回傳 None。內容是一段一段的 bytes，
                         邊讀邊送給瀏覽器，不用整個檔案先讀進記憶體。
    can_upload           能不能從後台上傳與刪除
    manage_url           不能從後台上傳時，要去哪個網址放檔案（沒有就是 None）
    save(name, content)  放進一個檔案。已經有同名的檔案就換成新的
    delete(file_id)      刪掉一個檔案，回傳有沒有刪到

文件有哪些，一律以存放的地方為準，資料庫裡不記。所以直接在存放的地方增減檔案也可以，
把存放的地方整個換掉（換一個資料夾、換一個帳號）也只是改設定。
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
    """讀不到或寫不進文件。

    大多是暫時的（資料夾不見了、Google Drive 連不上），稍後再試可能就好了，這時 hint 是 None。

    如果是設定不對、等多久都不會好，就把該檢查什麼寫在 hint。hint 會顯示給後台的人看，
    所以只能寫設定的名稱與該怎麼做，不能帶出設定的值。訊息本身（message）只進伺服器的 log。
    """

    def __init__(self, message: str = "", hint: str | None = None):
        super().__init__(message)
        self.hint = hint
