"""驗證 Google 登入給的 ID Token。

ID Token 是 Google 簽發的一段身分證明：「這個人剛剛用這個 Google 帳號登入了這個網站」。
它只在登入那一刻用一次，換成論壇自己的登入憑證（sessions.py）之後就不再使用。
它的效期只有一小時左右，拿來當長期的憑證，使用者會每小時被登出一次。
"""

from collections.abc import Callable
from dataclasses import dataclass

import cachecontrol
import requests
from google.auth import exceptions as google_exceptions
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

# 下載 Google 公開金鑰最多等幾秒。套件的預設是 120 秒，等那麼久只會讓登入看起來像當掉。
FETCH_TIMEOUT_SECONDS = 10
# 這台機器的時鐘比 Google 慢一點點時，剛簽發的憑證會被當成「還沒生效」。容許幾秒的誤差。
CLOCK_SKEW_SECONDS = 10


@dataclass(frozen=True)
class GoogleIdentity:
    email: str  # 一律小寫
    name: str  # Google 帳號上的名稱，可能是空字串


class VerificationUnavailable(Exception):
    """連不上 Google，沒辦法驗證。這不代表憑證是假的，所以不能把人登出。"""


def make_verifier(client_id: str) -> Callable[[str], GoogleIdentity | None]:
    """回傳一個函式：給它 ID Token，驗證通過回傳 GoogleIdentity，憑證無效回傳 None。"""
    # 驗證簽章要用 Google 的公開金鑰。google-auth 每次驗證都會重新下載一次；
    # 包一層 CacheControl，它會照 Google 回的 Cache-Control 標頭把金鑰留著重複使用。
    transport = google_requests.Request(session=cachecontrol.CacheControl(requests.Session()))

    def fetch(*args, **kwargs):
        kwargs["timeout"] = FETCH_TIMEOUT_SECONDS
        return transport(*args, **kwargs)

    def verify(token: str) -> GoogleIdentity | None:
        try:
            # 這一句檢查三件事：簽章真的是 Google 的、沒有過期、是發給這個網站的（client_id）。
            payload = id_token.verify_oauth2_token(
                token, fetch, client_id, clock_skew_in_seconds=CLOCK_SKEW_SECONDS
            )
        except google_exceptions.TransportError as error:
            raise VerificationUnavailable() from error
        except (ValueError, google_exceptions.GoogleAuthError):
            return None
        # email 沒有經過 Google 驗證的帳號不能信任：那個信箱不一定是他的。
        if not payload.get("email_verified") or not payload.get("email"):
            return None
        return GoogleIdentity(
            email=payload["email"].strip().lower(),
            name=(payload.get("name") or "").strip(),
        )

    return verify
