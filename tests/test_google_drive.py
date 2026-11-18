"""讀 Google Drive 資料夾的邏輯。不連到 Google：用假的 HTTP session 回固定的內容。"""

import pytest
import requests

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
