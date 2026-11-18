"""論壇文章與回覆的讀寫。"""

import psycopg

PAGE_SIZE = 20

_LIST_QUERY = """
    SELECT p.id, p.title, p.created_at, u.display_name AS author_name,
           (SELECT count(*) FROM replies r WHERE r.post_id = p.id) AS reply_count
    FROM posts p JOIN users u ON u.id = p.author_id
"""


def list_page(conn, before: int | None) -> tuple[list[dict], bool]:
    """一頁文章，新的在前面。回傳（這一頁的資料, 後面還有沒有）。

    before 是上一頁最後一筆的 id，只拿 id 比它小的；None 代表從最新的開始。
    """
    if before is None:
        rows = conn.execute(
            _LIST_QUERY + " ORDER BY p.id DESC LIMIT %s",
            (PAGE_SIZE + 1,),
        ).fetchall()
    else:
        rows = conn.execute(
            _LIST_QUERY + " WHERE p.id < %s ORDER BY p.id DESC LIMIT %s",
            (before, PAGE_SIZE + 1),
        ).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def get(conn, post_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT p.id, p.title, p.body, p.created_at, p.updated_at,
               p.author_id, u.display_name AS author_name
        FROM posts p JOIN users u ON u.id = p.author_id
        WHERE p.id = %s
        """,
        (post_id,),
    ).fetchone()


def create(conn, title: str, body: str, author_id: int) -> int:
    row = conn.execute(
        "INSERT INTO posts (title, body, author_id) VALUES (%s, %s, %s) RETURNING id",
        (title, body, author_id),
    ).fetchone()
    return row["id"]


def update(conn, post_id: int, title: str, body: str) -> bool:
    """回傳有沒有改到（False 代表沒有這篇文章）。"""
    cursor = conn.execute(
        "UPDATE posts SET title = %s, body = %s, updated_at = now() WHERE id = %s",
        (title, body, post_id),
    )
    return cursor.rowcount == 1


def delete(conn, post_id: int) -> bool:
    """刪除文章，回覆會跟著一起刪除。回傳有沒有刪到。"""
    cursor = conn.execute("DELETE FROM posts WHERE id = %s", (post_id,))
    return cursor.rowcount == 1


def count_posts_since(conn, author_id: int, seconds: int) -> int:
    """這個人在最近這段時間內發了幾篇文章。限制發文數量用。"""
    row = conn.execute(
        """
        SELECT count(*) AS total FROM posts
        WHERE author_id = %s AND created_at > now() - make_interval(secs => %s)
        """,
        (author_id, seconds),
    ).fetchone()
    return row["total"]


def count_replies_since(conn, author_id: int, seconds: int) -> int:
    """這個人在最近這段時間內發了幾則回覆。"""
    row = conn.execute(
        """
        SELECT count(*) AS total FROM replies
        WHERE author_id = %s AND created_at > now() - make_interval(secs => %s)
        """,
        (author_id, seconds),
    ).fetchone()
    return row["total"]


def add_images(conn, post_id: int, file_ids: list[str]) -> None:
    """記下這篇文章的附圖。file_ids 是圖片存放處給的代號，照上傳的順序。"""
    for file_id in file_ids:
        conn.execute(
            "INSERT INTO post_images (post_id, file_id) VALUES (%s, %s)",
            (post_id, file_id),
        )


def list_images(conn, post_id: int) -> list[dict]:
    """一篇文章的附圖，照上傳的順序。"""
    return conn.execute(
        "SELECT id, file_id FROM post_images WHERE post_id = %s ORDER BY id",
        (post_id,),
    ).fetchall()


def get_image_file_id(conn, post_id: int, image_id: int) -> str | None:
    """某一張附圖存放處的代號。這張圖不屬於這篇文章就回傳 None。"""
    row = conn.execute(
        "SELECT file_id FROM post_images WHERE id = %s AND post_id = %s",
        (image_id, post_id),
    ).fetchone()
    return row["file_id"] if row else None


def list_replies(conn, post_id: int) -> list[dict]:
    """一篇文章的全部回覆，舊的在前面。"""
    return conn.execute(
        """
        SELECT r.id, r.body, r.created_at, r.author_id, u.display_name AS author_name
        FROM replies r JOIN users u ON u.id = r.author_id
        WHERE r.post_id = %s
        ORDER BY r.id
        """,
        (post_id,),
    ).fetchall()


def get_reply(conn, reply_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT r.id, r.body, r.created_at, r.author_id, u.display_name AS author_name
        FROM replies r JOIN users u ON u.id = r.author_id
        WHERE r.id = %s
        """,
        (reply_id,),
    ).fetchone()


def create_reply(conn, post_id: int, body: str, author_id: int) -> int | None:
    """新增回覆，回傳回覆的 id。文章不存在就回傳 None。

    用 INSERT ... SELECT：文章存在才會真的寫入，不用先查一次再寫。
    文章如果剛好在這一刻被別人刪掉，資料庫會用外鍵擋下來，一樣當成文章不存在。
    """
    try:
        row = conn.execute(
            """
            INSERT INTO replies (post_id, body, author_id)
            SELECT p.id, %s::text, %s::bigint FROM posts p WHERE p.id = %s
            RETURNING id
            """,
            (body, author_id, post_id),
        ).fetchone()
    except psycopg.errors.ForeignKeyViolation:
        return None
    return row["id"] if row else None


def delete_reply(conn, reply_id: int) -> bool:
    """回傳有沒有刪到（False 代表沒有這則回覆）。"""
    cursor = conn.execute("DELETE FROM replies WHERE id = %s", (reply_id,))
    return cursor.rowcount == 1
