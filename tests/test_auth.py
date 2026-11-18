"""登入、註冊審核、角色權限、後台。"""

import psycopg
import pytest

from conftest import ADMIN_EMAIL

PROFILE = {"display_name": "王小明", "unit_no": "A-5"}
ENTRY = {"title": "標題", "body": "內文"}


def code_of(response) -> str:
    return response.get_json()["code"]


def member_update(role="member", status="approved", **extra) -> dict:
    return {"display_name": "住戶", "unit_no": "A-1", "role": role, "status": status} | extra


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
    approved = client.put(f"/admin/users/{listed[0]['id']}", json=PROFILE | {"role": "member", "status": "approved"})
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


def test_disabling_takes_effect_on_the_next_request(add_member, client):
    resident = add_member("resident@example.com")
    assert resident.get("/posts").status_code == 200

    client.put(f"/admin/users/{resident.user_id}", json=member_update(status="disabled"))

    blocked = resident.get("/posts")  # 同一張憑證，下一個請求就被擋
    assert blocked.status_code == 403
    assert code_of(blocked) == "disabled"
    assert resident.get("/auth/status").get_json()["state"] == "disabled"
    assert code_of(resident.post("/auth/register", json=PROFILE)) == "already_registered"  # 不能重新註冊復活


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
        resident.put(f"/admin/users/{resident.user_id}", json=member_update(role="admin")),
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

    assert author.put(f"/posts/{post['id']}", json={"title": "作者改的", "body": "內文"}).status_code == 200
    assert moderator.put(f"/posts/{post['id']}", json={"title": "版主改的", "body": "內文"}).status_code == 200
    assert moderator.delete(f"/replies/{reply['id']}").status_code == 204
    assert client.delete(f"/posts/{post['id']}").status_code == 204


def test_moderator_can_announce_and_review_but_not_touch_admins(add_member, sign_in, client):
    moderator = add_member("moderator@example.com", role="moderator")
    assert moderator.post("/announcements", json=ENTRY).status_code == 201

    applicant = sign_in("applicant@example.com")
    applicant_id = applicant.post("/auth/register", json=PROFILE).get_json()["user"]["id"]
    assert moderator.put(f"/admin/users/{applicant_id}", json=member_update()).status_code == 200
    assert applicant.get("/posts").status_code == 200
    assert moderator.put(f"/admin/users/{applicant_id}", json=member_update(role="moderator")).status_code == 200

    # 不能改管理員的帳號，也不能把別人變成管理員
    touch_admin = moderator.put(f"/admin/users/{client.user_id}", json=member_update(role="admin", display_name="改名"))
    assert touch_admin.status_code == 403
    assert code_of(touch_admin) == "cannot_change_admin"
    grant_admin = moderator.put(f"/admin/users/{applicant_id}", json=member_update(role="admin"))
    assert grant_admin.status_code == 403
    assert code_of(grant_admin) == "cannot_grant_admin"

    # 管理員可以
    assert client.put(f"/admin/users/{applicant_id}", json=member_update(role="admin")).status_code == 200


def test_nobody_can_change_their_own_role_or_status(add_member, client):
    moderator = add_member("moderator@example.com", role="moderator")
    for who, update in (
        (client, member_update(role="member")),
        (client, member_update(role="admin", status="disabled")),
        (moderator, member_update(role="member")),
    ):
        response = who.put(f"/admin/users/{who.user_id}", json=update)
        assert response.status_code == 403
        assert code_of(response) == "cannot_change_self"

    renamed = client.put(f"/admin/users/{client.user_id}", json=member_update(role="admin", display_name="新名字"))
    assert renamed.status_code == 200  # 改自己的名稱可以
    assert renamed.get_json()["item"]["display_name"] == "新名字"


def test_admin_update_rules(add_member, client):
    resident = add_member("resident@example.com")
    path = f"/admin/users/{resident.user_id}"

    unchanged = client.put(path, json=member_update())
    assert unchanged.status_code == 200
    assert unchanged.get_json()["changed"] is False

    for update, code in (
        (member_update(status="pending"), "status_invalid"),  # 不能改回審核中
        (member_update(role="moderator", status="disabled"), "role_requires_approved"),
        (member_update(role="owner"), "role_invalid"),
        (member_update(status="banned"), "status_invalid"),
        (member_update(display_name=""), "display_name_required"),
    ):
        response = client.put(path, json=update)
        assert response.status_code == 400, update
        assert code_of(response) == code

    missing = client.put("/admin/users/99999", json=member_update())
    assert missing.status_code == 404
    assert code_of(missing) == "user_not_found"


def test_first_admin_is_restored_at_login(add_member, sign_in, client):
    second_admin = add_member("second@example.com", role="admin")
    second_admin.put(f"/admin/users/{client.user_id}", json=member_update(role="member"))
    assert client.get("/admin/users").status_code == 403

    again = sign_in(ADMIN_EMAIL)  # 重新登入就恢復
    assert again.get("/admin/users").status_code == 200
