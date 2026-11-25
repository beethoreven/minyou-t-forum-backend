"""登入、註冊審核、角色權限、後台。"""

import threading
import time

import psycopg
import pytest

from conftest import ADMIN_EMAIL, member_fields
from db.schema import ensure_schema

PROFILE = {"display_name": "王小明", "unit_no": "A-5"}
ENTRY = {"title": "標題", "body": "內文"}


def code_of(response) -> str:
    return response.get_json()["code"]


def member_update(role="member", status="approved", **extra) -> dict:
    return {"display_name": "住戶", "unit_no": "A-1", "role": role, "status": status} | extra


def with_expected(config, user_id, update: dict) -> dict:
    """在要改的內容裡附上這位成員現在的樣子（後台畫面載入時看到的）。"""
    return update | {"expected": member_fields(config.database_url, user_id)}


# ── 登入 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/auth/status"),
        ("post", "/auth/register"),
        ("get", "/announcements"),
        ("post", "/announcements"),
        ("get", "/posts"),
        ("post", "/posts"),
        ("delete", "/posts/1"),
        ("post", "/posts/1/replies"),
        ("delete", "/replies/1"),
        ("get", "/documents"),
        ("get", "/documents/0123456789abcdef/download"),
        ("get", "/admin/users"),
        ("put", "/admin/users/1"),
        ("delete", "/admin/users/1/sessions"),
        ("get", "/admin/documents"),
    ],
)
def test_every_protected_route_needs_a_session(anonymous, method, path):
    for headers in ({}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic abc"}):
        response = getattr(anonymous, method)(path, headers=headers)
        assert response.status_code == 401, (path, headers)
        assert code_of(response) == "not_signed_in"


def test_login_rejects_a_token_google_did_not_issue(anonymous):
    response = anonymous.post("/auth/login", json={"id_token": "forged"})
    assert response.status_code == 401
    assert code_of(response) == "invalid_google_token"


def test_login_when_google_is_unreachable_is_503(anonymous):
    response = anonymous.post("/auth/login", json={"id_token": "google-is-down"})
    assert response.status_code == 503
    assert code_of(response) == "google_unavailable"


@pytest.mark.parametrize("payload", [{}, {"id_token": ""}, {"id_token": 123}])
def test_login_needs_an_id_token(anonymous, payload):
    response = anonymous.post("/auth/login", json=payload)
    assert response.status_code == 400
    assert code_of(response) == "id_token_required"


def test_first_admin_is_approved_without_review(client):
    status = client.get("/auth/status").get_json()
    assert status["email"] == ADMIN_EMAIL
    assert status["state"] == "approved"
    assert status["authorized"] is True
    assert status["user"]["role"] == "admin"
    assert status["user"]["display_name"] == "測試使用者"


def test_logout_ends_the_session(client):
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/status").status_code == 401
    assert client.post("/auth/logout").status_code == 204  # 已經失效的憑證再登出一次也算成功


def test_session_expires_after_being_idle(client, config):
    with psycopg.connect(config.database_url) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = now() - interval '89 days'")
    assert client.get("/auth/status").status_code == 200  # 還沒到 90 天，而且這次使用會重新計算

    with psycopg.connect(config.database_url) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = now() - interval '91 days'")
    assert client.get("/auth/status").status_code == 401


def test_database_never_stores_the_token_itself(client, config):
    with psycopg.connect(config.database_url) as conn:
        stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert stored != client.session_token
    assert len(stored) == 64


# ── 註冊與審核 ────────────────────────────────────────────


def test_register_then_wait_for_review(sign_in, client):
    resident = sign_in("resident@example.com", "王小明")

    status = resident.get("/auth/status").get_json()
    assert status["state"] == "unregistered"
    assert status["user"] is None
    assert code_of(resident.get("/posts")) == "unregistered"

    registered = resident.post("/auth/register", json=PROFILE)
    assert registered.status_code == 201
    account = registered.get_json()
    assert account["state"] == "pending"
    assert account["authorized"] is False
    assert account["user"]["role"] == "member"

    blocked = resident.get("/posts")
    assert blocked.status_code == 403
    assert code_of(blocked) == "pending"

    # 管理員在後台看到他排在最前面，審核通過
    listed = client.get("/admin/users").get_json()["items"]
    assert listed[0]["email"] == "resident@example.com"
    assert listed[0]["status"] == "pending"
    assert listed[0]["unit_no"] == "A-5"
    expected = {key: listed[0][key] for key in ("display_name", "unit_no", "role", "status")}
    approved = client.put(
        f"/admin/users/{listed[0]['id']}",
        json=PROFILE | {"role": "member", "status": "approved", "expected": expected},
    )
    assert approved.get_json()["changed"] is True

    assert resident.get("/posts").status_code == 200
    assert resident.get("/auth/status").get_json()["state"] == "approved"


def test_cannot_register_twice_or_choose_a_role(sign_in):
    resident = sign_in("resident@example.com")
    first = resident.post("/auth/register", json=PROFILE | {"role": "admin", "status": "approved"})
    assert first.get_json()["state"] == "pending"  # 多送的角色與狀態不會被採用
    assert first.get_json()["user"]["role"] == "member"

    second = resident.post("/auth/register", json=PROFILE)
    assert second.status_code == 409
    assert code_of(second) == "already_registered"


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"unit_no": "A-5"}, "display_name_required"),
        ({"display_name": "王" * 31, "unit_no": "A-5"}, "display_name_too_long"),
        ({"display_name": "王小明"}, "unit_no_required"),
    ],
)
def test_register_validates_input(sign_in, payload, code):
    resident = sign_in("resident@example.com")
    response = resident.post("/auth/register", json=payload)
    assert response.status_code == 400
    assert code_of(response) == code
    assert resident.get("/auth/status").get_json()["state"] == "unregistered"


