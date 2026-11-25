"""管理操作紀錄：版主與管理員做的事、被拒絕的特權操作、只有管理員看得到、180 天後清掉。"""

import psycopg
import pytest
from test_documents import listed, upload

from conftest import ADMIN_EMAIL
from db import admin_actions
from documents.base import DocumentsUnavailable
from documents.local_folder import LocalFolder

ENTRY = {"title": "標題", "body": "內文"}


def logged(client) -> list[tuple]:
    """管理員在後台看到的紀錄，舊的在前面：（誰的 email, 動作, 對象, 結果）。"""
    items = client.get("/admin/actions").get_json()["items"]
    return [(item["actor_email"], item["action"], item["target_id"], item["outcome"]) for item in reversed(items)]


def stored_rows(database_url) -> list[dict]:
    with psycopg.connect(database_url) as conn:
        cursor = conn.execute("SELECT * FROM admin_actions ORDER BY id")
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def test_staff_actions_are_logged(add_member, edit_member, client, documents_dir):
    moderator = add_member("moderator@example.com", role="moderator")
    author = add_member("author@example.com")
    resident = add_member("resident@example.com")

    announcement = moderator.post("/announcements", json=ENTRY).get_json()
    moderator.put(f"/announcements/{announcement['id']}", json=ENTRY | {"version": announcement["version"]})
    post = author.post("/posts", json=ENTRY).get_json()
    reply = author.post(f"/posts/{post['id']}/replies", json={"body": "回覆"}).get_json()
    moderator.put(f"/posts/{post['id']}", json=ENTRY | {"version": post["version"]})
    moderator.delete(f"/replies/{reply['id']}")
    client.delete(f"/posts/{post['id']}")
    client.delete(f"/announcements/{announcement['id']}")
    assert upload(moderator, "社區公約.pdf").status_code == 201
    file_id = listed(client)["社區公約.pdf"]
    assert client.delete(f"/admin/documents/{file_id}").status_code == 204
    edit_member(moderator, resident.user_id, status="disabled")
    edit_member(client, resident.user_id, status="approved", display_name="新名字")
    edit_member(client, author.user_id, role="moderator")
    client.delete(f"/admin/users/{author.user_id}/sessions")

    mod, admin = "moderator@example.com", ADMIN_EMAIL
    ids = (moderator.user_id, author.user_id, resident.user_id)
    approvals = [(admin, "user.approve", str(user_id), "ok") for user_id in ids]
    approvals.insert(1, (admin, "user.role", str(moderator.user_id), "ok"))  # 加的時候就指定成版主
    assert logged(client) == approvals + [
        (mod, "announcement.create", str(announcement["id"]), "ok"),
        (mod, "announcement.update", str(announcement["id"]), "ok"),
        (mod, "post.update", str(post["id"]), "ok"),
        (mod, "reply.delete", str(reply["id"]), "ok"),
        (admin, "post.delete", str(post["id"]), "ok"),
        (admin, "announcement.delete", str(announcement["id"]), "ok"),
        (mod, "document.upload", "社區公約.pdf", "ok"),
        (admin, "document.delete", file_id, "ok"),
        (mod, "user.disable", str(resident.user_id), "ok"),
        (admin, "user.enable", str(resident.user_id), "ok"),
        (admin, "user.profile", str(resident.user_id), "ok"),
        (admin, "user.role", str(author.user_id), "ok"),
        (admin, "user.sign_out", str(author.user_id), "ok"),
    ]


def test_members_own_content_and_unchanged_saves_are_not_logged(add_member, edit_member, client):
    author = add_member("author@example.com")
    before = len(logged(client))

    post = author.post("/posts", json=ENTRY).get_json()
    reply = author.post(f"/posts/{post['id']}/replies", json={"body": "回覆"}).get_json()
    author.put(f"/posts/{post['id']}", json=ENTRY | {"version": post["version"]})
    author.delete(f"/replies/{reply['id']}")
    author.delete(f"/posts/{post['id']}")
    own = client.post("/posts", json=ENTRY).get_json()
    client.delete(f"/posts/{own['id']}")  # 管理員刪自己的文章也不是特權操作
    assert edit_member(client, author.user_id).get_json()["changed"] is False

    assert len(logged(client)) == before


