"""設定的唯一讀取點。

其他模組不直接讀環境變數，一律用這裡回傳的 Config。必填的設定沒設就直接報錯、不啟動：
帶著缺的設定跑起來，問題會拖到某一個請求才出現，更難找。
"""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 5180
DEFAULT_SESSION_IDLE_DAYS = 90

# Google Drive 的資料夾與檔案代號只會有這些字元。
DRIVE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{10,100}")


class ConfigError(RuntimeError):
    """設定有問題。訊息只說是哪一個設定，不印出它的值。"""


@dataclass(frozen=True)
class Config:
    database_url: str = field(repr=False)
    allowed_origins: tuple[str, ...]
    google_client_id: str
    first_admin_email: str
    # 社區文檔的來源，兩種擇一：本機資料夾，或 Google Drive 的資料夾。
    documents_dir: Path | None = None
    service_account_info: dict | None = field(default=None, repr=False)
    drive_folder_id: str | None = None
    session_idle_days: int = DEFAULT_SESSION_IDLE_DAYS
    port: int = DEFAULT_PORT


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """讀設定。平常從環境變數讀（本機是 .env）；env 參數只給測試用。"""
    if env is None:
        load_dotenv(PROJECT_DIR / ".env")
        env = os.environ

    return Config(
        database_url=_required(env, "DATABASE_URL"),
        allowed_origins=_origins(_required(env, "ALLOWED_ORIGINS")),
        google_client_id=_required(env, "GOOGLE_CLIENT_ID"),
        first_admin_email=_email(_required(env, "FIRST_ADMIN_EMAIL")),
        session_idle_days=_number(env, "SESSION_IDLE_DAYS", DEFAULT_SESSION_IDLE_DAYS, 3650),
        port=_number(env, "PORT", DEFAULT_PORT, 65535),
        **_documents_source(env),
    )


def _value(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    # 有些主機的設定頁會把引號當成值的一部分存起來。帶著引號的連線字串或代號一定對不上，
    # 而且之後的錯誤訊息常常把整個值（包含密碼）印出來，所以在這裡就擋下來。
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        raise ConfigError(f"{key} 的值被引號包住了，請把前後的引號拿掉。")
    return value


def _required(env: Mapping[str, str], key: str) -> str:
    value = _value(env, key)
    if not value:
        raise ConfigError(f"缺少必填的設定 {key}，請參考 .env.example。")
    return value


def _origins(raw: str) -> tuple[str, ...]:
    origins = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not origins:
        raise ConfigError("ALLOWED_ORIGINS 至少要有一個來源。")
    for origin in origins:
        scheme, _, rest = origin.partition("://")
        # 瀏覽器送來的 Origin 沒有結尾的斜線也沒有路徑。多寫的話永遠對不上，
        # 結果是每個請求都被擋，卻沒有任何錯誤訊息，所以在這裡就擋下來。
        if scheme not in ("http", "https") or not rest or "/" in rest:
            raise ConfigError(
                "ALLOWED_ORIGINS 的每一項都要是 http:// 或 https:// 開頭的來源，"
                "結尾不要有斜線，也不要帶路徑。"
            )
    return origins


def _email(raw: str) -> str:
    # 一律用小寫比對：Google 回傳的 email 大小寫不一定跟這裡填的一樣。
    email = raw.lower()
    if email.count("@") != 1 or email.startswith("@") or email.endswith("@") or " " in email:
        raise ConfigError("FIRST_ADMIN_EMAIL 要是一個 email。")
    return email


def _number(env: Mapping[str, str], key: str, default: int, maximum: int) -> int:
    raw = _value(env, key)
    if not raw:
        return default
    is_number = raw.isascii() and raw.isdigit() and len(raw) <= len(str(maximum))
    if not is_number or not 1 <= int(raw) <= maximum:
        raise ConfigError(f"{key} 要是 1 到 {maximum} 之間的數字。")
    return int(raw)


def _documents_source(env: Mapping[str, str]) -> dict:
    """社區文檔從哪裡來。本機資料夾與 Google Drive 只能設定一種。"""
    documents_dir = _value(env, "DOCUMENTS_DIR")
    service_account = _value(env, "SERVICE_ACCOUNT_JSON")
    folder_id = _value(env, "DRIVE_FOLDER_ID")

    if documents_dir and (service_account or folder_id):
        raise ConfigError(
            "文件的來源只能設定一種：DOCUMENTS_DIR（本機資料夾），"
            "或 SERVICE_ACCOUNT_JSON 加 DRIVE_FOLDER_ID（Google Drive）。"
        )
    if documents_dir:
        path = Path(documents_dir).expanduser()
        # 相對路徑一律從這個專案的資料夾算起，不看啟動時人在哪個資料夾。
        return {"documents_dir": path if path.is_absolute() else PROJECT_DIR / path}
    if not service_account and not folder_id:
        raise ConfigError(
            "還沒設定文件的來源：本機用 DOCUMENTS_DIR；"
            "正式環境用 SERVICE_ACCOUNT_JSON 加 DRIVE_FOLDER_ID。"
        )
    if not service_account:
        raise ConfigError("缺少必填的設定 SERVICE_ACCOUNT_JSON，請參考 .env.example。")
    if not folder_id:
        raise ConfigError("缺少必填的設定 DRIVE_FOLDER_ID，請參考 .env.example。")

    try:
        info = json.loads(service_account)
    except ValueError:
        info = None
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise ConfigError("SERVICE_ACCOUNT_JSON 要是服務帳號金鑰檔的完整內容（一段 JSON）。")
    if not DRIVE_ID_PATTERN.fullmatch(folder_id):
        raise ConfigError("DRIVE_FOLDER_ID 要是資料夾網址最後那一段代號，不是整個網址。")
    return {"service_account_info": info, "drive_folder_id": folder_id}
