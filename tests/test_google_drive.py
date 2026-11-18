"""讀寫 Google Drive 資料夾的邏輯。不連到 Google：用假的 HTTP session 與假的回應。"""

import base64
from dataclasses import replace

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import _documents
from documents.base import DocumentsUnavailable
from documents.google_drive import FILES_URL, GoogleDriveFolder

FOLDER = "folder-id-0123456789"
PDF = {"id": "file-aaaaaaaaaa", "name": "b 社區公約.pdf", "mimeType": "application/pdf",
       "size": "5", "modifiedTime": "2026-11-16T13:07:12.000Z"}
WORD = {"id": "file-bbbbbbbbbb", "name": "a 管理辦法.docx", "mimeType": "application/msword",
        "size": "7", "modifiedTime": "2026-11-17T01:00:00.000Z"}
GOOGLE_DOC = {"id": "file-cccccccccc", "name": "線上文件", "mimeType": "application/vnd.google-apps.document",
              "modifiedTime": "2026-11-17T01:00:00.000Z"}
SUBFOLDER = {"id": "file-dddddddddd", "name": "子資料夾", "mimeType": "application/vnd.google-apps.folder",
             "modifiedTime": "2026-11-17T01:00:00.000Z"}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self._content = content
        self.closed = False

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._content

    def close(self):
        self.closed = True


