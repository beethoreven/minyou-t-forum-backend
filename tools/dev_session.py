"""本機開發用：不經過 Google，直接替一個 email 發一張登入憑證。

    .venv/bin/python tools/dev_session.py someone@example.com

用途是在本機換不同身分測試（一般使用者、版主、還沒註冊的人），不用準備好幾個 Google 帳號。
它要連得到資料庫才能執行（讀 .env 的 DATABASE_URL），所以不是後門：連得到資料庫的人，
本來就什麼都改得了。

執行後會印出一行 JavaScript。在瀏覽器打開論壇的頁面，把那一行貼到開發者工具的主控台，
就會以那個 email 登入。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth_utils import sessions  # noqa: E402
from config import load_config  # noqa: E402
from db import users  # noqa: E402
from db.connection import connect  # noqa: E402

# 要跟 web 的 js/auth.js 存登入狀態用的名稱一樣。
STORAGE_KEY = "minyou_t_forum.session"


def main() -> None:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        sys.exit("用法：.venv/bin/python tools/dev_session.py someone@example.com")
    email = sys.argv[1].strip().lower()

    config = load_config()
    with connect(config.database_url) as conn:
        # 跟真的登入一樣：設定檔指定的第一位管理員不用審核，直接是管理員。
        if email == config.first_admin_email:
            users.ensure_first_admin(conn, email, "")
        token = sessions.create(conn, email)

    stored = json.dumps({"token": token, "email": email, "name": ""})
    print(f"localStorage.setItem({json.dumps(STORAGE_KEY)}, {json.dumps(stored)}); location.reload();")


if __name__ == "__main__":
    main()
