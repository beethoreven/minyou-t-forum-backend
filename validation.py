"""輸入檢查。

字數算的是 Unicode 字元數（一個中文字、一個表情符號都算 1）。web 要用同一種算法
（JavaScript 的 [...text].length），兩邊才不會一個說可以、一個說太長。
"""

from flask import Request

from errors import ApiError

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
