"""啟動時的準備工作：建表、文件資料夾，以及重新啟動之後登入還在。"""

from dataclasses import replace

import psycopg

from app import create_app


def test_restarting_keeps_data_and_sessions(app, client, config):
    created = client.post("/announcements", json={"title": "停水通知", "body": "內文"}).get_json()

    restarted = create_app(config).test_client()  # 同一個資料庫再啟動一次

    # 重新啟動前拿到的登入憑證還能用，不用重新登入
    headers = {"Authorization": f"Bearer {client.session_token}"}
    assert restarted.get(f"/announcements/{created['id']}", headers=headers).get_json() == created
    with psycopg.connect(config.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_creates_the_documents_folder_when_missing(app, config, tmp_path):
    folder = tmp_path / "還沒建立" / "documents"
    create_app(replace(config, documents_dir=folder))
    assert folder.is_dir()
