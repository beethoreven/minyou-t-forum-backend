"""公告的讀寫。"""

PAGE_SIZE = 20


def list_page(conn, before: int | None) -> tuple[list[dict], bool]:
    """一頁公告，新的在前面。回傳（這一頁的資料, 後面還有沒有）。

    before 是上一頁最後一筆的 id，只拿 id 比它小的；None 代表從最新的開始。
    多拿一筆，是為了知道後面還有沒有資料，不用另外算總數。
    """
    if before is None:
        rows = conn.execute(
            "SELECT id, title, created_at FROM announcements ORDER BY id DESC LIMIT %s",
            (PAGE_SIZE + 1,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, title, created_at FROM announcements
            WHERE id < %s ORDER BY id DESC LIMIT %s
            """,
            (before, PAGE_SIZE + 1),
        ).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def get(conn, announcement_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT a.id, a.title, a.body, a.created_at, a.updated_at,
               a.author_id, u.display_name AS author_name
        FROM announcements a JOIN users u ON u.id = a.author_id
        WHERE a.id = %s
        """,
        (announcement_id,),
    ).fetchone()


def create(conn, title: str, body: str, author_id: int) -> int:
    row = conn.execute(
        "INSERT INTO announcements (title, body, author_id) VALUES (%s, %s, %s) RETURNING id",
        (title, body, author_id),
    ).fetchone()
    return row["id"]


def update(conn, announcement_id: int, title: str, body: str) -> bool:
    """回傳有沒有改到（False 代表沒有這則公告）。"""
    cursor = conn.execute(
        "UPDATE announcements SET title = %s, body = %s, updated_at = now() WHERE id = %s",
        (title, body, announcement_id),
    )
    return cursor.rowcount == 1


def delete(conn, announcement_id: int) -> bool:
    """回傳有沒有刪到（False 代表沒有這則公告）。"""
    cursor = conn.execute("DELETE FROM announcements WHERE id = %s", (announcement_id,))
    return cursor.rowcount == 1


def add_images(conn, announcement_id: int, file_ids: list[str]) -> None:
    """記下這則公告的附圖。file_ids 是圖片的代號（見 db/pictures.py），照上傳的順序。"""
    for file_id in file_ids:
        conn.execute(
            "INSERT INTO announcement_images (announcement_id, file_id) VALUES (%s, %s)",
            (announcement_id, file_id),
        )


def list_images(conn, announcement_id: int) -> list[dict]:
    """一則公告的附圖，照上傳的順序。"""
    return conn.execute(
        "SELECT id, file_id FROM announcement_images WHERE announcement_id = %s ORDER BY id",
        (announcement_id,),
    ).fetchall()


def get_image_file_id(conn, announcement_id: int, image_id: int) -> str | None:
    """某一張附圖的代號。這張圖不屬於這則公告就回傳 None。"""
    row = conn.execute(
        "SELECT file_id FROM announcement_images WHERE id = %s AND announcement_id = %s",
        (image_id, announcement_id),
    ).fetchone()
    return row["file_id"] if row else None
