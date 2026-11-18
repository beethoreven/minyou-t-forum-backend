"""社區文檔的 API：文件清單與下載。"""

import os
from urllib.parse import quote


def test_empty_folder(client):
    assert client.get("/documents").get_json() == {"items": []}


def test_admin_page_is_told_where_documents_live(client, documents_dir):
    info = client.get("/admin/documents").get_json()
    assert info == {"source": "local_folder", "manage_url": None, "location": str(documents_dir)}


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