def test_disabling_signs_out_every_device(add_member, sign_in, edit_member, client):
    resident = add_member("resident@example.com")
    phone = sign_in("resident@example.com")  # 同一個人的另一台裝置
    assert resident.get("/posts").status_code == 200

    assert edit_member(client, resident.user_id, status="disabled").status_code == 200

    # 兩台裝置的憑證都作廢了
    for device in (resident, phone):
        assert device.get("/posts").status_code == 401
        assert device.get("/auth/status").status_code == 401

    # 重新用 Google 登入看得到自己被停用，也不能重新註冊復活
    again = sign_in("resident@example.com")
    assert again.get("/auth/status").get_json()["state"] == "disabled"
    assert code_of(again.get("/posts")) == "disabled"
    assert code_of(again.post("/auth/register", json=PROFILE)) == "already_registered"

    # 恢復之後，停用前的舊憑證也不會跟著復活：要重新登入
    assert edit_member(client, resident.user_id, status="approved").status_code == 200
    assert resident.get("/posts").status_code == 401
    assert again.get("/posts").status_code == 200  # 停用期間重新登入的那一張還能用


# ── 角色權限 ──────────────────────────────────────────────


def test_member_can_read_and_post_but_not_announce(add_member, client):
    resident = add_member("resident@example.com")
    announcement = client.post("/announcements", json=ENTRY).get_json()

    assert resident.get("/announcements").status_code == 200
    assert resident.get(f"/announcements/{announcement['id']}").status_code == 200
    assert resident.get("/documents").status_code == 200
    assert resident.post("/posts", json=ENTRY).status_code == 201

    for response in (
        resident.post("/announcements", json=ENTRY),
        resident.put(f"/announcements/{announcement['id']}", json=ENTRY),
        resident.delete(f"/announcements/{announcement['id']}"),
        resident.get("/admin/users"),
        resident.get("/admin/documents"),
        resident.put(f"/admin/users/{resident.user_id}", json=member_update(role="admin")),
        resident.delete(f"/admin/users/{resident.user_id}/sessions"),
    ):
        assert response.status_code == 403
        assert code_of(response) == "forbidden"
    assert client.get(f"/announcements/{announcement['id']}").get_json() == announcement


