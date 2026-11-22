"""測試的共用設定。

測試需要一個真的 PostgreSQL，連線字串放在環境變數 TEST_DATABASE_URL。每個測試開始前都會
清空那個資料庫，所以它的名稱一定要以 _test 結尾，避免清到開發用的資料。

測試不會連到 Google：登入時的 Google 驗證換成下面的 fake_google。
"""

import os

import psycopg
import pytest
from flask.testing import FlaskClient
from psycopg.conninfo import conninfo_to_dict

from app import create_app
from auth_utils.google_auth import GoogleIdentity, VerificationUnavailable
from config import Config

WEB_ORIGIN = "http://localhost:8180"
ADMIN_EMAIL = "admin@example.com"


def fake_google(token: str) -> GoogleIdentity | None:
    """測試用的 Google 驗證：憑證寫成「google:<email>:<名稱>」就算通過。"""
    if token == "google-is-down":
        raise VerificationUnavailable()
    parts = token.split(":")
    if len(parts) != 3 or parts[0] != "google":
        return None
    return GoogleIdentity(email=parts[1], name=parts[2])


class SignedInClient(FlaskClient):
    """有登入憑證的話，每個請求都自動帶上它。"""

    session_token: str | None = None
    user_id: int | None = None

    def open(self, *args, **kwargs):
        if self.session_token:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Authorization", f"Bearer {self.session_token}")
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.exit("要先設定 TEST_DATABASE_URL（執行 tools/local_db.sh start 會印出來）。", returncode=2)
    if not conninfo_to_dict(url).get("dbname", "").endswith("_test"):
        pytest.exit("TEST_DATABASE_URL 的資料庫名稱要以 _test 結尾：測試會清空裡面所有資料。", returncode=2)
    return url


@pytest.fixture
def documents_dir(tmp_path):
    folder = tmp_path / "documents"
    folder.mkdir()
    return folder


@pytest.fixture
def config(database_url, documents_dir) -> Config:
    return Config(
        database_url=database_url,
        allowed_origins=(WEB_ORIGIN,),
        google_client_id="test-client-id",
        first_admin_email=ADMIN_EMAIL,
        documents_dir=documents_dir,
    )


@pytest.fixture
def app(config):
    # 每個測試都從全空的資料庫開始，順便確認「啟動時自動建表」真的建得起來。
    with psycopg.connect(config.database_url) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    app = create_app(config, verify_google_token=fake_google)
    app.test_client_class = SignedInClient
    return app


@pytest.fixture
def anonymous(app) -> SignedInClient:
    """沒有登入的用戶端。"""
    return app.test_client()


@pytest.fixture
def sign_in(app):
    """用某個 email 登入，回傳帶著憑證的用戶端。只是登入，還沒註冊。"""

    def _sign_in(email: str, name: str = "測試使用者") -> SignedInClient:
        client = app.test_client()
        response = client.post("/auth/login", json={"id_token": f"google:{email}:{name}"})
        assert response.status_code == 200, response.get_json()
        client.session_token = response.get_json()["session_token"]
        return client

    return _sign_in


@pytest.fixture
def client(sign_in) -> SignedInClient:
    """管理員（設定檔指定的第一位管理員，登入就是已審核）。"""
    admin = sign_in(ADMIN_EMAIL)
    admin.user_id = admin.get("/auth/status").get_json()["user"]["id"]
    return admin


@pytest.fixture
def add_member(sign_in, client):
    """註冊一位成員並由管理員審核通過，回傳他的用戶端。role 可以指定成版主或管理員。"""

    def _add(email: str, role: str = "member", name: str = "住戶") -> SignedInClient:
        member = sign_in(email, name)
        registered = member.post("/auth/register", json={"display_name": name, "unit_no": "A-1"})
        assert registered.status_code == 201, registered.get_json()
        member.user_id = registered.get_json()["user"]["id"]
        approved = client.put(
            f"/admin/users/{member.user_id}",
            json={
                "display_name": name,
                "unit_no": "A-1",
                "role": role,
                "status": "approved",
                "expected": {"display_name": name, "unit_no": "A-1", "role": "member", "status": "pending"},
            },
        )
        assert approved.status_code == 200, approved.get_json()
        return member

    return _add


def member_fields(database_url: str, user_id: int) -> dict:
    """資料庫裡這位成員現在的名稱、戶號、角色、狀態。"""
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT display_name, unit_no, role, status FROM users WHERE id = %s", (user_id,)
        ).fetchone()
    return dict(zip(("display_name", "unit_no", "role", "status"), row)) if row else {}


@pytest.fixture
def edit_member(config):
    """照後台畫面的做法改一位成員：先看他現在的樣子當作 expected，再送出要改的欄位。"""

    def _edit(actor: SignedInClient, user_id: int, **changes):
        current = member_fields(config.database_url, user_id)
        fields = {"display_name": "住戶", "unit_no": "A-1", "role": "member", "status": "approved"}
        payload = (current or fields) | {"expected": current or fields} | changes
        return actor.put(f"/admin/users/{user_id}", json=payload)

    return _edit
