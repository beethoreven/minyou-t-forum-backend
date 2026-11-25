"""社區文檔的 API：文件清單、下載，以及後台的上傳與刪除。測試用本機資料夾放文檔。"""

import io
import os
from urllib.parse import quote

import psycopg
import pytest

from auth_utils import guard
from auth_utils.guard import WriteLimit
from documents.base import DocumentsUnavailable
from documents.local_folder import LocalFolder

PDF = b"%PDF-1.7 fake content \x00\x01\x02"


def upload(client, name, content=PDF):
    """照瀏覽器上傳檔案的格式（multipart/form-data）送出一個檔案。"""
    return client.post(
        "/admin/documents",
        data={"file": (io.BytesIO(content), name)},
        content_type="multipart/form-data",
    )


def listed(client) -> dict:
    """現在的文件清單：{檔名: 代號}。"""
    return {item["name"]: item["id"] for item in client.get("/documents").get_json()["items"]}


def test_empty_folder(client):
    assert client.get("/documents").get_json() == {"items": []}


def test_lists_only_regular_files_sorted_by_name(client, documents_dir):
    (documents_dir / "b 管理辦法.pdf").write_bytes(b"12345")
    (documents_dir / "a 社區公約.pdf").write_bytes(b"123")
    (documents_dir / ".DS_Store").write_bytes(b"hidden")
    (documents_dir / "子資料夾").mkdir()
    (documents_dir / "子資料夾" / "裡面的檔案.pdf").write_bytes(b"nested")

    items = client.get("/documents").get_json()["items"]
    assert [item["name"] for item in items] == ["a 社區公約.pdf", "b 管理辦法.pdf"]
    assert [item["size"] for item in items] == [3, 5]
    for item in items:
        assert set(item) == {"id", "name", "size", "modified_time"}
        assert item["id"].isalnum()
        assert item["modified_time"].endswith("Z")


