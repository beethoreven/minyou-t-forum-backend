"""社區公告的 API。"""

import pytest

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
        json={"title": "停水通知（改期）", "body": "改到週六。"},
    )
    assert response.status_code == 200
    updated = response.get_json()
    assert updated["title"] == "停水通知（改期）"
    assert updated["body"] == "改到週六。"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["created_at"]
    assert client.get(f"/announcements/{created['id']}").get_json() == updated


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
    payload = {"json": {"title": "標題", "body": "內文"}} if method == "put" else {}
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
