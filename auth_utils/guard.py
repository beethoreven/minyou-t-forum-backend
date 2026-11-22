"""防濫用：不讓同一個人狂打 API、狂發文章。

限制都是「每個帳號」各算各的。全站一起算的話，一個人亂來會害所有人一起被擋。

一、請求的頻率：每個帳號每分鐘最多 REQUESTS_PER_MINUTE 個請求（RateLimiter）。
    記在記憶體裡，不進資料庫：每個請求都會經過這裡，為了省資源而每次寫一筆資料，
    反而花更多資源。服務重啟時計數會歸零，那只是重新開始算一分鐘，不是漏洞。
    因為記在記憶體，服務只能跑一個行程（gunicorn --workers 1）。

二、寫入的數量：每個帳號一段時間內能發幾篇文章、幾則回覆、幾則公告，能上傳幾個、多少文檔
    （check_write_limits、check_total_bytes）。直接數資料庫裡他最近寫了多少，不另外記帳，
    所以服務重啟也不會被重設。數之前先拿這個人的鎖（lock_writer），同一個人同時送出好幾個
    請求時一個一個來，才不會大家都數到「還沒超過」就一起寫進去。

三、登入憑證的數量：一個帳號最多留 MAX_SESSIONS_PER_ACCOUNT 張，登入時把最舊的清掉。
    不然同一個帳號一直重複登入，可以把 sessions 表塞滿。

四、登入的次數：還沒登入的人也打得到 /auth/login，所以另外限制：同一個來源每分鐘幾次、
    同一個 Google 帳號一段時間內幾次（client_ip 說明來源怎麼算）。

管理員不受一、二限制：唯一的管理員如果被系統擋在外面，就沒有人能處理了。

被限制只是暫時不能操作，回 429，並說明多久之後可以再試；不會因此被停用。
要停用一個帳號，由版主或管理員在後台做。
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from errors import ApiError

# 正常使用離這個數字很遠：看一篇有五張圖的文章是 6 個請求，換一個頁籤是 1 個。
REQUESTS_PER_MINUTE = 120
WINDOW_SECONDS = 60

MAX_SESSIONS_PER_ACCOUNT = 10


@dataclass(frozen=True)
class WriteLimit:
    seconds: int  # 多長的一段時間
    maximum: int  # 這段時間內最多幾篇
    label: str  # 給人看的說法，例如「10 分鐘」


POST_LIMITS = (WriteLimit(600, 5, "10 分鐘"), WriteLimit(86400, 30, "一天"))
REPLY_LIMITS = (WriteLimit(600, 20, "10 分鐘"), WriteLimit(86400, 200, "一天"))
# 公告只有版主與管理員能發，正常一天發不了幾則。一則公告最多 5 張圖，大約 1.5 MB。
ANNOUNCEMENT_LIMITS = (WriteLimit(600, 5, "10 分鐘"), WriteLimit(86400, 20, "一天"))
# 上傳的文檔一個最多 10 MB，放在存放文檔的那個帳號的雲端硬碟（空間跟他的信箱共用）。
UPLOAD_LIMITS = (WriteLimit(600, 10, "10 分鐘"), WriteLimit(86400, 50, "一天"))
UPLOAD_BYTES_PER_DAY = 200 * 1024 * 1024

# 登入：同一個來源每分鐘幾次（還沒驗證 Google 的憑證之前就算）、同一個 Google 帳號
# 10 分鐘內幾次（驗證過之後才算）。正常的人幾個月才登入一次。
LOGIN_ATTEMPTS_PER_MINUTE = 30
LOGINS_PER_ACCOUNT = 10
LOGIN_ACCOUNT_WINDOW_SECONDS = 600

# 同一個人的寫入一個一個來用的鎖。跟建表用的鎖（db/schema.py）分開。
WRITER_LOCK_PREFIX = "minyou-t-forum:writer:"


class RateLimiter:
    """每個 key（這裡是帳號的 email）在一段時間內最多幾次。"""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        # 服務用好幾條執行緒同時處理請求，改這份紀錄時要一個一個來。
        self._lock = threading.Lock()

    def hit(self, key: str) -> int | None:
        """記一次請求。還在額度內回傳 None；超過了回傳幾秒後可以再試，而且這一次不算數。"""
        now = time.monotonic()
        with self._lock:
            times = self._hits.setdefault(key, deque())
            while times and now - times[0] > self.window_seconds:
                times.popleft()
            if len(times) >= self.limit:
                # 最舊的那一次離開這段時間之後，就又有額度了。
                return max(1, int(self.window_seconds - (now - times[0])) + 1)
            times.append(now)
            self._forget_idle_keys(now)
            return None

    def _forget_idle_keys(self, now: float) -> None:
        """把已經沒有紀錄的帳號清掉，這份紀錄才不會隨著時間越長越大。"""
        if len(self._hits) < 1000:
            return
        for key in [key for key, times in self._hits.items() if not times or now - times[-1] > self.window_seconds]:
            del self._hits[key]


def rate_limited(retry_after: int) -> ApiError:
    return ApiError(
        429,
        "rate_limited",
        f"操作太頻繁，請 {retry_after} 秒後再試",
        headers={"Retry-After": str(retry_after)},
    )


def client_ip(request) -> str:
    """發出這個請求的來源位址，限制登入次數用。

    服務放在代理伺服器後面，直接看到的連線位址都是代理的。代理會把它看到的來源接在
    X-Forwarded-For 的最後面；前面的部分是請求自己帶來的，可以亂填，所以只用最後一個。
    如果最後一個其實是更前面一層代理的位址，很多人會被算成同一個來源，所以每分鐘的次數
    抓得比較寬，再加上每個 Google 帳號各自的限制。
    """
    forwarded = [part.strip() for part in request.headers.get("X-Forwarded-For", "").split(",")]
    forwarded = [part for part in forwarded if part]
    return forwarded[-1] if forwarded else (request.remote_addr or "unknown")


def lock_writer(conn, user_id: int) -> None:
    """拿這個人的寫入鎖，到交易結束時自動放開。數「最近寫了幾篇」之前呼叫。"""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{WRITER_LOCK_PREFIX}{user_id}",))


def check_total_bytes(used: int, adding: int, limit: int, label: str) -> None:
    """這一次加上去會不會超過總量。used 是這段時間已經用掉的位元組。"""
    if used + adding > limit:
        raise ApiError(
            429,
            "write_limited",
            f"{label}內最多上傳 {limit // (1024 * 1024)} MB，請晚一點再傳",
        )


def check_write_limits(count_since: Callable[[int], int], limits: tuple[WriteLimit, ...], noun: str) -> None:
    """寫入前檢查有沒有超過數量。count_since(秒數) 回傳這個人在這段時間內已經寫了幾筆。

    要在同一筆交易裡先呼叫 lock_writer，再呼叫這裡，最後才寫入。
    """
    for limit in limits:
        if count_since(limit.seconds) >= limit.maximum:
            raise ApiError(
                429,
                "write_limited",
                f"{limit.label}內最多發 {limit.maximum} {noun}，請晚一點再發",
            )
