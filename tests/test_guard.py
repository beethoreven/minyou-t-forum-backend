"""防濫用：請求頻率、發文數量、登入憑證數量。"""

import threading

import psycopg

from auth_utils import guard
from auth_utils.guard import RateLimiter, WriteLimit
from conftest import ADMIN_EMAIL

ENTRY = {"title": "標題", "body": "內文"}


def test_rate_limiter_counts_each_key_on_its_own(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("auth_utils.guard.time.monotonic", lambda: now[0])
    limiter = RateLimiter(limit=3, window_seconds=60)

    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    assert limiter.hit("a") == 61  # 第四次被擋，要等最舊的那一次離開這一分鐘
    assert limiter.hit("b") is None  # 別人不受影響

    now[0] += 30
    assert limiter.hit("a") == 31  # 被擋的那幾次不算數，所以不會越擋越久

    now[0] += 31
    assert limiter.hit("a") is None  # 一分鐘過了，又可以了


def test_too_many_requests_get_429_with_retry_after(app, add_member):
    resident = add_member("resident@example.com")
    app.extensions["rate_limiter"].limit = 3
    app.extensions["rate_limiter"]._hits.clear()

    assert [resident.get("/posts").status_code for _ in range(3)] == [200, 200, 200]
    blocked = resident.get("/posts")
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1
    assert resident.get("/auth/status").status_code == 429  # 每一種請求都算在一起


def test_rate_limit_is_per_account_and_skips_the_admin(app, add_member, sign_in, client):
    first = add_member("first@example.com")
    second = add_member("second@example.com")
    stranger = sign_in("stranger@example.com")  # 登入了、還沒註冊的人也一樣要算
    app.extensions["rate_limiter"].limit = 2
    app.extensions["rate_limiter"]._hits.clear()

    assert [first.get("/posts").status_code for _ in range(3)] == [200, 200, 429]
    assert second.get("/posts").status_code == 200  # 一個人被擋，不影響別人
    assert [stranger.get("/auth/status").status_code for _ in range(3)] == [200, 200, 429]
    assert [client.get("/posts").status_code for _ in range(5)] == [200] * 5  # 管理員不限


def test_post_limit(add_member, client, monkeypatch):
    monkeypatch.setattr(guard, "POST_LIMITS", (WriteLimit(600, 2, "10 分鐘"),))
    resident = add_member("resident@example.com")
    other = add_member("other@example.com")

    assert [resident.post("/posts", json=ENTRY).status_code for _ in range(2)] == [201, 201]
    blocked = resident.post("/posts", json=ENTRY)
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "write_limited"
    assert "10 分鐘" in blocked.get_json()["error"]
    assert len(client.get("/posts").get_json()["items"]) == 2  # 第三篇沒有寫進去

    assert other.post("/posts", json=ENTRY).status_code == 201  # 各算各的
    assert [client.post("/posts", json=ENTRY).status_code for _ in range(3)] == [201] * 3  # 管理員不限
    assert resident.get("/posts").status_code == 200  # 只是不能再發，其他照常


def test_reply_limit(add_member, client, monkeypatch):
    monkeypatch.setattr(guard, "REPLY_LIMITS", (WriteLimit(600, 2, "10 分鐘"),))
    resident = add_member("resident@example.com")
    post = client.post("/posts", json=ENTRY).get_json()
    path = f"/posts/{post['id']}/replies"

    assert [resident.post(path, json={"body": "回覆"}).status_code for _ in range(2)] == [201, 201]
    blocked = resident.post(path, json={"body": "回覆"})
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "write_limited"
    assert len(client.get(f"/posts/{post['id']}").get_json()["replies"]) == 2


def test_only_recent_posts_count_toward_the_limit(add_member, config, monkeypatch):
    monkeypatch.setattr(guard, "POST_LIMITS", (WriteLimit(600, 1, "10 分鐘"),))
    resident = add_member("resident@example.com")
    assert resident.post("/posts", json=ENTRY).status_code == 201
    assert resident.post("/posts", json=ENTRY).status_code == 429

    with psycopg.connect(config.database_url) as conn:
        conn.execute("UPDATE posts SET created_at = now() - interval '11 minutes'")
    assert resident.post("/posts", json=ENTRY).status_code == 201  # 那一篇已經超過 10 分鐘


def test_posting_at_the_same_moment_cannot_slip_past_the_limit(add_member, app, monkeypatch):
    """同一個人同時送出好幾篇：大家不能都數到「還沒超過」就一起寫進去。"""
    monkeypatch.setattr(guard, "POST_LIMITS", (WriteLimit(600, 2, "10 分鐘"),))
    resident = add_member("resident@example.com")
    start = threading.Barrier(8)
    results = []

    def send():
        client = app.test_client()
        client.session_token = resident.session_token
        start.wait()
        results.append(client.post("/posts", json=ENTRY).status_code)

    threads = [threading.Thread(target=send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [201, 201] + [429] * 6


def test_an_account_keeps_only_its_newest_sessions(app, sign_in, config):
    app.extensions["logins_per_account"].limit = 100  # 這裡要同一個帳號連續登入很多次
    clients = [sign_in(ADMIN_EMAIL) for _ in range(guard.MAX_SESSIONS_PER_ACCOUNT + 2)]

    with psycopg.connect(config.database_url) as conn:
        count = conn.execute("SELECT count(*) FROM sessions WHERE email = %s", (ADMIN_EMAIL,)).fetchone()[0]
    assert count == guard.MAX_SESSIONS_PER_ACCOUNT
    assert clients[-1].get("/auth/status").status_code == 200  # 最新登入的還能用
