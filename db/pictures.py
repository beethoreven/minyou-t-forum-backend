"""附圖的內容，公告與文章共用。

圖片存在資料庫的 stored_pictures 表。web 上傳前已經把圖片縮到 480P，一張只有幾十 KB，
放資料庫負擔不大，也不用另外申請、設定任何儲存服務。

哪一張圖屬於哪一則公告、哪一篇文章，記在 announcement_images 與 post_images 兩張表
（讀寫在 db/announcements.py、db/posts.py），那裡只記這張表的代號。瀏覽器要看圖時給的是
那兩張表裡的編號，不是這裡的代號，所以沒辦法用猜的方式拿到別人的圖。
"""

import secrets


def save(conn, images: list[bytes]) -> list[str]:
    """把幾張圖片存起來，回傳每一張的代號，順序跟傳進來的一樣。"""
    picture_ids = []
    for content in images:
        picture_id = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO stored_pictures (id, content) VALUES (%s, %s)",
            (picture_id, content),
        )
        picture_ids.append(picture_id)
    return picture_ids


def get(conn, picture_id: str) -> bytes | None:
    """一張圖片的內容。找不到回傳 None。"""
    row = conn.execute(
        "SELECT content FROM stored_pictures WHERE id = %s",
        (picture_id,),
    ).fetchone()
    return bytes(row["content"]) if row else None


def delete(conn, picture_ids: list[str]) -> None:
    conn.execute("DELETE FROM stored_pictures WHERE id = ANY(%s)", (picture_ids,))