class FakeSession:
    """照順序回預先準備好的回應，並記下每次被呼叫的網址與參數。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, stream=False, timeout=None):
        self.calls.append((url, params))
        assert timeout, "每個對外連線都要設逾時"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_manage_url_points_at_the_folder_in_google_drive():
    folder = GoogleDriveFolder(FOLDER, FakeSession())
    assert folder.manage_url == f"https://drive.google.com/drive/folders/{FOLDER}"


def test_list_follows_every_page_and_skips_google_native_items():
    session = FakeSession(
        FakeResponse(payload={"files": [PDF, GOOGLE_DOC], "nextPageToken": "page-2"}),
        FakeResponse(payload={"files": [SUBFOLDER, WORD]}),
    )
    files = GoogleDriveFolder(FOLDER, session).list_files()

    assert [file.name for file in files] == ["a 管理辦法.docx", "b 社區公約.pdf"]  # 照檔名排序
    assert [file.size for file in files] == [7, 5]
    assert files[1].modified_time.isoformat() == "2026-11-16T13:07:12+00:00"
    assert len(session.calls) == 2
    assert f"'{FOLDER}' in parents" in session.calls[0][1]["q"]
    assert session.calls[1][1]["pageToken"] == "page-2"


def test_open_file_downloads_a_file_in_the_folder():
    session = FakeSession(
        FakeResponse(payload=PDF | {"parents": [FOLDER], "trashed": False}),
        FakeResponse(content=b"%PDF-"),
    )
    file, chunks = GoogleDriveFolder(FOLDER, session).open_file(PDF["id"])

    assert file.name == "b 社區公約.pdf"
    assert b"".join(chunks) == b"%PDF-"
    assert session.calls[1] == (f"{FILES_URL}/{PDF['id']}", {"alt": "media", "supportsAllDrives": "true"})


@pytest.mark.parametrize(
    "metadata",
    [
        PDF | {"parents": ["some-other-folder"], "trashed": False},  # 不在指定的資料夾裡
        PDF | {"parents": [FOLDER], "trashed": True},  # 在垃圾桶
        GOOGLE_DOC | {"parents": [FOLDER], "trashed": False},  # 線上格式
        PDF | {"trashed": False},  # 看不到它在哪個資料夾
    ],
)
def test_open_file_refuses_files_that_are_not_listed(metadata):
    session = FakeSession(FakeResponse(payload=metadata))
    assert GoogleDriveFolder(FOLDER, session).open_file(metadata["id"]) is None
    assert len(session.calls) == 1  # 沒有去下載


def test_open_file_with_unknown_or_malformed_id():
    session = FakeSession(FakeResponse(status_code=404))
    folder = GoogleDriveFolder(FOLDER, session)
    assert folder.open_file("file-zzzzzzzzzz") is None

    for bad_id in ("../etc/passwd", "abc?alt=media", "短", ""):
        assert folder.open_file(bad_id) is None
    assert len(session.calls) == 1  # 格式不對的代號根本不會送到 Google


@pytest.mark.parametrize(
    "response",
    [FakeResponse(status_code=403), FakeResponse(status_code=500), requests.ConnectionError("down")],
)
def test_google_errors_mean_unavailable(response):
    with pytest.raises(DocumentsUnavailable):
        GoogleDriveFolder(FOLDER, FakeSession(response)).list_files()


# ── 上傳與刪除：請 Apps Script 代勞 ───────────────────────

SCRIPT_URL = "https://script.google.com/macros/s/x/exec"
SECRET = "s3cret-shared-with-the-script"


class FakeReply:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def with_script(monkeypatch, reply):
    """一個設定好 Apps Script 的資料夾，以及記下「送了什麼給 Apps Script」的清單。"""
    sent = []

    def fake_post(url, json, timeout):
        sent.append({"url": url, "body": json, "timeout": timeout})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("documents.google_drive.requests.post", fake_post)
    return GoogleDriveFolder(FOLDER, FakeSession(), SCRIPT_URL, SECRET), sent


def test_cannot_upload_without_the_script():
    folder = GoogleDriveFolder(FOLDER, FakeSession())
    assert folder.can_upload is False
    with pytest.raises(DocumentsUnavailable):
        folder.save("社區公約.pdf", b"%PDF-")


def test_save_sends_the_file_to_the_script(monkeypatch):
    folder, sent = with_script(monkeypatch, FakeReply({"ok": True}))
    assert folder.can_upload is True

    folder.save("社區公約.pdf", b"%PDF-1.7")
    assert len(sent) == 1
    assert sent[0]["url"] == SCRIPT_URL
    assert sent[0]["timeout"], "每個對外連線都要設逾時"
    assert sent[0]["body"] == {
        "action": "upload",
        "name": "社區公約.pdf",
        "content_type": "application/pdf",  # 從副檔名判斷
        "data": base64.b64encode(b"%PDF-1.7").decode("ascii"),
        "secret": SECRET,
    }


def test_a_file_of_an_unknown_type_is_sent_as_plain_bytes(monkeypatch):
    folder, sent = with_script(monkeypatch, FakeReply({"ok": True}))
    folder.save("沒有副檔名", b"content")
    assert sent[0]["body"]["content_type"] == "application/octet-stream"


def test_delete_asks_the_script_to_trash_the_file(monkeypatch):
    folder, sent = with_script(monkeypatch, FakeReply({"ok": True}))
    assert folder.delete(PDF["id"]) is True
    assert sent[0]["body"] == {"action": "trash", "id": PDF["id"], "secret": SECRET}


def test_delete_of_a_missing_or_malformed_id(monkeypatch):
    folder, sent = with_script(monkeypatch, FakeReply({"ok": False, "error": "not_found"}))
    assert folder.delete("file-zzzzzzzzzz") is False
    assert len(sent) == 1

    for bad_id in ("../etc/passwd", "短", ""):
        assert folder.delete(bad_id) is False
    assert len(sent) == 1  # 格式不對的代號根本不會送出去


@pytest.mark.parametrize(
    "reply, hint_mentions",
    [
        # 設定不對：等多久都不會好，要告訴後台的人該檢查什麼
        (FakeReply({"ok": False, "error": "forbidden"}), "APPS_SCRIPT_SECRET"),
        (FakeReply({"ok": False, "error": "folder_unavailable"}), "FOLDER_ID"),
        (FakeReply({"ok": False, "error": "unknown_action"}), "最新版"),
        (FakeReply({"ok": False, "error": "failed", "message": "Exception: 雲端硬碟滿了"}), "雲端硬碟滿了"),
        (FakeReply(ValueError("回來的是登入頁，不是 JSON")), "所有人"),
        (FakeReply("不是物件"), "所有人"),
        (FakeReply({"ok": True}, status_code=404), "APPS_SCRIPT_URL"),
        # 暫時連不上：沒有什麼要檢查的
        (FakeReply({"ok": True}, status_code=503), None),
        (requests.ConnectionError("down"), None),
    ],
)
def test_a_reply_that_is_not_a_clear_success_means_unavailable(monkeypatch, reply, hint_mentions):
    folder, _ = with_script(monkeypatch, reply)
    for action in (lambda: folder.save("社區公約.pdf", b"%PDF-"), lambda: folder.delete(PDF["id"])):
        with pytest.raises(DocumentsUnavailable) as error:
            action()
        hint = error.value.hint
        assert (hint is None) if hint_mentions is None else (hint_mentions in hint)
        assert SECRET not in f"{error.value} {hint}"  # 密語不會被帶進錯誤訊息


def test_saving_only_treats_not_found_as_missing_when_deleting(monkeypatch):
    folder, _ = with_script(monkeypatch, FakeReply({"ok": False, "error": "not_found"}))
    with pytest.raises(DocumentsUnavailable):
        folder.save("社區公約.pdf", b"%PDF-")


def test_production_setup_can_upload_only_with_the_script(config):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    on_drive = replace(
        config,
        documents_dir=None,
        service_account_info={
            "type": "service_account",
            "client_email": "bot@example.iam.gserviceaccount.com",
            "private_key": pem,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        drive_folder_id=FOLDER,
    )

    reading_only = _documents(on_drive)
    assert reading_only.can_upload is False
    assert reading_only.manage_url.endswith(f"/{FOLDER}")

    scripted = _documents(replace(on_drive, apps_script_url=SCRIPT_URL, apps_script_secret=SECRET))
    assert scripted.can_upload is True
