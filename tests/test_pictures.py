"""公告與文章的附圖。兩邊的規則一樣，所以大部分的測試兩邊各跑一次。"""

import base64

import psycopg
import pytest

# 不是真的圖片，只是檔頭長得像 JPEG 的幾個位元組：後端只檢查檔頭與大小。
JPEG_A = b"\xff\xd8\xff\xe0" + b"first picture"
JPEG_B = b"\xff\xd8\xff\xe0" + b"second picture"

both_boards = pytest.mark.parametrize("board", ["/announcements", "/posts"])


def encoded(content: bytes) -> dict:
    return {"data": base64.b64encode(content).decode("ascii")}


def create(client, board, images):
    return client.post(board, json={"title": "頂樓的花", "body": "開了。", "images": images})


def stored_pictures(config) -> int:
    """資料庫裡現在存了幾張圖。"""
    with psycopg.connect(config.database_url) as conn:
        return conn.execute("SELECT count(*) FROM stored_pictures").fetchone()[0]


@both_boards
def test_entry_with_pictures(client, config, board):
    response = create(client, board, [encoded(JPEG_A), encoded(JPEG_B)])
    assert response.status_code == 201
    entry = response.get_json()
    assert len(entry["images"]) == 2
    assert client.get(f"{board}/{entry['id']}").get_json() == entry

    first, second = (client.get(f"{board}/{entry['id']}/images/{image['id']}") for image in entry["images"])
    assert first.status_code == 200
    assert first.mimetype == "image/jpeg"
    assert first.data == JPEG_A  # 照上傳的順序
    assert second.data == JPEG_B
    assert stored_pictures(config) == 2


@both_boards
def test_entry_without_pictures_still_works(client, board):
    entry = client.post(board, json={"title": "標題", "body": "內文"}).get_json()
    assert entry["images"] == []


@both_boards
def test_a_picture_belongs_to_its_own_entry_only(client, board):
    with_picture = create(client, board, [encoded(JPEG_A)]).get_json()
    other = create(client, board, []).get_json()
    image_id = with_picture["images"][0]["id"]

    wrong_entry = client.get(f"{board}/{other['id']}/images/{image_id}")
    assert wrong_entry.status_code == 404
    assert wrong_entry.get_json()["code"] == "image_not_found"
    assert client.get(f"{board}/{with_picture['id']}/images/99999").status_code == 404


def test_announcement_pictures_and_post_pictures_are_kept_apart(client):
    announcement = create(client, "/announcements", [encoded(JPEG_A)]).get_json()
    post = create(client, "/posts", [encoded(JPEG_B)]).get_json()
    assert announcement["id"] == post["id"] and announcement["images"] == post["images"]  # 兩邊的編號剛好一樣

    assert client.get(f"/announcements/{announcement['id']}/images/{announcement['images'][0]['id']}").data == JPEG_A
    assert client.get(f"/posts/{post['id']}/images/{post['images'][0]['id']}").data == JPEG_B


@both_boards
def test_pictures_need_a_session(client, anonymous, board):
    entry = create(client, board, [encoded(JPEG_A)]).get_json()
    response = anonymous.get(f"{board}/{entry['id']}/images/{entry['images'][0]['id']}")
    assert response.status_code == 401


@both_boards
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
def test_rejects_bad_pictures(client, config, board, images, code):
    response = create(client, board, images)
    assert response.status_code == 400
    assert response.get_json()["code"] == code
    assert client.get(board).get_json()["items"] == []  # 公告或文章沒有建立
    assert stored_pictures(config) == 0  # 圖片也沒有存


@both_boards
def test_good_pictures_are_not_kept_when_the_text_is_rejected(client, config, board):
    response = client.post(board, json={"title": "", "body": "內文", "images": [encoded(JPEG_A)]})
    assert response.status_code == 400
    assert stored_pictures(config) == 0


def test_a_member_cannot_post_an_announcement_with_pictures(add_member, config):
    resident = add_member("resident@example.com")
    response = create(resident, "/announcements", [encoded(JPEG_A)])
    assert response.status_code == 403
    assert stored_pictures(config) == 0


@both_boards
def test_five_pictures_at_the_size_limit_fit_in_one_request(client, board):
    biggest = b"\xff\xd8\xff" + b"x" * (300 * 1024 - 3)
    response = create(client, board, [encoded(biggest)] * 5)
    assert response.status_code == 201
    assert len(response.get_json()["images"]) == 5


@both_boards
def test_deleting_the_entry_deletes_its_pictures_only(client, config, board):
    doomed = create(client, board, [encoded(JPEG_A), encoded(JPEG_B)]).get_json()
    kept = create(client, board, [encoded(JPEG_A)]).get_json()
    assert stored_pictures(config) == 3

    assert client.delete(f"{board}/{doomed['id']}").status_code == 204
    assert stored_pictures(config) == 1
    assert client.get(f"{board}/{kept['id']}/images/{kept['images'][0]['id']}").data == JPEG_A


@both_boards
def test_editing_the_text_keeps_the_pictures(client, board):
    entry = create(client, board, [encoded(JPEG_A)]).get_json()
    updated = client.put(f"{board}/{entry['id']}", json={"title": "新標題", "body": "新內文"}).get_json()
    assert updated["images"] == entry["images"]
