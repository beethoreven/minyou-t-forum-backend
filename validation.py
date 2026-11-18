"""輸入檢查。

字數算的是 Unicode 字元數（一個中文字、一個表情符號都算 1）。web 要用同一種算法
（JavaScript 的 [...text].length），兩邊才不會一個說可以、一個說太長。
"""

import base64
import binascii

from flask import Request

from errors import ApiError

# 一篇文章最多幾張附圖、一張最大多少。web 會先把圖片縮到 480P 再送，通常只有幾十 KB；
# 這裡的上限是用來擋不照規矩來的請求。
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 300 * 1024
JPEG_SIGNATURE = b"\xff\xd8\xff"

TITLE_MAX = 100
BODY_MAX = 10_000
REPLY_MAX = 2_000
NAME_MAX = 30
UNIT_MAX = 30

# PostgreSQL bigint 的上限，也是 19 位數。
MAX_ID = 2**63 - 1
MAX_ID_DIGITS = 19


def json_object(request: Request) -> dict:
    """讀請求裡的 JSON。不是 JSON、或不是物件（例如是陣列）都算錯。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "請求內容必須是 JSON 物件")
    return data


def text_field(data: dict, key: str, label: str, max_length: int) -> str:
    """讀一個必填的文字欄位：去掉頭尾空白，不能是空的，不能超過字數。"""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, f"{key}_required", f"請填寫{label}")
    return _clean(value, key, label, max_length)


def optional_text_field(data: dict, key: str, label: str, max_length: int) -> str:
    """讀一個可以不填的文字欄位。沒填回傳空字串。"""
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"{key}_invalid", f"{label}必須是文字")
    return _clean(value, key, label, max_length)


def choice_field(data: dict, key: str, label: str, choices: tuple[str, ...]) -> str:
    """讀一個只能從幾個值裡面選的欄位。"""
    value = data.get(key)
    if not isinstance(value, str) or value not in choices:
        raise ApiError(400, f"{key}_invalid", f"{label}不是可以選的值")
    return value


def images_field(data: dict) -> list[bytes]:
    """讀文章的附圖：[{"data": "<base64 編碼的 JPEG>"}, …]。沒有附圖回傳空的清單。"""
    raw = data.get("images")
    if raw is None:
        return []
    invalid = ApiError(400, "images_invalid", "附圖的格式不正確")
    if not isinstance(raw, list):
        raise invalid
    if len(raw) > MAX_IMAGES:
        raise ApiError(400, "too_many_images", f"一篇文章最多 {MAX_IMAGES} 張圖")

    images = []
    for item in raw:
        encoded = item.get("data") if isinstance(item, dict) else None
        if not isinstance(encoded, str):
            raise invalid
        # base64 編碼後大約是原本的 4/3。先用長度擋掉太大的，不用真的解開。
        if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 4:
            raise ApiError(400, "image_too_large", "圖片太大")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise invalid from None
        if len(content) > MAX_IMAGE_BYTES:
            raise ApiError(400, "image_too_large", "圖片太大")
        # 只收 JPEG。檔頭對不上的一律不收，免得有人把別種檔案當成圖片塞進來。
        if not content.startswith(JPEG_SIGNATURE):
            raise ApiError(400, "image_not_jpeg", "附圖必須是 JPEG 圖片")
        images.append(content)
    return images


def before_param(args) -> int | None:
    """列表的 before 參數：只要 id 比它小的資料。沒帶就是從最新的開始。"""
    raw = args.get("before")
    if raw is None:
        return None
    is_number = raw.isascii() and raw.isdigit() and len(raw) <= MAX_ID_DIGITS
    if not is_number or not 1 <= int(raw) <= MAX_ID:
        raise ApiError(400, "before_invalid", "before 參數要是正整數")
    return int(raw)


def _clean(value: str, key: str, label: str, max_length: int) -> str:
    value = value.strip()
    if "\x00" in value:
        # PostgreSQL 的 text 存不了 NUL 字元。先擋下來，不要讓它變成資料庫錯誤。
        raise ApiError(400, f"{key}_invalid", f"{label}含有不能使用的字元")
    if len(value) > max_length:
        raise ApiError(400, f"{key}_too_long", f"{label}最多 {max_length} 字")
    return value