def test_only_the_author_or_staff_can_change_a_post(add_member, client):
    author = add_member("author@example.com", name="作者")
    other = add_member("other@example.com", name="別人")
    moderator = add_member("moderator@example.com", role="moderator", name="版主")
    post = author.post("/posts", json=ENTRY).get_json()
    reply = author.post(f"/posts/{post['id']}/replies", json={"body": "回覆"}).get_json()
    assert post["author_id"] == author.user_id
    assert reply["author_id"] == author.user_id

    for response in (
        other.put(f"/posts/{post['id']}", json=ENTRY),
        other.delete(f"/posts/{post['id']}"),
        other.delete(f"/replies/{reply['id']}"),
    ):
        assert response.status_code == 403
        assert code_of(response) == "forbidden"

    edited = author.put(f"/posts/{post['id']}", json={"title": "作者改的", "body": "內文", "version": post["version"]})
    assert edited.status_code == 200
    edit_again = {"title": "版主改的", "body": "內文", "version": edited.get_json()["version"]}
    assert moderator.put(f"/posts/{post['id']}", json=edit_again).status_code == 200
    assert moderator.delete(f"/replies/{reply['id']}").status_code == 204
    assert client.delete(f"/posts/{post['id']}").status_code == 204


def test_moderator_can_announce_and_review_but_only_manage_members(add_member, sign_in, edit_member, client):
    moderator = add_member("moderator@example.com", role="moderator")
    other_moderator = add_member("other-moderator@example.com", role="moderator")
    assert moderator.post("/announcements", json=ENTRY).status_code == 201

    applicant = sign_in("applicant@example.com")
    applicant_id = applicant.post("/auth/register", json=PROFILE).get_json()["user"]["id"]
    assert edit_member(moderator, applicant_id, status="approved").status_code == 200
    assert applicant.get("/posts").status_code == 200
    assert edit_member(moderator, applicant_id, display_name="改過的名字").status_code == 200

    # 角色只有管理員能改：不能把人升成版主或管理員
    for role in ("moderator", "admin"):
        promote = edit_member(moderator, applicant_id, role=role)
        assert promote.status_code == 403
        assert code_of(promote) == "cannot_change_role"

    # 管理員與其他版主的帳號都不能動，也不能讓他們登出所有裝置
    for target, code in ((client, "cannot_change_admin"), (other_moderator, "cannot_change_moderator")):
        for response in (
            edit_member(moderator, target.user_id, display_name="改名"),
            edit_member(moderator, target.user_id, status="disabled"),
            moderator.delete(f"/admin/users/{target.user_id}/sessions"),
        ):
            assert response.status_code == 403
            assert code_of(response) == code
    assert edit_member(moderator, other_moderator.user_id, role="member").status_code == 403
    assert client.get("/auth/status").status_code == 200
    assert other_moderator.get("/auth/status").status_code == 200

    # 版主還是可以停用一般使用者
    assert edit_member(moderator, applicant_id, status="disabled").status_code == 200

    # 管理員可以
    assert edit_member(client, other_moderator.user_id, role="member").status_code == 200
    assert edit_member(client, applicant_id, status="approved", role="admin").status_code == 200


def test_nobody_can_change_their_own_role_or_status(add_member, edit_member, client):
    moderator = add_member("moderator@example.com", role="moderator")
    for who, changes in (
        (client, {"role": "member"}),
        (client, {"status": "disabled"}),
        (moderator, {"role": "member"}),
    ):
        response = edit_member(who, who.user_id, **changes)
        assert response.status_code == 403
        assert code_of(response) == "cannot_change_self"

    renamed = edit_member(client, client.user_id, display_name="新名字")
    assert renamed.status_code == 200  # 改自己的名稱可以
    assert renamed.get_json()["item"]["display_name"] == "新名字"


