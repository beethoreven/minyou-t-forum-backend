"""錯誤的統一格式：{"error": 給人看的訊息, "code": 給程式判斷的代碼}。"""

import psycopg
from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException


class ApiError(Exception):
    """路由主動回報的錯誤，例如找不到資料、輸入不合規定。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


_HTTP_ERRORS = {
    400: ("bad_request", "請求的格式不正確"),
    404: ("not_found", "找不到這個網址"),
    405: ("method_not_allowed", "這個網址不支援這種請求方式"),
    413: ("payload_too_large", "送出的內容太大"),
}


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify(error=error.message, code=error.code), error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        code, message = _HTTP_ERRORS.get(error.code, (f"http_{error.code}", "請求沒有成功"))
        return jsonify(error=message, code=code), error.code

    @app.errorhandler(psycopg.OperationalError)
    def handle_database_down(error: psycopg.OperationalError):
        # 連不上資料庫、連線中斷都是這一類。跟程式寫錯分開回報，使用者才知道是稍後再試就好。
        app.logger.error("資料庫連線失敗：%s", type(error).__name__)
        return jsonify(error="資料庫暫時連不上，請稍後再試", code="database_unavailable"), 503

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        # 細節只寫進伺服器的 log，不回給瀏覽器：錯誤訊息裡可能有資料庫的內容或程式的路徑。
        app.logger.exception("沒有預期到的錯誤")
        return jsonify(error="伺服器發生錯誤，請稍後再試", code="internal_error"), 500
