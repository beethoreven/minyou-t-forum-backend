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
