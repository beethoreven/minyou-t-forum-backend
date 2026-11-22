"""社區公告的 API。"""

import pytest

from auth_utils import guard
from auth_utils.guard import WriteLimit

# 19 位數，但比 PostgreSQL bigint 的上限還大。
BEYOND_BIGINT = "9999999999999999999"


def create(client, title="停水通知", body="週五上午九點到十二點停水。"):
    return client.post("/announcements", json={"title": title, "body": body})


def test_create_then_read(client):
    response = create(client)
    assert response.status_code == 201
    created = response.get_json()
    assert created["title"] == "停水通知"
    assert created["body"] == "週五上午九點到十二點停水。"
    assert created["author_name"] == "測試使用者"
    assert created["created_at"].endswith("Z")
    assert created["updated_at"] == created["created_at"]

    fetched = client.get(f"/announcements/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json() == created


def test_list_shows_only_the_fields_the_list_needs(client):
    created = create(client).get_json()
    page = client.get("/announcements").get_json()
    assert page == {
        "items": [{"id": created["id"], "title": "停水通知", "created_at": created["created_at"]}],
        "has_more": False,
    }


def test_empty_list(client):
    assert client.get("/announcements").get_json() == {"items": [], "has_more": False}


def test_list_is_newest_first_and_paged(client):
    ids = [create(client, title=f"公告 {number}").get_json()["id"] for number in range(1, 26)]

    first = client.get("/announcements").get_json()
    assert [item["id"] for item in first["items"]] == ids[:4:-1]  # 最新的 20 筆，新的在前
    assert first["has_more"] is True

    last_id = first["items"][-1]["id"]
    second = client.get(f"/announcements?before={last_id}").get_json()
    assert [item["id"] for item in second["items"]] == ids[4::-1]  # 剩下的 5 筆
    assert second["has_more"] is False


def test_exactly_one_full_page_has_no_more(client):
    for number in range(20):
        create(client, title=f"公告 {number}")
    page = client.get("/announcements").get_json()
    assert len(page["items"]) == 20
    assert page["has_more"] is False


def test_trims_surrounding_whitespace(client):
    created = create(client, title="  停水通知  ", body="\n內文\n\n").get_json()
    assert created["title"] == "停水通知"
    assert created["body"] == "內文"


def test_keeps_line_breaks_inside_the_body(client):
    created = create(client, body="第一行\n\n第二行").get_json()
    assert created["body"] == "第一行\n\n第二行"


def test_stores_markup_as_plain_text(client):
    body = "<script>alert(1)</script> & <b>粗體</b>"
    created = create(client, body=body).get_json()
    assert created["body"] == body


def test_accepts_text_exactly_at_the_limit(client):
    assert create(client, title="標" * 100, body="字" * 10_000).status_code == 201


def test_an_emoji_counts_as_one_character(client):
    assert create(client, title="😀" * 100).status_code == 201
    assert create(client, title="😀" * 101).status_code == 400


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"body": "內文"}, "title_required"),
        ({"title": "", "body": "內文"}, "title_required"),
        ({"title": "  \n ", "body": "內文"}, "title_required"),
        ({"title": 123, "body": "內文"}, "title_required"),
        ({"title": "標" * 101, "body": "內文"}, "title_too_long"),
        ({"title": "標\x00題", "body": "內文"}, "title_invalid"),
        ({"title": "標題"}, "body_required"),
        ({"title": "標題", "body": "   "}, "body_required"),
        ({"title": "標題", "body": "字" * 10_001}, "body_too_long"),
    ],
)
def test_rejects_invalid_input(client, payload, code):
    response = client.post("/announcements", json=payload)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert client.get("/announcements").get_json()["items"] == []  # 什麼都沒有寫進去


def test_update(client):
    created = create(client).get_json()
    response = client.put(
        f"/announcements/{created['id']}",
        json={"title": "停水通知（改期）", "body": "改到週六。", "version": created["version"]},
    )
    assert response.status_code == 200
    updated = response.get_json()
    assert updated["title"] == "停水通知（改期）"
    assert updated["body"] == "改到週六。"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["created_at"]
    assert updated["version"] == created["version"] + 1
    assert client.get(f"/announcements/{created['id']}").get_json() == updated


def test_an_edit_based_on_an_old_version_is_refused(add_member, client):
    moderator = add_member("moderator@example.com", role="moderator")
    created = create(client).get_json()

    assert moderator.put(f"/announcements/{created['id']}", json={"title": "版主改的", "body": "內文", "version": 1}).status_code == 200
    late = client.put(f"/announcements/{created['id']}", json={"title": "管理員改的", "body": "內文", "version": 1})
    assert late.status_code == 409
    assert late.get_json()["code"] == "edit_conflict"
    assert client.get(f"/announcements/{created['id']}").get_json()["title"] == "版主改的"

    old_page = client.put(f"/announcements/{created['id']}", json={"title": "標題", "body": "內文"})
    assert old_page.status_code == 409


def test_moderators_have_an_announcement_limit_but_the_admin_does_not(add_member, client, monkeypatch):
    monkeypatch.setattr(guard, "ANNOUNCEMENT_LIMITS", (WriteLimit(600, 2, "10 分鐘"),))
    moderator = add_member("moderator@example.com", role="moderator")

    assert [create(moderator).status_code for _ in range(2)] == [201, 201]
    blocked = create(moderator)
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "write_limited"
    assert "10 分鐘" in blocked.get_json()["error"]
    assert [create(client).status_code for _ in range(3)] == [201] * 3  # 管理員不限
    assert len(client.get("/announcements").get_json()["items"]) == 5


def test_announcements_show_the_author_role(add_member, client):
    moderator = add_member("moderator@example.com", role="moderator", name="版主")
    created = create(moderator).get_json()
    assert (created["author_name"], created["author_role"]) == ("版主", "moderator")


def test_update_validates_input_and_keeps_the_old_content(client):
    created = create(client).get_json()
    response = client.put(f"/announcements/{created['id']}", json={"title": "", "body": "內文"})
    assert response.status_code == 400
    assert client.get(f"/announcements/{created['id']}").get_json() == created


def test_delete(client):
    created = create(client).get_json()
    response = client.delete(f"/announcements/{created['id']}")
    assert response.status_code == 204
    assert response.data == b""
    assert client.get(f"/announcements/{created['id']}").status_code == 404
    assert client.get("/announcements").get_json()["items"] == []


@pytest.mark.parametrize("method", ["get", "put", "delete"])
@pytest.mark.parametrize("missing_id", ["12345", BEYOND_BIGINT])
def test_missing_announcement_is_404(client, method, missing_id):
    payload = {"json": {"title": "標題", "body": "內文", "version": 1}} if method == "put" else {}
    response = getattr(client, method)(f"/announcements/{missing_id}", **payload)
    assert response.status_code == 404
    assert response.get_json()["code"] == "announcement_not_found"


@pytest.mark.parametrize("bad_id", ["0", "-1", "abc", "1.5", "007", "9" * 30, "9" * 5000])
def test_malformed_id_is_404(client, bad_id):
    response = client.get(f"/announcements/{bad_id}")
    assert response.status_code == 404
    assert response.get_json()["code"] == "not_found"


@pytest.mark.parametrize("before", ["", "abc", "0", "-1", "1.5", "９", "9" * 20, "9" * 5000])
def test_before_must_be_a_positive_integer(client, before):
    response = client.get("/announcements", query_string={"before": before})
    assert response.status_code == 400
    assert response.get_json()["code"] == "before_invalid"
