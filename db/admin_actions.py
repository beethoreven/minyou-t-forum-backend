"""管理操作紀錄（admin_actions 表）。

記版主與管理員做的事（發公告、刪別人的文章、審核、改角色、停用、上傳文檔……），
也記被拒絕的特權操作。每一筆只有：時間、誰、動作代碼、對象、成功或被拒。

成功的操作跟操作本身寫在同一筆交易裡，要嘛一起留下，要嘛一起消失。
被拒絕的操作，原本的交易會因為錯誤而取消，所以另外開一筆交易寫（見 app.py 的 privileged）。

保留 180 天：每次寫入時順便刪掉更舊的，不另外排程。
"""

PAGE_SIZE = 50
RETENTION_DAYS = 180
# 同一個人、同一種動作被拒絕，這段時間內只記一筆。被拒絕的請求誰都送得出來（包含還沒審核的人），
# 不限制的話，一直送就能把紀錄灌滿。
DENIED_REPEAT_SECONDS = 60


def record(conn, actor: dict, action: str, target_id=None) -> int:
    """記下一筆成功的操作，回傳紀錄的編號。actor 是 users 表的一列。"""
    return _insert(conn, actor["id"], actor["email"], action, target_id, "ok")


def record_denied(conn, actor_id: int | None, actor_email: str, action: str, target_id=None) -> None:
    """記下一筆被拒絕的操作。同一個人、同一種動作在 DENIED_REPEAT_SECONDS 內已經記過就不再記。"""
    repeated = conn.execute(
        """
        SELECT 1 FROM admin_actions
        WHERE actor_email = %s AND action = %s AND outcome = 'denied'
          AND created_at > now() - make_interval(secs => %s)
        LIMIT 1
        """,
        (actor_email, action, DENIED_REPEAT_SECONDS),
    ).fetchone()
    if repeated is None:
        _insert(conn, actor_id, actor_email, action, target_id, "denied")


def forget(conn, record_id: int) -> None:
    """拿掉一筆紀錄。給「先記下、再去外面的服務做」的操作用：外面沒有做成，就把紀錄拿掉。"""
    conn.execute("DELETE FROM admin_actions WHERE id = %s", (record_id,))


def list_page(conn, before: int | None) -> tuple[list[dict], bool]:
    """一頁紀錄，新的在前面。回傳（這一頁的資料, 後面還有沒有）。before 的用法跟公告列表一樣。"""
    rows = conn.execute(
        """
        SELECT a.id, a.created_at, a.actor_id, a.actor_email, u.display_name AS actor_name,
               a.action, a.target_id, a.outcome
        FROM admin_actions a LEFT JOIN users u ON u.id = a.actor_id
        WHERE %(before)s::bigint IS NULL OR a.id < %(before)s::bigint
        ORDER BY a.id DESC LIMIT %(limit)s
        """,
        {"before": before, "limit": PAGE_SIZE + 1},
    ).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def _insert(conn, actor_id, actor_email, action, target_id, outcome) -> int:
    conn.execute(
        "DELETE FROM admin_actions WHERE created_at < now() - make_interval(days => %s)",
        (RETENTION_DAYS,),
    )
    row = conn.execute(
        """
        INSERT INTO admin_actions (actor_id, actor_email, action, target_id, outcome)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
        """,
        (actor_id, actor_email, action, None if target_id is None else str(target_id), outcome),
    ).fetchone()
    return row["id"]
