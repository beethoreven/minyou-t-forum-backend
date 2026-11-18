"""資料庫連線。

每個請求開一條新連線，用完就關，不用連線池。有些主機會把閒置的服務凍結，連線池裡的連線
醒來時可能已經斷了，下一個請求就卡在那裡。這個論壇的流量很小，每次重新連線的成本可以忽略。
"""

import psycopg
from psycopg.rows import dict_row

CONNECT_TIMEOUT_SECONDS = 10


def connect(database_url: str) -> psycopg.Connection:
    """開一條連線。搭配 with 使用：正常結束會 commit，發生例外會 rollback，最後一定會關閉連線。

    查詢結果的每一列是 dict，用欄位名稱取值。
    """
    return psycopg.connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
