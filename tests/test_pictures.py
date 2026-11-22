"""公告與文章的附圖。兩邊的規則一樣，所以大部分的測試兩邊各跑一次。"""

import base64
import os
from io import BytesIO

import psycopg
import pytest
from PIL import Image

both_boards = pytest.mark.parametrize("board", ["/announcements", "/posts"])

MAX_IMAGE_BYTES = 300 * 1024


def make_jpeg(color=(200, 30, 30), size=(640, 480), exif=None, comment=None, quality=90, mode="RGB", **extra) -> bytes:
    image = Image.new("RGB", size, color).convert(mode)
    output = BytesIO()
    if exif is not None:
        extra["exif"] = exif
    if comment is not None:
        extra["comment"] = comment
    image.save(output, format="JPEG", quality=quality, **extra)
    return output.getvalue()


def noisy_jpeg(target_bytes: int) -> bytes:
    """一張接近 target_bytes 大小、但不超過的雜訊圖。雜訊幾乎壓不小，用來測大小的上限。"""
    width, height = 640, 480
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    for quality in range(95, 10, -5):
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality)
        if output.tell() <= target_bytes:
            return output.getvalue()
    raise AssertionError("產生不出夠小的雜訊圖")


RED = make_jpeg((200, 30, 30))
BLUE = make_jpeg((30, 30, 200))


def encoded(content: bytes) -> dict:
    return {"data": base64.b64encode(content).decode("ascii")}


def create(client, board, images):
    return client.post(board, json={"title": "頂樓的花", "body": "開了。", "images": images})


def stored_pictures(config) -> int:
    """資料庫裡現在存了幾張圖。"""
    with psycopg.connect(config.database_url) as conn:
        return conn.execute("SELECT count(*) FROM stored_pictures").fetchone()[0]


def opened(response) -> Image.Image:
    return Image.open(BytesIO(response.data))


