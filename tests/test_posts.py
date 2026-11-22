"""社區論壇的 API：文章與回覆。"""

import pytest

# 19 位數，但比 PostgreSQL bigint 的上限還大。
BEYOND_BIGINT = "9999999999999999999"


def create_post(client, title="頂樓可以曬棉被嗎", body="想請問大家的看法。"):
    return client.post("/posts", json={"title": title, "body": body})


def create_reply(client, post_id, body="可以，但要記得收。"):
    return client.post(f"/posts/{post_id}/replies", json={"body": body})


def test_create_then_read(client):
    response = create_post(client)
    assert response.status_code == 201
    created = response.get_json()
    assert created["title"] == "頂樓可以曬棉被嗎"
    assert created["body"] == "想請問大家的看法。"
    assert created["author_name"] == "測試使用者"
    assert created["author_role"] == "admin"
    assert created["version"] == 1
    assert created["replies"] == []

    fetched = client.get(f"/posts/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json() == created


def test_list_shows_author_and_reply_count(client):
    first = create_post(client, title="第一篇").get_json()
    second = create_post(client, title="第二篇").get_json()
    create_reply(client, first["id"])
    create_reply(client, first["id"])

    page = client.get("/posts").get_json()
    assert page["has_more"] is False
    assert page["items"] == [
        {
            "id": second["id"],
            "title": "第二篇",
            "author_name": "測試使用者",
            "author_role": "admin",
            "created_at": second["created_at"],
            "reply_count": 0,
        },
        {
            "id": first["id"],
            "title": "第一篇",
            "author_name": "測試使用者",
            "author_role": "admin",
            "created_at": first["created_at"],
            "reply_count": 2,
        },
    ]


def test_list_is_newest_first_and_paged(client):
    ids = [create_post(client, title=f"文章 {number}").get_json()["id"] for number in range(1, 26)]

    first = client.get("/posts").get_json()
    assert [item["id"] for item in first["items"]] == ids[:4:-1]
    assert first["has_more"] is True

    second = client.get(f"/posts?before={first['items'][-1]['id']}").get_json()
    assert [item["id"] for item in second["items"]] == ids[4::-1]
    assert second["has_more"] is False


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"body": "內文"}, "title_required"),
        ({"title": "標" * 101, "body": "內文"}, "title_too_long"),
        ({"title": "標題"}, "body_required"),
        ({"title": "標題", "body": "字" * 10_001}, "body_too_long"),
    ],
)
def test_rejects_invalid_post(client, payload, code):
    response = client.post("/posts", json=payload)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert client.get("/posts").get_json()["items"] == []


def test_update_keeps_the_replies(client):
    created = create_post(client).get_json()
    reply = create_reply(client, created["id"]).get_json()

    response = client.put(f"/posts/{created['id']}", json={"title": "新標題", "body": "新內文", "version": 1})
    assert response.status_code == 200
    updated = response.get_json()
    assert updated["title"] == "新標題"
    assert updated["body"] == "新內文"
    assert updated["version"] == 2
    assert updated["replies"] == [reply]
    assert client.get(f"/posts/{created['id']}").get_json() == updated


def test_an_edit_based_on_an_old_version_is_refused(add_member, client):
    """作者與版主同時打開編輯頁：後送出的那一個不能蓋掉先送出的。"""
    author = add_member("author@example.com", name="作者")
    post = create_post(author).get_json()

    assert author.put(f"/posts/{post['id']}", json={"title": "作者改的", "body": "內文", "version": 1}).status_code == 200
    late = client.put(f"/posts/{post['id']}", json={"title": "版主改的", "body": "內文", "version": 1})
    assert late.status_code == 409
    assert late.get_json()["code"] == "edit_conflict"
    assert client.get(f"/posts/{post['id']}").get_json()["title"] == "作者改的"

    # 沒帶版本號的是改版之前的舊畫面，也請他重新整理
    old_page = author.put(f"/posts/{post['id']}", json={"title": "標題", "body": "內文"})
    assert old_page.status_code == 409
    assert old_page.get_json()["code"] == "edit_conflict"

    for bad in ("1", 0, -1, True, 1.5):
        response = author.put(f"/posts/{post['id']}", json={"title": "標題", "body": "內文", "version": bad})
        assert response.status_code == 400, bad
        assert response.get_json()["code"] == "version_invalid"


