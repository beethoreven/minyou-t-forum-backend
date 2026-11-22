"""成員（users 表）的讀寫。

帳號就是 Google 驗證過的 email，一律存小寫。
"""

ROLES = ("member", "moderator", "admin")  # 一般使用者、版主、管理員
STATUSES = ("pending", "approved", "disabled")  # 審核中、已審核、已停用

_COLUMNS = "id, email, display_name, unit_no, role, status, created_at"


def get_by_email(conn, email: str) -> dict | None:
    return conn.execute(f"SELECT {_COLUMNS} FROM users WHERE email = %s", (email,)).fetchone()


def get(conn, user_id: int) -> dict | None:
    return conn.execute(f"SELECT {_COLUMNS} FROM users WHERE id = %s", (user_id,)).fetchone()


def list_all(conn) -> list[dict]:
    """全部成員。審核中的排最前面（等著處理），其他照加入的先後。"""
    return conn.execute(
        f"SELECT {_COLUMNS} FROM users ORDER BY (status = 'pending') DESC, id"
    ).fetchall()


def register(conn, email: str, display_name: str, unit_no: str) -> dict | None:
    """自己註冊：一律是一般使用者、審核中。這個 email 已經有帳號就回傳 None。

    角色與狀態寫死在這裡，不接受呼叫端指定：開放的話，任何人都能把自己註冊成管理員。
    已經有帳號（不管什麼狀態）就不能再註冊：否則被停用的人重新註冊一次就復活了。
    """
    return conn.execute(
        f"""
        INSERT INTO users (email, display_name, unit_no, role, status)
        VALUES (%s, %s, %s, 'member', 'pending')
        ON CONFLICT (email) DO NOTHING
        RETURNING {_COLUMNS}
        """,
        (email, display_name, unit_no),
    ).fetchone()


def ensure_first_admin(conn, email: str, display_name: str) -> None:
    """設定檔指定的第一位管理員：只在論壇裡還沒有任何管理員的時候，把他設成已審核的管理員。

    他每次登入都會呼叫，但已經有管理員之後就什麼都不做。所以其他管理員停用或降級這個帳號
    之後，他重新登入也不會自己恢復：這個帳號被盜的時候，別的管理員鎖得住它。
    """
    conn.execute(
        """
        INSERT INTO users (email, display_name, role, status)
        SELECT %s, %s, 'admin', 'approved'
        WHERE NOT EXISTS (SELECT 1 FROM users WHERE role = 'admin')
        ON CONFLICT (email) DO UPDATE SET role = 'admin', status = 'approved'
        """,
        (email, display_name or "管理員"),
    )


def get_for_update(conn, user_id: int) -> dict | None:
    """讀一位成員，並鎖住這一筆到交易結束。

    後台改帳號時用：先檢查「能不能改」，再寫入。鎖住之後，這段期間別人改不了這一筆，
    檢查時看到的就是寫入前最新的內容。
    """
    return conn.execute(f"SELECT {_COLUMNS} FROM users WHERE id = %s FOR UPDATE", (user_id,)).fetchone()


def update(conn, user_id: int, display_name: str, unit_no: str, role: str, status: str) -> None:
    conn.execute(
        """
        UPDATE users SET display_name = %s, unit_no = %s, role = %s, status = %s
        WHERE id = %s
        """,
        (display_name, unit_no, role, status, user_id),
    )