def test_admin_update_rules(add_member, edit_member, client):
    resident = add_member("resident@example.com")

    unchanged = edit_member(client, resident.user_id)
    assert unchanged.status_code == 200
    assert unchanged.get_json()["changed"] is False

    for changes, code in (
        ({"status": "pending"}, "status_invalid"),  # 不能改回審核中
        ({"role": "moderator", "status": "disabled"}, "role_requires_approved"),
        ({"role": "owner"}, "role_invalid"),
        ({"status": "banned"}, "status_invalid"),
        ({"display_name": ""}, "display_name_required"),
        ({"expected": {"display_name": 1}}, "expected_invalid"),
    ):
        response = edit_member(client, resident.user_id, **changes)
        assert response.status_code == 400, changes
        assert code_of(response) == code

    missing = edit_member(client, 99999)
    assert missing.status_code == 404
    assert code_of(missing) == "user_not_found"


def test_a_stale_admin_page_cannot_undo_a_change(add_member, edit_member, config, client):
    """畫面是別人改之前載入的：送出時帶的 expected 對不上，不能用舊的值蓋回去。"""
    resident = add_member("resident@example.com")
    moderator = add_member("moderator@example.com", role="moderator")
    stale = member_fields(config.database_url, resident.user_id)  # 版主的畫面在這時候載入

    assert edit_member(client, resident.user_id, status="disabled").status_code == 200  # 管理員停用他

    # 版主用沒重新整理的畫面，只改了名字就按儲存
    response = moderator.put(
        f"/admin/users/{resident.user_id}",
        json=stale | {"display_name": "改個名字", "expected": stale},
    )
    assert response.status_code == 409
    assert code_of(response) == "member_changed"
    assert member_fields(config.database_url, resident.user_id)["status"] == "disabled"  # 停用還在

    # 沒帶 expected 的是舊版的畫面，一樣請他重新整理
    old_page = moderator.put(f"/admin/users/{resident.user_id}", json=stale)
    assert old_page.status_code == 409
    assert code_of(old_page) == "member_changed"


def test_checking_and_writing_a_member_happen_on_a_locked_row(add_member, config, client):
    """版主儲存的同時，管理員剛好把同一個人升成管理員：版主不能把他改回去。

    模擬的方法：另外開一條連線鎖住那一筆、把他改成管理員，先不送出；版主的請求這時候要等。
    放開之後，版主的請求看到的是改完的樣子，所以會被擋下來，而不是用舊的檢查結果寫進去。
    """
    resident = add_member("resident@example.com")
    moderator = add_member("moderator@example.com", role="moderator")
    stale = member_fields(config.database_url, resident.user_id)
    results = []

    with psycopg.connect(config.database_url) as conn:
        conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (resident.user_id,))
        conn.execute("UPDATE users SET role = 'admin' WHERE id = %s", (resident.user_id,))
        worker = threading.Thread(
            target=lambda: results.append(
                moderator.put(
                    f"/admin/users/{resident.user_id}",
                    json=stale | {"display_name": "版主改的", "expected": stale},
                )
            )
        )
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive()  # 版主的請求在等那把鎖
    worker.join(timeout=10)

    assert results[0].status_code in (403, 409)
    assert member_fields(config.database_url, resident.user_id)["role"] == "admin"


def test_staff_can_sign_a_member_out_everywhere(add_member, sign_in, client):
    resident = add_member("resident@example.com")
    phone = sign_in("resident@example.com")

    response = client.delete(f"/admin/users/{resident.user_id}/sessions")
    assert response.status_code == 200
    assert response.get_json() == {"revoked": 2}
    assert resident.get("/posts").status_code == 401
    assert phone.get("/posts").status_code == 401

    again = sign_in("resident@example.com")  # 帳號沒有被停用，重新登入就好
    assert again.get("/posts").status_code == 200

    missing = client.delete("/admin/users/99999/sessions")
    assert missing.status_code == 404