def test_staff_names_carry_their_role(add_member, client):
    resident = add_member("resident@example.com", name="管理員")  # 一般住戶取了「管理員」這個名字
    moderator = add_member("moderator@example.com", role="moderator", name="版主")
    post = create_post(resident).get_json()
    reply = moderator.post(f"/posts/{post['id']}/replies", json={"body": "收到"}).get_json()

    assert post["author_role"] == "member"
    assert reply["author_role"] == "moderator"
    listed = client.get("/posts").get_json()["items"][0]
    assert (listed["author_name"], listed["author_role"]) == ("管理員", "member")


def test_delete_post_also_deletes_its_replies(client):
    kept = create_post(client, title="留下來的").get_json()
    kept_reply = create_reply(client, kept["id"]).get_json()
    doomed = create_post(client, title="要刪掉的").get_json()
    doomed_reply = create_reply(client, doomed["id"]).get_json()

    assert client.delete(f"/posts/{doomed['id']}").status_code == 204

    assert client.get(f"/posts/{doomed['id']}").status_code == 404
    assert client.delete(f"/replies/{doomed_reply['id']}").status_code == 404  # 回覆也不在了
    assert client.get(f"/posts/{kept['id']}").get_json()["replies"] == [kept_reply]


@pytest.mark.parametrize("method", ["get", "put", "delete"])
@pytest.mark.parametrize("missing_id", ["12345", BEYOND_BIGINT])
def test_missing_post_is_404(client, method, missing_id):
    payload = {"json": {"title": "標題", "body": "內文", "version": 1}} if method == "put" else {}
    response = getattr(client, method)(f"/posts/{missing_id}", **payload)
    assert response.status_code == 404
    assert response.get_json()["code"] == "post_not_found"


def test_replies_are_oldest_first(client):
    post = create_post(client).get_json()
    first = create_reply(client, post["id"], body="第一則").get_json()
    second = create_reply(client, post["id"], body="第二則").get_json()

    assert first["body"] == "第一則"
    assert first["author_name"] == "測試使用者"
    assert first["created_at"].endswith("Z")
    assert client.get(f"/posts/{post['id']}").get_json()["replies"] == [first, second]


def test_reply_is_trimmed_and_keeps_line_breaks(client):
    post = create_post(client).get_json()
    reply = create_reply(client, post["id"], body="  第一行\n第二行\n ").get_json()
    assert reply["body"] == "第一行\n第二行"


@pytest.mark.parametrize(
    "payload, code",
    [
        ({}, "body_required"),
        ({"body": "  "}, "body_required"),
        ({"body": ["不是文字"]}, "body_required"),
        ({"body": "字" * 2_001}, "body_too_long"),
    ],
)
def test_rejects_invalid_reply(client, payload, code):
    post = create_post(client).get_json()
    response = client.post(f"/posts/{post['id']}/replies", json=payload)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert client.get(f"/posts/{post['id']}").get_json()["replies"] == []


def test_accepts_reply_exactly_at_the_limit(client):
    post = create_post(client).get_json()
    assert create_reply(client, post["id"], body="字" * 2_000).status_code == 201


@pytest.mark.parametrize("missing_id", ["12345", BEYOND_BIGINT])
def test_reply_to_missing_post_is_404(client, missing_id):
    response = create_reply(client, missing_id)
    assert response.status_code == 404
    assert response.get_json()["code"] == "post_not_found"


def test_delete_reply(client):
    post = create_post(client).get_json()
    first = create_reply(client, post["id"], body="第一則").get_json()
    second = create_reply(client, post["id"], body="第二則").get_json()

    response = client.delete(f"/replies/{first['id']}")
    assert response.status_code == 204
    assert client.get(f"/posts/{post['id']}").get_json()["replies"] == [second]
    assert client.get("/posts").get_json()["items"][0]["reply_count"] == 1


@pytest.mark.parametrize("missing_id", ["12345", BEYOND_BIGINT])
def test_delete_missing_reply_is_404(client, missing_id):
    response = client.delete(f"/replies/{missing_id}")
    assert response.status_code == 404
    assert response.get_json()["code"] == "reply_not_found"