def test_does_not_list_links_to_files_outside_the_folder(client, documents_dir, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not a community document")
    os.symlink(outside, documents_dir / "link.txt")

    assert client.get("/documents").get_json()["items"] == []


def test_download_returns_the_file_with_its_chinese_name(client, documents_dir):
    content = b"%PDF-1.7 fake content \x00\x01\x02"
    (documents_dir / "社區公約.pdf").write_bytes(content)
    file_id = client.get("/documents").get_json()["items"][0]["id"]

    response = client.get(f"/documents/{file_id}/download")
    assert response.status_code == 200
    assert response.data == content
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=UTF-8''" + quote("社區公約.pdf") in disposition
    response.close()


def test_the_same_file_keeps_the_same_id(client, documents_dir):
    (documents_dir / "社區公約.pdf").write_bytes(b"1")
    first = client.get("/documents").get_json()["items"][0]["id"]
    (documents_dir / "另一份.pdf").write_bytes(b"2")
    ids = {item["name"]: item["id"] for item in client.get("/documents").get_json()["items"]}
    assert ids["社區公約.pdf"] == first


def test_unknown_id_is_404(client, documents_dir):
    (documents_dir / "社區公約.pdf").write_bytes(b"1")
    response = client.get("/documents/0123456789abcdef/download")
    assert response.status_code == 404
    assert response.get_json()["code"] == "document_not_found"


def test_cannot_reach_files_outside_the_folder(client, documents_dir, tmp_path):
    (documents_dir / "社區公約.pdf").write_bytes(b"1")
    (tmp_path / "secret.txt").write_bytes(b"secret")

    for path in (
        "/documents/../secret.txt/download",
        "/documents/..%2Fsecret.txt/download",
        "/documents/%2e%2e/download",
        "/documents/secret.txt/download",
        "/documents/社區公約.pdf/download",  # 檔名本身也不是代號
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert b"secret" not in response.data


def test_removed_file_is_404(client, documents_dir):
    target = documents_dir / "社區公約.pdf"
    target.write_bytes(b"1")
    file_id = client.get("/documents").get_json()["items"][0]["id"]
    target.unlink()

    response = client.get(f"/documents/{file_id}/download")
    assert response.status_code == 404
    assert response.get_json()["code"] == "document_not_found"


def test_unreadable_folder_is_503(client, documents_dir):
    documents_dir.rmdir()

    for path in ("/documents", "/documents/0123456789abcdef/download"):
        response = client.get(path)
        assert response.status_code == 503, path
        assert response.get_json()["code"] == "documents_unavailable"


# ── 後台：上傳與刪除 ─────────────────────────────────────


def test_admin_page_is_told_whether_it_can_upload(client):
    info = client.get("/admin/documents").get_json()
    assert info["can_upload"] is True
    assert info["max_bytes"] == 10 * 1024 * 1024
    assert info["manage_url"] is None
    assert ".pdf" in info["extensions"] and ".exe" not in info["extensions"]


def test_upload_then_download(client, documents_dir):
    response = upload(client, "社區公約.pdf")
    assert response.status_code == 201
    assert response.get_json() == {"name": "社區公約.pdf"}
    assert (documents_dir / "社區公約.pdf").read_bytes() == PDF

    file_id = listed(client)["社區公約.pdf"]
    download = client.get(f"/documents/{file_id}/download")
    assert download.data == PDF
    download.close()
    # 上傳時用的暫存檔不會留下來
    assert [entry.name for entry in documents_dir.iterdir()] == ["社區公約.pdf"]


def test_uploading_the_same_name_replaces_the_old_file(client, documents_dir):
    upload(client, "社區公約.pdf", b"old version")
    assert upload(client, "社區公約.pdf", b"new version").status_code == 201

    assert list(listed(client)) == ["社區公約.pdf"]
    assert (documents_dir / "社區公約.pdf").read_bytes() == b"new version"


def test_delete(client, documents_dir):
    upload(client, "社區公約.pdf")
    upload(client, "管理辦法.pdf")
    file_id = listed(client)["社區公約.pdf"]

    response = client.delete(f"/admin/documents/{file_id}")
    assert response.status_code == 204
    assert list(listed(client)) == ["管理辦法.pdf"]
    assert not (documents_dir / "社區公約.pdf").exists()

    again = client.delete(f"/admin/documents/{file_id}")
    assert again.status_code == 404
    assert again.get_json()["code"] == "document_not_found"


def test_only_staff_can_upload_and_delete(client, add_member, documents_dir):
    resident = add_member("resident@example.com")
    moderator = add_member("moderator@example.com", role="moderator")
    upload(client, "社區公約.pdf")
    file_id = listed(client)["社區公約.pdf"]

    for response in (
        upload(resident, "偷放的.pdf"),
        resident.delete(f"/admin/documents/{file_id}"),
        resident.get("/admin/documents"),
    ):
        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"
    assert list(listed(client)) == ["社區公約.pdf"]  # 什麼都沒有變

    assert upload(moderator, "版主放的.pdf").status_code == 201
    assert moderator.delete(f"/admin/documents/{file_id}").status_code == 204


def test_upload_needs_a_session(anonymous, documents_dir):
    assert upload(anonymous, "社區公約.pdf").status_code == 401
    assert list(documents_dir.iterdir()) == []


def test_a_path_in_the_file_name_is_dropped(client, documents_dir, tmp_path):
    for sent in ("../../outside.txt", "/etc/outside.txt", "a/b/../outside.txt"):
        assert upload(client, sent).get_json() == {"name": "outside.txt"}
    assert [entry.name for entry in documents_dir.iterdir()] == ["outside.txt"]
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize(
    "name, content, code",
    [
        (".hidden", PDF, "file_name_invalid"),
        ("..", PDF, "file_name_invalid"),
        ("   ", PDF, "file_name_invalid"),
        ("響鈴\x07.pdf", PDF, "file_name_invalid"),
        ("長" * 101, PDF, "file_name_too_long"),
        ("空的.pdf", b"", "file_empty"),
    ],
)
def test_rejects_bad_uploads(client, documents_dir, name, content, code):
    response = upload(client, name, content)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert list(documents_dir.iterdir()) == []


def test_upload_without_a_file(client):
    for response in (
        client.post("/admin/documents", data={}, content_type="multipart/form-data"),
        client.post("/admin/documents", json={"file": "不是檔案"}),
    ):
        assert response.status_code == 400
        assert response.get_json()["code"] == "file_required"


def test_upload_only_accepts_document_types(client, documents_dir):
    for name in ("安裝程式.exe", "網頁.html", "指令.js", "沒有副檔名"):
        response = upload(client, name)
        assert response.status_code == 400
        assert response.get_json()["code"] == "file_type_not_allowed"
        assert not (documents_dir / name).exists()
    assert upload(client, "照片.JPG").status_code == 201  # 大小寫不分


def test_upload_size_limit(client, documents_dir, monkeypatch):
    monkeypatch.setattr("app.MAX_DOCUMENT_BYTES", 20)

    assert upload(client, "剛好.pdf", b"x" * 20).status_code == 201
    too_large = upload(client, "超過.pdf", b"x" * 21)
    assert too_large.status_code == 413
    assert too_large.get_json()["code"] == "file_too_large"
    assert [entry.name for entry in documents_dir.iterdir()] == ["剛好.pdf"]

    # 大到連整個請求都超過上限的，還沒讀檔案就會被擋下來
    monkeypatch.setattr("app.UPLOAD_OVERHEAD_BYTES", 500)
    huge = upload(client, "太大.pdf", b"x" * 5000)
    assert huge.status_code == 413
    assert huge.get_json()["code"] == "payload_too_large"


def test_a_long_chinese_name_at_the_limit_is_fine(client):
    name = "長" * 96 + ".pdf"
    assert upload(client, name).get_json() == {"name": name}


def test_upload_and_delete_are_refused_when_the_source_is_read_only(client, documents_dir, monkeypatch):
    (documents_dir / "社區公約.pdf").write_bytes(PDF)
    file_id = listed(client)["社區公約.pdf"]
    monkeypatch.setattr(LocalFolder, "can_upload", False)

    assert client.get("/admin/documents").get_json()["can_upload"] is False
    for response in (upload(client, "新的.pdf"), client.delete(f"/admin/documents/{file_id}")):
        assert response.status_code == 503
        assert response.get_json()["code"] == "upload_unavailable"
    assert list(listed(client)) == ["社區公約.pdf"]


def test_a_failed_upload_tells_staff_what_to_check(client, monkeypatch):
    def misconfigured(self, name, content):
        raise DocumentsUnavailable("只進 log 的細節", hint="請檢查某一項設定")

    monkeypatch.setattr(LocalFolder, "save", misconfigured)
    response = upload(client, "社區公約.pdf")
    assert response.status_code == 503
    assert response.get_json() == {"error": "請檢查某一項設定", "code": "documents_unavailable"}

    def down(self, name, content):
        raise DocumentsUnavailable("只進 log 的細節")

    monkeypatch.setattr(LocalFolder, "save", down)
    response = upload(client, "社區公約.pdf")
    assert response.status_code == 503
    assert "只進 log 的細節" not in response.get_data(as_text=True)


# ── 上傳的量 ─────────────────────────────────────────────


def test_moderators_have_an_upload_limit_but_the_admin_does_not(add_member, client, monkeypatch):
    monkeypatch.setattr(guard, "UPLOAD_LIMITS", (WriteLimit(600, 2, "10 分鐘"),))
    moderator = add_member("moderator@example.com", role="moderator")

    assert [upload(moderator, f"第{n}份.pdf").status_code for n in range(2)] == [201, 201]
    blocked = upload(moderator, "第三份.pdf")
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "write_limited"
    assert "第三份.pdf" not in listed(client)  # 沒有存進去
    assert [upload(client, f"管理員{n}.pdf").status_code for n in range(3)] == [201] * 3


def test_moderators_have_a_daily_upload_total(add_member, monkeypatch):
    monkeypatch.setattr(guard, "UPLOAD_BYTES_PER_DAY", 1024 * 1024)  # 一天 1 MB
    moderator = add_member("moderator@example.com", role="moderator")

    assert upload(moderator, "大的.pdf", b"x" * (700 * 1024)).status_code == 201
    blocked = upload(moderator, "又一個.pdf", b"x" * (400 * 1024))
    assert blocked.status_code == 429
    assert "1 MB" in blocked.get_json()["error"]
    assert upload(moderator, "小的.pdf", b"x" * (300 * 1024)).status_code == 201  # 還在總量內


def test_a_failed_upload_does_not_use_up_the_limit(add_member, config, monkeypatch):
    monkeypatch.setattr(guard, "UPLOAD_LIMITS", (WriteLimit(600, 1, "10 分鐘"),))
    moderator = add_member("moderator@example.com", role="moderator")

    def down(self, name, content):
        raise DocumentsUnavailable("暫時連不上")

    with monkeypatch.context() as patch:
        patch.setattr(LocalFolder, "save", down)
        assert upload(moderator, "社區公約.pdf").status_code == 503
    assert upload(moderator, "社區公約.pdf").status_code == 201  # 失敗的那一次不算數
    with psycopg.connect(config.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM document_uploads").fetchone()[0] == 1