def test_first_admin_is_not_restored_once_another_admin_exists(add_member, sign_in, edit_member, client):
    second_admin = add_member("second@example.com", role="admin")
    assert edit_member(second_admin, client.user_id, role="member").status_code == 200
    assert client.get("/admin/users").status_code == 403

    again = sign_in(ADMIN_EMAIL)  # 重新登入也不會自己變回管理員
    assert again.get("/admin/users").status_code == 403
    assert again.get("/auth/status").get_json()["user"]["role"] == "member"

    assert edit_member(second_admin, client.user_id, status="disabled").status_code == 200
    assert sign_in(ADMIN_EMAIL).get("/auth/status").get_json()["state"] == "disabled"  # 停用也鎖得住


def test_first_admin_is_created_only_when_there_is_no_admin(sign_in, config):
    resident = sign_in("resident@example.com")
    resident.post("/auth/register", json=PROFILE)
    with psycopg.connect(config.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM users WHERE role = 'admin'").fetchone()[0] == 0

    admin = sign_in(ADMIN_EMAIL)  # 還沒有任何管理員：設定檔指定的這位直接是管理員
    assert admin.get("/auth/status").get_json()["user"]["role"] == "admin"


# ── 登入的次數 ────────────────────────────────────────────


def test_login_attempts_are_limited_per_source(app, anonymous):
    app.extensions["login_attempts"].limit = 3
    from_a = {"X-Forwarded-For": "203.0.113.7"}
    assert [anonymous.post("/auth/login", json={"id_token": "forged"}, headers=from_a).status_code for _ in range(3)] == [401] * 3
    blocked = anonymous.post("/auth/login", json={"id_token": "google:a@example.com:A"}, headers=from_a)
    assert blocked.status_code == 429  # 連真的憑證也先擋下
    assert code_of(blocked) == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1

    # 只認代理接在最後面的那一個位址：前面亂填的換不到新的額度
    spoofed = {"X-Forwarded-For": "198.51.100.1, 203.0.113.7"}
    assert anonymous.post("/auth/login", json={"id_token": "forged"}, headers=spoofed).status_code == 429
    other_source = {"X-Forwarded-For": "203.0.113.8"}
    assert anonymous.post("/auth/login", json={"id_token": "forged"}, headers=other_source).status_code == 401


def test_logins_are_limited_per_google_account(app, anonymous):
    app.extensions["logins_per_account"].limit = 2
    login = lambda email: anonymous.post("/auth/login", json={"id_token": f"google:{email}:名字"})
    assert [login("a@example.com").status_code for _ in range(2)] == [200, 200]
    assert login("a@example.com").status_code == 429
    assert login("b@example.com").status_code == 200  # 別的帳號不受影響


# ── 資料庫裡的限制 ────────────────────────────────────────


def test_database_rejects_staff_that_is_not_approved(add_member, config):
    moderator = add_member("moderator@example.com", role="moderator")
    with psycopg.connect(config.database_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (moderator.user_id,))
    with psycopg.connect(config.database_url) as conn:
        validated = conn.execute(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'users_staff_must_be_approved'"
        ).fetchone()[0]
    assert validated is True


def test_existing_bad_rows_do_not_stop_the_service_from_starting(add_member, config, caplog):
    """正式資料庫裡如果已經有不符合的資料，啟動不能失敗；之後的寫入照樣會被擋，而且 log 裡看得到。"""
    moderator = add_member("moderator@example.com", role="moderator")
    with psycopg.connect(config.database_url) as conn:
        conn.execute("ALTER TABLE users DROP CONSTRAINT users_staff_must_be_approved")
        conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (moderator.user_id,))

    with psycopg.connect(config.database_url) as conn:
        ensure_schema(conn)  # 不會丟例外
    assert any("版主或管理員不是已審核" in record.getMessage() for record in caplog.records)
    with psycopg.connect(config.database_url) as conn:
        validated = conn.execute(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'users_staff_must_be_approved'"
        ).fetchone()[0]
        assert validated is False  # 限制加上了，只是既有的資料還沒檢查通過
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO users (email, display_name, role, status) VALUES ('x@example.com', 'x', 'admin', 'pending')"
            )
