"""資料表的定義。服務啟動時執行一次，已經存在的表不會被動到。

要改既有的表（加欄位、改型別）不能只改這裡：CREATE TABLE IF NOT EXISTS 遇到已經存在的表
什麼都不做。那種修改要另外寫 ALTER TABLE 手動執行。
"""

# 兩個行程同時啟動、一起建表時會互相撞到而失敗。建表前先拿這把鎖，一次只讓一個行程做。
SCHEMA_LOCK = "minyou_t_forum:schema"

STATEMENTS = (
    # 成員。
    #   email：帳號，就是 Google 驗證過的 email，一律存小寫。
    #   status：pending 審核中、approved 已審核、disabled 已停用。
    #   role：member 一般使用者、moderator 版主、admin 管理員。
    """
    CREATE TABLE IF NOT EXISTS users (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        email        text NOT NULL UNIQUE,
        display_name text NOT NULL,
        unit_no      text NOT NULL DEFAULT '',
        role         text NOT NULL DEFAULT 'member'
                     CHECK (role IN ('member', 'moderator', 'admin')),
        status       text NOT NULL
                     CHECK (status IN ('pending', 'approved', 'disabled')),
        created_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 登入憑證。只存憑證的 SHA-256，不存原文。
    # 用 email 對應成員，不用 users.id：還沒註冊的人也要能登入，才能填註冊表單。
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash   text PRIMARY KEY,
        email        text NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 公告。
    """
    CREATE TABLE IF NOT EXISTS announcements (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        title      text NOT NULL,
        body       text NOT NULL,
        author_id  bigint NOT NULL REFERENCES users (id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 論壇的文章。
    """
    CREATE TABLE IF NOT EXISTS posts (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        title      text NOT NULL,
        body       text NOT NULL,
        author_id  bigint NOT NULL REFERENCES users (id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 文章底下的回覆。文章被刪除時，它的回覆一起刪除（ON DELETE CASCADE）。
    """
    CREATE TABLE IF NOT EXISTS replies (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        post_id    bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
        body       text NOT NULL,
        author_id  bigint NOT NULL REFERENCES users (id),
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 「某一篇文章的所有回覆」是最常用的查詢，幫它建索引。
    "CREATE INDEX IF NOT EXISTS replies_post_id_idx ON replies (post_id)",
    # 文章的附圖。圖片本身不在資料庫裡，這裡只記它存放處的代號（file_id）。
    # 文章被刪除時，這裡的紀錄一起刪除。
    """
    CREATE TABLE IF NOT EXISTS post_images (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        post_id    bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
        file_id    text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS post_images_post_id_idx ON post_images (post_id)",
)


def ensure_schema(conn) -> None:
    """建立還不存在的表。可以重複執行。"""
    # 這把鎖在交易結束時自動放開。
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA_LOCK,))
    for statement in STATEMENTS:
        conn.execute(statement)
