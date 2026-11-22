"""從後台上傳過的文檔的紀錄（document_uploads 表），用來限制每個人上傳的量。

文檔本身不在這裡，在存放的地方（documents/）。這裡只記誰、多大、什麼時候。
"""


def record(conn, user_id: int, size_bytes: int) -> int:
    """記下一次上傳，回傳這筆紀錄的編號。上傳沒有成功的話，用 forget 拿掉。"""
    row = conn.execute(
        "INSERT INTO document_uploads (user_id, size_bytes) VALUES (%s, %s) RETURNING id",
        (user_id, size_bytes),
    ).fetchone()
    return row["id"]


def forget(conn, upload_id: int) -> None:
    conn.execute("DELETE FROM document_uploads WHERE id = %s", (upload_id,))


def count_since(conn, user_id: int, seconds: int) -> int:
    """這個人在最近這段時間內上傳了幾個檔案。"""
    row = conn.execute(
        """
        SELECT count(*) AS total FROM document_uploads
        WHERE user_id = %s AND created_at > now() - make_interval(secs => %s)
        """,
        (user_id, seconds),
    ).fetchone()
    return row["total"]


def bytes_since(conn, user_id: int, seconds: int) -> int:
    """這個人在最近這段時間內一共上傳了多少位元組。"""
    row = conn.execute(
        """
        SELECT coalesce(sum(size_bytes), 0) AS total FROM document_uploads
        WHERE user_id = %s AND created_at > now() - make_interval(secs => %s)
        """,
        (user_id, seconds),
    ).fetchone()
    return int(row["total"])