def test_denied_privileged_actions_are_logged(add_member, edit_member, sign_in, client):
    moderator = add_member("moderator@example.com", role="moderator")
    resident = add_member("resident@example.com")
    post = client.post("/posts", json=ENTRY).get_json()
    stranger = sign_in("stranger@example.com")  # 登入了、還沒註冊
    before = len(logged(client))

    assert resident.post("/announcements", json=ENTRY).status_code == 403
    assert resident.delete(f"/posts/{post['id']}").status_code == 403
    assert edit_member(moderator, client.user_id, status="disabled").status_code == 403
    assert moderator.delete(f"/admin/users/{client.user_id}/sessions").status_code == 403
    assert stranger.post("/announcements", json=ENTRY).status_code == 403

    assert logged(client)[before:] == [
        ("resident@example.com", "announcement.create", None, "denied"),
        ("resident@example.com", "post.delete", str(post["id"]), "denied"),
        ("moderator@example.com", "user.update", str(client.user_id), "denied"),
        ("moderator@example.com", "user.sign_out", str(client.user_id), "denied"),
        ("stranger@example.com", "announcement.create", None, "denied"),
    ]
    # 被拒絕的操作沒有留下任何東西
    assert client.get("/auth/status").get_json()["state"] == "approved"
    assert client.get("/announcements").get_json()["items"] == []


def test_repeated_denials_are_logged_once_a_minute(add_member, client):
    resident = add_member("resident@example.com")
    before = len(logged(client))
    for _ in range(5):
        assert resident.post("/announcements", json=ENTRY).status_code == 403
    assert len(logged(client)) == before + 1


def test_a_failed_action_leaves_no_record(add_member, client, documents_dir, monkeypatch):
    moderator = add_member("moderator@example.com", role="moderator")
    before = len(logged(client))

    # 編輯衝突（409）：公告沒改到，也沒有紀錄
    announcement = client.post("/announcements", json=ENTRY).get_json()
    stale = moderator.put(f"/announcements/{announcement['id']}", json=ENTRY | {"version": 99})
    assert stale.status_code == 409

    # 存檔失敗：沒有上傳成功，紀錄也拿掉
    def broken(self, name, content):
        raise DocumentsUnavailable("存不進去")

    monkeypatch.setattr(LocalFolder, "save", broken)
    assert upload(moderator, "社區公約.pdf").status_code == 503
    # 刪除不存在的檔案：沒有紀錄
    assert client.delete("/admin/documents/0123456789abcdef").status_code == 404

    assert [row[1] for row in logged(client)[before:]] == ["announcement.create"]


def test_only_admins_can_read_the_log(add_member, client):
    moderator = add_member("moderator@example.com", role="moderator")
    resident = add_member("resident@example.com")
    for who in (moderator, resident):
        response = who.get("/admin/actions")
        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"
    # 想看紀錄被拒絕，本身也記下來
    assert ("moderator@example.com", "action_log.view", None, "denied") in logged(client)


def test_log_pages_newest_first(client, config, monkeypatch):
    monkeypatch.setattr(admin_actions, "PAGE_SIZE", 2)
    for _ in range(3):
        client.post("/announcements", json=ENTRY)
    first = client.get("/admin/actions").get_json()
    assert first["has_more"] is True
    assert [item["action"] for item in first["items"]] == ["announcement.create"] * 2
    assert first["items"][0]["id"] > first["items"][1]["id"]
    assert first["items"][0]["actor_name"]
    second = client.get(f"/admin/actions?before={first['items'][-1]['id']}").get_json()
    assert second["has_more"] is False
    assert len(second["items"]) == 1


def test_log_keeps_no_content_or_secrets(client, config):
    client.post("/announcements", json={"title": "秘密標題", "body": "秘密內文"})
    rows = stored_rows(config.database_url)
    assert set(rows[0]) == {"id", "actor_id", "actor_email", "action", "target_id", "outcome", "created_at"}
    assert "秘密" not in repr(rows)
    assert client.session_token not in repr(rows)


def test_records_older_than_180_days_are_removed(add_member, client, config):
    with psycopg.connect(config.database_url) as conn:
        conn.execute(
            """
            INSERT INTO admin_actions (actor_email, action, outcome, created_at)
            VALUES ('old@example.com', 'user.approve', 'ok', now() - interval '181 days'),
                   ('recent@example.com', 'user.approve', 'ok', now() - interval '179 days')
            """
        )
    client.post("/announcements", json=ENTRY)  # 寫入時順便清掉

    emails = [row["actor_email"] for row in stored_rows(config.database_url)]
    assert "old@example.com" not in emails
    assert "recent@example.com" in emails


@pytest.mark.parametrize("before", ["0", "abc", "-1"])
def test_log_rejects_a_bad_page_cursor(client, before):
    assert client.get(f"/admin/actions?before={before}").status_code == 400
