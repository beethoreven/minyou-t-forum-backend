"""文章附圖。測試用本機資料夾存圖，不連到 Google。"""

import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import _sources
from documents.pictures import DatabasePictures, DrivePictures, PicturesUnavailable, PictureStores

# 不是真的圖片，只是檔頭長得像 JPEG 的幾個位元組：後端只檢查檔頭與大小。
JPEG_A = b"\xff\xd8\xff\xe0" + b"first picture"
JPEG_B = b"\xff\xd8\xff\xe0" + b"second picture"


def encoded(content: bytes) -> dict:
    return {"data": base64.b64encode(content).decode("ascii")}


def create_post(client, images):
    return client.post("/posts", json={"title": "頂樓的花", "body": "開了。", "images": images})


def test_status_says_pictures_can_be_attached(client):
    assert client.get("/auth/status").get_json()["can_attach_pictures"] is True


def test_post_with_pictures(client, documents_dir):
    response = create_post(client, [encoded(JPEG_A), encoded(JPEG_B)])
    assert response.status_code == 201
    post = response.get_json()
    assert len(post["images"]) == 2
    assert client.get(f"/posts/{post['id']}").get_json() == post

    first, second = (client.get(f"/posts/{post['id']}/images/{image['id']}") for image in post["images"])
    assert first.status_code == 200
    assert first.mimetype == "image/jpeg"
    assert first.data == JPEG_A  # 照上傳的順序
    assert second.data == JPEG_B

    # 圖片存在文件資料夾底下的 Pic，而且不會出現在社區文檔的清單裡
    assert len(list((documents_dir / "Pic").iterdir())) == 2
    assert client.get("/documents").get_json() == {"items": []}


def test_post_without_pictures_still_works(client):
    post = client.post("/posts", json={"title": "標題", "body": "內文"}).get_json()
    assert post["images"] == []


def test_a_picture_belongs_to_its_own_post_only(client):
    with_picture = create_post(client, [encoded(JPEG_A)]).get_json()
    other = create_post(client, []).get_json()
    image_id = with_picture["images"][0]["id"]

    wrong_post = client.get(f"/posts/{other['id']}/images/{image_id}")
    assert wrong_post.status_code == 404
    assert wrong_post.get_json()["code"] == "image_not_found"
    assert client.get(f"/posts/{with_picture['id']}/images/99999").status_code == 404


def test_pictures_need_a_session(client, anonymous):
    post = create_post(client, [encoded(JPEG_A)]).get_json()
    response = anonymous.get(f"/posts/{post['id']}/images/{post['images'][0]['id']}")
    assert response.status_code == 401


@pytest.mark.parametrize(
    "images, code",
    [
        ([encoded(JPEG_A)] * 6, "too_many_images"),
        ([encoded(b"GIF89a not a jpeg")], "image_not_jpeg"),
        ([encoded(b"\xff\xd8\xff" + b"x" * 300 * 1024)], "image_too_large"),
        ([{"data": "不是 base64"}], "images_invalid"),
        ([{"name": "沒有內容"}], "images_invalid"),
        ("不是清單", "images_invalid"),
    ],
)
def test_rejects_bad_pictures(client, documents_dir, images, code):
    response = create_post(client, images)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert client.get("/posts").get_json()["items"] == []  # 文章沒有建立
    assert not (documents_dir / "Pic").exists()  # 圖片也沒有存


def test_five_pictures_at_the_size_limit_fit_in_one_request(client):
    biggest = b"\xff\xd8\xff" + b"x" * (300 * 1024 - 3)
    response = create_post(client, [encoded(biggest)] * 5)
    assert response.status_code == 201
    assert len(response.get_json()["images"]) == 5


def test_deleting_the_post_deletes_its_pictures(client, documents_dir):
    post = create_post(client, [encoded(JPEG_A), encoded(JPEG_B)]).get_json()
    assert client.delete(f"/posts/{post['id']}").status_code == 204
    assert list((documents_dir / "Pic").iterdir()) == []


def test_editing_the_text_keeps_the_pictures(client):
    post = create_post(client, [encoded(JPEG_A)]).get_json()
    updated = client.put(f"/posts/{post['id']}", json={"title": "新標題", "body": "新內文"}).get_json()
    assert updated["images"] == post["images"]


# ── 正式環境預設的存放處：資料庫 ──────────────────────────


