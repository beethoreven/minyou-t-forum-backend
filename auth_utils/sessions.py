"""論壇自己的登入憑證。

登入時 Google 只負責證明「這個人是誰」。證明完之後發一張論壇自己的憑證給瀏覽器，
之後每個請求都帶這張憑證，不再經過 Google。

- 憑證是一串隨機的字，本身不帶任何資訊；它代表誰，要查 sessions 表才知道。
- 資料庫只存憑證的 SHA-256，不存原文：資料庫外洩時，別人拿不到能用的憑證。
- 有效期是「滑動」的：有在用就一直有效，連續好幾天完全沒用才失效。
- 這張憑證只回答「這是誰」。這個人現在能不能用論壇（審核過了沒、被停用了沒）是另一件事，
  每個請求都另外查 users 表（見 access.py），所以停用帳號馬上生效，不用等憑證過期。
"""

import hashlib
import secrets

# 最後使用時間不用每個請求都寫一次，超過這麼久才更新。
TOUCH_INTERVAL_SECONDS = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(conn, email: str) -> str:
    """發一張新的憑證，回傳原文。原文只有這一次拿得到。"""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, email) VALUES (%s, %s)",
        (_hash(token), email),
    )
    return token


def resolve(conn, token: str, idle_days: int) -> str | None:
    """憑證有效就回傳它代表的 email，無效（查不到，或太久沒用）回傳 None。"""
    token_hash = _hash(token)
    row = conn.execute(
        """
        SELECT email,
               last_seen_at < now() - make_interval(secs => %s) AS needs_touch
        FROM sessions
        WHERE token_hash = %s
          AND last_seen_at > now() - make_interval(days => %s)
        """,
        (TOUCH_INTERVAL_SECONDS, token_hash, idle_days),
    ).fetchone()
    if row is None:
        return None
    if row["needs_touch"]:
        conn.execute(
            "UPDATE sessions SET last_seen_at = now() WHERE token_hash = %s",
            (token_hash,),
        )
    return row["email"]


def delete(conn, token: str) -> None:
    """登出：讓這張憑證失效。憑證本來就不存在也沒關係，結果一樣是「它不能用了」。"""
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


def delete_idle(conn, idle_days: int) -> None:
    """清掉太久沒用的憑證。有人登入時順手做，不另外開排程。"""
    conn.execute(
        "DELETE FROM sessions WHERE last_seen_at <= now() - make_interval(days => %s)",
        (idle_days,),
    )