def center_color(image: Image.Image) -> tuple[int, int, int]:
    return image.convert("RGB").getpixel((image.width // 2, image.height // 2))


def close_to(actual, expected, tolerance=12) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


@both_boards
def test_entry_with_pictures(client, config, board):
    response = create(client, board, [encoded(RED), encoded(BLUE)])
    assert response.status_code == 201
    entry = response.get_json()
    assert len(entry["images"]) == 2
    assert client.get(f"{board}/{entry['id']}").get_json() == entry

    first, second = (client.get(f"{board}/{entry['id']}/images/{image['id']}") for image in entry["images"])
    assert first.status_code == 200
    assert first.mimetype == "image/jpeg"
    # 存進去的是伺服器重新存過的 JPEG，內容跟上傳的不同，但畫面一樣，而且照上傳的順序
    assert opened(first).size == (640, 480)
    assert close_to(center_color(opened(first)), (200, 30, 30))
    assert close_to(center_color(opened(second)), (30, 30, 200))
    assert stored_pictures(config) == 2


def test_pictures_are_stored_without_exif_or_comments(client):
    """直接打 API 上傳帶著拍攝位置的原始照片：存進去的圖不能留下任何中繼資料。"""
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # 相機廠牌
    exif[0x0112] = 6  # 這張圖要順時針轉 90 度才是正的
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (25.0, 2.0, 30.0)  # 緯度
    original = make_jpeg(size=(640, 480), exif=exif, comment=b"home address")
    assert Image.open(BytesIO(original)).getexif()  # 原圖確實帶著 EXIF

    entry = create(client, "/posts", [encoded(original)]).get_json()
    picture = client.get(f"/posts/{entry['id']}/images/{entry['images'][0]['id']}")
    stored = opened(picture)

    assert not stored.getexif()
    assert "exif" not in stored.info and "comment" not in stored.info
    assert b"SecretCam" not in picture.data and b"home address" not in picture.data
    assert stored.size == (480, 640)  # 已經照標記轉正，標記本身丟掉了


@pytest.mark.parametrize(
    "variant",
    [
        {"mode": "L"},  # 黑白照片
        {"mode": "CMYK"},  # 印刷用的色彩模式，有些掃描檔是這種
        {"progressive": True},  # 網路上常見的漸進式 JPEG
        {"size": (480, 8000)},  # 480P 的全景照，384 萬畫素
    ],
)
def test_other_kinds_of_jpeg_are_accepted_and_stored_as_rgb(client, variant):
    entry = create(client, "/posts", [encoded(make_jpeg(**variant))])
    assert entry.status_code == 201, entry.get_json()
    entry = entry.get_json()
    stored = opened(client.get(f"/posts/{entry['id']}/images/{entry['images'][0]['id']}"))
    assert stored.format == "JPEG"
    assert stored.mode == "RGB"


@both_boards
def test_entry_without_pictures_still_works(client, board):
    entry = client.post(board, json={"title": "標題", "body": "內文"}).get_json()
    assert entry["images"] == []


@both_boards
def test_a_picture_belongs_to_its_own_entry_only(client, board):
    with_picture = create(client, board, [encoded(RED)]).get_json()
    other = create(client, board, []).get_json()
    image_id = with_picture["images"][0]["id"]

    wrong_entry = client.get(f"{board}/{other['id']}/images/{image_id}")
    assert wrong_entry.status_code == 404
    assert wrong_entry.get_json()["code"] == "image_not_found"
    assert client.get(f"{board}/{with_picture['id']}/images/99999").status_code == 404


def test_announcement_pictures_and_post_pictures_are_kept_apart(client):
    announcement = create(client, "/announcements", [encoded(RED)]).get_json()
    post = create(client, "/posts", [encoded(BLUE)]).get_json()
    assert announcement["id"] == post["id"] and announcement["images"] == post["images"]  # 兩邊的編號剛好一樣

    red = client.get(f"/announcements/{announcement['id']}/images/{announcement['images'][0]['id']}")
    blue = client.get(f"/posts/{post['id']}/images/{post['images'][0]['id']}")
    assert close_to(center_color(opened(red)), (200, 30, 30))
    assert close_to(center_color(opened(blue)), (30, 30, 200))


@both_boards
def test_pictures_need_a_session(client, anonymous, board):
    entry = create(client, board, [encoded(RED)]).get_json()
    response = anonymous.get(f"{board}/{entry['id']}/images/{entry['images'][0]['id']}")
    assert response.status_code == 401


def test_pictures_are_cached_briefly_and_only_for_the_same_session(client):
    entry = create(client, "/posts", [encoded(RED)]).get_json()
    picture = client.get(f"/posts/{entry['id']}/images/{entry['images'][0]['id']}")
    assert picture.headers["Cache-Control"] == "private, max-age=3600"
    assert "Authorization" in picture.headers["Vary"]
    assert picture.headers["X-Content-Type-Options"] == "nosniff"


@both_boards
@pytest.mark.parametrize(
    "images, code",
    [
        ([encoded(RED)] * 6, "too_many_images"),
        ([encoded(b"GIF89a not a jpeg")], "image_not_jpeg"),
        ([encoded(b"\xff\xd8\xff" + b"x" * MAX_IMAGE_BYTES)], "image_too_large"),
        ([encoded(b"\xff\xd8\xff\xe0" + b"not really a picture")], "image_invalid"),
        ([encoded(make_jpeg(size=(9000, 10)))], "image_too_large"),  # 檔案很小、尺寸卻很大
        ([encoded(make_jpeg(size=(2100, 2000)))], "image_too_large"),  # 420 萬畫素，超過上限
        ([{"data": "不是 base64"}], "images_invalid"),
        ([{"name": "沒有內容"}], "images_invalid"),
        ("不是清單", "images_invalid"),
    ],
)
def test_rejects_bad_pictures(client, config, board, images, code):
    response = create(client, board, images)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert client.get(board).get_json()["items"] == []  # 公告或文章沒有建立
    assert stored_pictures(config) == 0  # 圖片也沒有存


@both_boards
def test_good_pictures_are_not_kept_when_the_text_is_rejected(client, config, board):
    response = client.post(board, json={"title": "", "body": "內文", "images": [encoded(RED)]})
    assert response.status_code == 400
    assert stored_pictures(config) == 0


def test_a_member_cannot_post_an_announcement_with_pictures(add_member, config):
    resident = add_member("resident@example.com")
    response = create(resident, "/announcements", [encoded(RED)])
    assert response.status_code == 403
    assert stored_pictures(config) == 0


@both_boards
def test_five_pictures_near_the_size_limit_fit_in_one_request(client, board):
    biggest = noisy_jpeg(MAX_IMAGE_BYTES - 1024)
    assert len(biggest) > MAX_IMAGE_BYTES // 2
    response = create(client, board, [encoded(biggest)] * 5)
    assert response.status_code == 201
    assert len(response.get_json()["images"]) == 5


@both_boards
def test_deleting_the_entry_deletes_its_pictures_only(client, config, board):
    doomed = create(client, board, [encoded(RED), encoded(BLUE)]).get_json()
    kept = create(client, board, [encoded(RED)]).get_json()
    assert stored_pictures(config) == 3

    assert client.delete(f"{board}/{doomed['id']}").status_code == 204
    assert stored_pictures(config) == 1
    remaining = client.get(f"{board}/{kept['id']}/images/{kept['images'][0]['id']}")
    assert close_to(center_color(opened(remaining)), (200, 30, 30))


@both_boards
def test_editing_the_text_keeps_the_pictures(client, board):
    entry = create(client, board, [encoded(RED)]).get_json()
    payload = {"title": "新標題", "body": "新內文", "version": entry["version"]}
    updated = client.put(f"{board}/{entry['id']}", json=payload).get_json()
    assert updated["images"] == entry["images"]
