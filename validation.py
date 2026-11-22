"""輸入檢查。

字數算的是 Unicode 字元數（一個中文字、一個表情符號都算 1）。web 要用同一種算法
（JavaScript 的 [...text].length），兩邊才不會一個說可以、一個說太長。
"""

import base64
import binascii
import unicodedata

from flask import Request

from errors import ApiError
from imaging import ImageRejected, clean_jpeg

# 一則公告、一篇文章最多幾張附圖、一張最大多少。web 會先把圖片縮到 480P 再送，通常只有
# 幾十 KB；這裡的上限是用來擋不照規矩來的請求。
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 300 * 1024
JPEG_SIGNATURE = b"\xff\xd8\xff"

# 上傳的文檔，檔名最多幾個字。
FILE_NAME_MAX = 100

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
    """讀公告或文章的附圖：[{"data": "<base64 編碼的 JPEG>"}, …]。沒有附圖回傳空的清單。

    回傳的是重新存過一次的 JPEG（見 imaging.py），不是瀏覽器送來的原始內容。
    """
    raw = data.get("images")
    if raw is None:
        return []
    invalid = ApiError(400, "images_invalid", "附圖的格式不正確")
    if not isinstance(raw, list):
        raise invalid
    if len(raw) > MAX_IMAGES:
        raise ApiError(400, "too_many_images", f"最多只能附 {MAX_IMAGES} 張圖")

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
        try:
            images.append(clean_jpeg(content, MAX_IMAGE_BYTES))
        except ImageRejected as rejected:
            raise ApiError(400, rejected.code, rejected.message) from None
    return images


def version_field(data: dict) -> int | None:
    """讀編輯時帶來的版本號（打開編輯頁時看到的那一版）。沒帶回傳 None，格式不對就是 400。"""
    value = data.get("version")
    if value is None:
        return None
    # bool 也是 int 的一種，要另外擋掉。
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ID:
        raise ApiError(400, "version_invalid", "版本號的格式不正確")
    return value


MEMBER_FIELDS = ("display_name", "unit_no", "role", "status")


def expected_member(data: dict) -> dict | None:
    """讀後台改帳號時帶來的「改之前的樣子」：畫面上載入時的名稱、戶號、角色、狀態。

    沒帶回傳 None（舊版的畫面）；帶了但格式不對是 400。
    """
    value = data.get("expected")
    if value is None:
        return None
    is_valid = isinstance(value, dict) and all(isinstance(value.get(key), str) for key in MEMBER_FIELDS)
    if not is_valid:
        raise ApiError(400, "expected_invalid", "改之前的資料格式不正確")
    return {key: value[key] for key in MEMBER_FIELDS}


def uploaded_file(request: Request, max_bytes: int) -> tuple[str, bytes]:
    """讀上傳的檔案，回傳（檔名, 內容）。

    檔案要用 multipart/form-data 送，欄位名稱是 file：這是瀏覽器上傳檔案的標準格式。
    """
    file = request.files.get("file")
    if file is None or not file.filename:
        raise ApiError(400, "file_required", "請選擇要上傳的檔案")
    name = _file_name(file.filename)
    # 多讀一個位元組，就分得出「剛好到上限」與「超過上限」，又不用把太大的檔案整個讀進來。
    content = file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ApiError(413, "file_too_large", f"檔案太大，最多 {max_bytes // (1024 * 1024)} MB")
    if not content:
        raise ApiError(400, "file_empty", "這個檔案是空的")
    return name, content


def _file_name(raw: str) -> str:
    """把瀏覽器送來的檔名整理成可以放心使用的檔名：不帶路徑、不是隱藏檔、沒有控制字元。

    不用 werkzeug 的 secure_filename：它只留英數字，中文的檔名會整個被拿掉。
    """
    # 有些瀏覽器送來的是完整路徑，只留最後一段。
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # macOS 可能把一個字拆成「字母＋附加符號」兩個字元。統一成合起來的形式，同一個檔名才會是同一個字串。
    name = unicodedata.normalize("NFC", name).strip()
    # 「.」開頭的是隱藏檔，不會出現在文件清單上；「..」也在這裡一起擋掉。
    has_control = any(ord(char) < 32 or ord(char) == 127 for char in name)
    if not name or name.startswith(".") or has_control:
        raise ApiError(400, "file_name_invalid", "這個檔名不能用，請改個檔名再上傳")
    if len(name) > FILE_NAME_MAX:
        raise ApiError(400, "file_name_too_long", f"檔名太長，最多 {FILE_NAME_MAX} 字")
    return name


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