def test_database_pictures_round_trip(app, config):
    store = DatabasePictures(config.database_url)

    ids = store.save([JPEG_A, JPEG_B])
    assert len(ids) == 2
    assert all(picture_id.startswith("db:") for picture_id in ids)
    assert b"".join(store.open(ids[0])) == JPEG_A
    assert b"".join(store.open(ids[1])) == JPEG_B
    assert store.open("db:no-such-picture") is None

    store.delete([ids[0]])
    assert store.open(ids[0]) is None
    assert b"".join(store.open(ids[1])) == JPEG_B


class RecordingStore:
    """記下自己被要求做了什麼的假存放處。"""

    def __init__(self, name):
        self.name = name
        self.deleted = []

    def save(self, images):
        return [f"saved-by-{self.name}"]

    def open(self, picture_id):
        return iter([f"{self.name}:{picture_id}".encode()])

    def delete(self, picture_ids):
        self.deleted.extend(picture_ids)


def test_pictures_are_found_by_where_their_id_says_they_are():
    database, drive = RecordingStore("database"), RecordingStore("drive")
    stores = PictureStores(writer=database, database=database, drive=drive)

    assert stores.save([JPEG_A]) == ["saved-by-database"]  # 新的圖存進 writer
    assert b"".join(stores.open("db:abc")) == b"database:db:abc"
    assert b"".join(stores.open("drive-file-id")) == b"drive:drive-file-id"

    stores.delete(["db:abc", "drive-file-id", "db:def"])
    assert database.deleted == ["db:abc", "db:def"]
    assert drive.deleted == ["drive-file-id"]


def test_drive_pictures_cannot_save_without_the_script():
    store = DrivePictures(session=None, script_url=None, script_secret=None)
    with pytest.raises(PicturesUnavailable):
        store.save([JPEG_A])
    store.delete(["file-aaaaaaaaaa"])  # 刪不掉不算錯，不會丟例外


def test_production_setup_picks_where_new_pictures_go(config):
    """用 Google Drive 放文件時：沒設定 Apps Script，新的圖存資料庫；有設定才存 Google Drive。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    service_account = {
        "type": "service_account",
        "client_email": "bot@example.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    on_drive = replace(
        config,
        documents_dir=None,
        service_account_info=service_account,
        drive_folder_id="folder-id-0123456789",
    )

    documents, pictures = _sources(on_drive)
    assert documents.manage_info()["manage_url"].endswith("/folder-id-0123456789")
    assert isinstance(pictures.writer, DatabasePictures)

    with_script = replace(
        on_drive,
        apps_script_url="https://script.google.com/macros/s/x/exec",
        apps_script_secret="s3cret",
    )
    _, pictures = _sources(with_script)
    assert isinstance(pictures.writer, DrivePictures)
    assert isinstance(pictures.database, DatabasePictures)  # 以前存在資料庫的圖還是找得到


# ── 把圖存進 Google Drive 的那一段：請 Apps Script 代勞 ────


class FakeReply:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_drive_pictures_are_saved_through_the_script(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(url=url, body=json, timeout=timeout)
        return FakeReply({"ok": True, "ids": ["file-aaaaaaaaaa", "file-bbbbbbbbbb"]})

    monkeypatch.setattr("documents.pictures.requests.post", fake_post)
    store = DrivePictures(session=None, script_url="https://script.google.com/macros/s/x/exec", script_secret="s3cret")

    assert store.save([JPEG_A, JPEG_B]) == ["file-aaaaaaaaaa", "file-bbbbbbbbbb"]
    assert sent["url"] == "https://script.google.com/macros/s/x/exec"
    assert sent["timeout"]
    assert sent["body"]["secret"] == "s3cret"
    assert sent["body"]["action"] == "upload"
    assert [base64.b64decode(file["data"]) for file in sent["body"]["files"]] == [JPEG_A, JPEG_B]


@pytest.mark.parametrize(
    "reply",
    [
        FakeReply({"ok": False, "error": "forbidden"}),
        FakeReply({"ok": True, "ids": ["only-one-id-0000"]}),  # 張數對不上
        FakeReply({"ok": True, "ids": ["../bad", "file-bbbbbbbbbb"]}),
        FakeReply("not a dict"),
        FakeReply({"ok": True, "ids": []}, status_code=500),
    ],
)
def test_drive_pictures_refuse_a_reply_that_is_not_a_clear_success(monkeypatch, reply):
    monkeypatch.setattr("documents.pictures.requests.post", lambda url, json, timeout: reply)
    store = DrivePictures(session=None, script_url="https://script.google.com/x", script_secret="s3cret")
    with pytest.raises(PicturesUnavailable):
        store.save([JPEG_A, JPEG_B])
