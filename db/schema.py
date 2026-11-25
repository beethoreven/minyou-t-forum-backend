"""資料表的定義。服務啟動時執行一次，可以重複執行。

STATEMENTS 建立還不存在的表；已經存在的表，CREATE TABLE IF NOT EXISTS 什麼都不做。
既有的表要加欄位或限制時，寫進 MIGRATIONS：每一句都要能重複執行，而且不能因為正式資料庫裡
已經有的資料而失敗，否則服務會起不來。改型別這類修改還是要另外寫 SQL 手動執行。
"""

import logging

import psycopg

logger = logging.getLogger(__name__)

# 兩個行程同時啟動、一起建表時會互相撞到而失敗。建表前先拿這把鎖，一次只讓一個行程做。
SCHEMA_LOCK = "minyou-t-forum:schema"

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
    # 文章的附圖：哪一張圖屬於哪一篇文章。圖片的內容在 stored_pictures，這裡只記它的代號（file_id）。
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
    # 公告的附圖，跟文章的附圖是同一種做法。
    """
    CREATE TABLE IF NOT EXISTS announcement_images (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        announcement_id bigint NOT NULL REFERENCES announcements (id) ON DELETE CASCADE,
        file_id         text NOT NULL,
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS announcement_images_announcement_id_idx
        ON announcement_images (announcement_id)
    """,
    # 附圖的內容。上面兩張表的 file_id 指的就是這裡的 id。
    # 圖片上傳前已經縮小過，一張只有幾十 KB。
    """
    CREATE TABLE IF NOT EXISTS stored_pictures (
        id         text PRIMARY KEY,
        content    bytea NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # 從後台上傳過的文檔：誰、多大、什麼時候。文檔本身在存放的地方（見 documents/），
    # 這裡只用來算每個人在一段時間內傳了幾個、多少，限制上傳的量。
    """
    CREATE TABLE IF NOT EXISTS document_uploads (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id    bigint NOT NULL REFERENCES users (id),
        size_bytes bigint NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS document_uploads_user_idx ON document_uploads (user_id, created_at)",
    # 管理操作紀錄：版主與管理員做了什麼、被拒絕的特權操作。只記誰、做了哪一種事、對象、
    # 成功或被拒，不記內文、秘密、登入憑證、IP。保留 180 天（見 db/admin_actions.py）。
    #   actor_id：users.id；還沒註冊的人被拒絕時沒有 id，只記 email。
    #   target_id：對象的編號。文檔的對象是檔名或存放處的檔案代號，所以用 text。
    #   outcome：ok 成功、denied 被拒絕。
    """
    CREATE TABLE IF NOT EXISTS admin_actions (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        actor_id    bigint,
        actor_email text NOT NULL,
        action      text NOT NULL,
        target_id   text,
        outcome     text NOT NULL CHECK (outcome IN ('ok', 'denied')),
        created_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS admin_actions_created_at_idx ON admin_actions (created_at)",
)

MIGRATIONS = (
    # 公告與文章的版本號。每改一次加一；編輯時帶著改之前的版本號，對不上就代表
    # 在編輯的這段時間裡被別人改過，不能直接蓋掉。
    "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1",
    # 版主與管理員一定是已審核的帳號。後端改帳號時本來就會檢查（auth_utils/access.py），
    # 這裡讓資料庫也擋住，直接改資料庫的人也做不出這種組合。
    # 先用 NOT VALID 加上：只檢查之後寫入的資料，既有的資料不會讓這一句失敗。
    # 既有的資料在 ensure_schema 最後另外檢查（VALIDATE）。
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'users_staff_must_be_approved' AND conrelid = 'users'::regclass
        ) THEN
            ALTER TABLE users ADD CONSTRAINT users_staff_must_be_approved
                CHECK (role = 'member' OR status = 'approved') NOT VALID;
        END IF;
    END
    $$
    """,
)


def ensure_schema(conn) -> None:
    """建立還不存在的表、補上新的欄位與限制。可以重複執行。"""
    # 這把鎖在交易結束時自動放開。
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA_LOCK,))
    for statement in STATEMENTS + MIGRATIONS:
        conn.execute(statement)
    _validate_existing_users(conn)


def _validate_existing_users(conn) -> None:
    """確認既有的成員都符合「版主與管理員一定是已審核」。

    符合的話，這個限制就連既有的資料也算數。不符合不擋啟動：寫 log 說明，之後的寫入照樣會被擋，
    既有的那幾筆要有人看過再決定怎麼改。已經檢查過的，再執行一次幾乎不花時間。
    """
    try:
        with conn.transaction():
            conn.execute("ALTER TABLE users VALIDATE CONSTRAINT users_staff_must_be_approved")
    except psycopg.errors.CheckViolation:
        logger.warning(
            "users 表裡有版主或管理員不是已審核的帳號；之後的寫入會被擋，既有的資料請手動修正"
        )
