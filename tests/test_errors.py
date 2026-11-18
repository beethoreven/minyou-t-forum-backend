"""每一種錯誤都用同一個 JSON 格式回報，以及跨來源的規則。"""

import psycopg

from conftest import WEB_ORIGIN


def assert_error(response, status, code):
    assert response.status_code == status
    body = response.get_json()
    assert set(body) == {"error", "code"}
    assert body["code"] == code
    assert body["error"]


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_unknown_path(client):
    assert_error(client.get("/no-such-path"), 404, "not_found")


def test_wrong_method(client):
    assert_error(client.delete("/announcements"), 405, "method_not_allowed")


def test_body_that_is_not_json(client):
    response = client.post("/announcements", data="title=abc", content_type="text/plain")
    assert_error(response, 400, "invalid_json")


def test_broken_json(client):
    response = client.post("/announcements", data="{broken", content_type="application/json")
    assert_error(response, 400, "invalid_json")


def test_json_that_is_not_an_object(client):
    assert_error(client.post("/announcements", json=["標題", "內文"]), 400, "invalid_json")


def test_oversized_request(client):
    response = client.post("/announcements", json={"title": "標題", "body": "字" * 30_000})
    assert_error(response, 413, "payload_too_large")
    assert client.get("/announcements").get_json()["items"] == []


def test_unexpected_error_hides_the_details(client, monkeypatch):
    def explode(conn, before):
        raise RuntimeError("資料表 secret_table 壞了")

    monkeypatch.setattr("db.announcements.list_page", explode)
    response = client.get("/announcements")
    assert_error(response, 500, "internal_error")
    assert "secret_table" not in response.get_data(as_text=True)


def test_database_down_is_503(client, monkeypatch):
    def refuse(database_url):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("app.connect", refuse)
    assert_error(client.get("/announcements"), 503, "database_unavailable")


def test_chinese_is_sent_as_is(client):
    client.post("/announcements", json={"title": "停水通知", "body": "內文"})
    raw = client.get("/announcements").get_data()
    assert "停水通知".encode() in raw  # 不是 \uXXXX


def test_allows_the_configured_web_origin(client):
    response = client.get("/health", headers={"Origin": WEB_ORIGIN})
    assert response.headers.get("Access-Control-Allow-Origin") == WEB_ORIGIN


def test_does_not_allow_other_origins(client):
    response = client.get("/health", headers={"Origin": "http://localhost:9999"})
    assert "Access-Control-Allow-Origin" not in response.headers


def test_preflight_for_put_and_delete(client):
    for method in ("PUT", "DELETE"):
        response = client.options(
            "/announcements/1",
            headers={
                "Origin": WEB_ORIGIN,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("Access-Control-Allow-Origin") == WEB_ORIGIN
        assert method in response.headers.get("Access-Control-Allow-Methods", "")
        assert "content-type" in response.headers.get("Access-Control-Allow-Headers", "").lower()
