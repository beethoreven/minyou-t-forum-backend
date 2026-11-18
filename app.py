"""老爺華廈論壇的 API。

每個要保護的路由，第一件事都是確認「這個請求是誰發的、他能不能做這件事」：
member(conn) 要求已審核的成員，staff(conn) 要求版主或管理員。規則在 auth_utils/access.py。
"""

import sys
from collections.abc import Callable
from urllib.parse import quote

import psycopg
from flask import Flask, Response, request
from flask_cors import CORS
from werkzeug.routing import BaseConverter

import validation
from auth_utils import access, guard, sessions
from auth_utils.google_auth import GoogleIdentity, VerificationUnavailable, make_verifier
from config import Config, ConfigError, load_config
from db import announcements, pictures, posts, users
from db.connection import connect
from db.schema import ensure_schema
from documents.base import DocumentFile, DocumentsUnavailable
from documents.google_drive import GoogleDriveFolder
from documents.local_folder import LocalFolder
from errors import ApiError, register_error_handlers
from timeutil import iso_utc

# 內文上限是 10,000 字，中文一個字 3 個位元組，再加上 JSON 的格式，64 KB 夠用。
MAX_REQUEST_BYTES = 64 * 1024
# 新增公告與文章時可以附圖（最多 5 張、每張最多 300 KB，編碼後會再大三分之一），
# 所以這兩個路由放寬到 3 MB。
MAX_ENTRY_BYTES = 3 * 1024 * 1024
# 後台上傳的文檔，一個檔案最大多少。
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
# 上傳檔案的請求除了檔案本身，還有表單的分隔字串與檔名，所以整個請求的上限要多留一點。
UPLOAD_OVERHEAD_BYTES = 64 * 1024


class IdConverter(BaseConverter):
    """網址裡的資料編號：不是 0 開頭、最多 19 位數的正整數。

    其他寫法（0、負數、長到不合理的數字）在比對網址時就不符合，直接回 404，不會進到路由。
    """

    regex = r"[1-9][0-9]{0,18}"

    def to_python(self, value: str) -> int:
        return int(value)


def create_app(
    config: Config | None = None,
    verify_google_token: Callable[[str], GoogleIdentity | None] | None = None,
) -> Flask:
    """建立 app。verify_google_token 只給測試用，用來換掉真的 Google 驗證。"""
    config = config or load_config()
    verify_google_token = verify_google_token or make_verifier(config.google_client_id)
    documents = _documents(config)

    app = Flask(__name__)
    app.url_map.converters["id"] = IdConverter
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.json.ensure_ascii = False  # 回應裡的中文直接輸出，不轉成 \uXXXX
    CORS(app, origins=list(config.allowed_origins))
    register_error_handlers(app)

    with connect(config.database_url) as conn:
        ensure_schema(conn)

    def db() -> psycopg.Connection:
        return connect(config.database_url)

    # 每個帳號每分鐘能送幾個請求（規則在 auth_utils/guard.py）。
    limiter = guard.RateLimiter(guard.REQUESTS_PER_MINUTE, guard.WINDOW_SECONDS)
    app.extensions["rate_limiter"] = limiter

    def signed_in(conn) -> access.Account:
        """這個請求是誰發的。每個帶登入憑證的請求都會經過這裡，所以順便在這裡算他的請求數。"""
        account = access.current_account(conn, request, config.session_idle_days)
        # 管理員不限：唯一的管理員如果被擋在外面，就沒有人能處理了。
        is_admin = account.user is not None and account.user["role"] == "admin"
        if not is_admin:
            retry_after = limiter.hit(account.email)
            if retry_after is not None:
                raise guard.rate_limited(retry_after)
        return account

    def member(conn) -> dict:
        return access.require_member(signed_in(conn))

    def staff(conn) -> dict:
        return access.require_staff(signed_in(conn))

    # ── 回應的格式 ──────────────────────────────────────────

    def account_json(email: str, user: dict | None) -> dict:
        """登入的人現在的狀態。state：unregistered 還沒註冊、pending 審核中、
        approved 已審核、disabled 已停用。"""
        state = user["status"] if user else "unregistered"
        return {
            "email": email,
            "state": state,
            "authorized": state == "approved",
            "user": user and {
                "id": user["id"],
                "display_name": user["display_name"],
                "unit_no": user["unit_no"],
                "role": user["role"],
            },
        }

    def entry_json(row: dict) -> dict:
        """公告與文章的完整內容，兩者欄位相同。"""
        return {
            "id": row["id"],
            "title": row["title"],
            "body": row["body"],
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "created_at": iso_utc(row["created_at"]),
            "updated_at": iso_utc(row["updated_at"]),
        }

    def reply_json(row: dict) -> dict:
        return {
            "id": row["id"],
            "body": row["body"],
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "created_at": iso_utc(row["created_at"]),
        }

    def images_json(rows: list[dict]) -> list[dict]:
        # 只給編號。圖片本身要另外用 GET …/images/<編號> 拿，一樣要登入。
        return [{"id": row["id"]} for row in rows]

    def announcement_json(conn, row: dict) -> dict:
        images = announcements.list_images(conn, row["id"])
        return entry_json(row) | {"images": images_json(images)}

    def post_json(conn, row: dict) -> dict:
        replies = posts.list_replies(conn, row["id"])
        images = posts.list_images(conn, row["id"])
        return entry_json(row) | {
            "images": images_json(images),
            "replies": [reply_json(reply) for reply in replies],
        }

    def picture_response(content: bytes | None) -> Response:
        if content is None:
            raise ApiError(404, "image_not_found", "找不到這張圖片")
        return Response(
            content,
            mimetype="image/jpeg",
            headers={
                # 一張圖存進去之後就不會再變，瀏覽器可以一直留著，不用每次重新抓。
                "Cache-Control": "private, max-age=31536000, immutable",
                # 一律當成圖片處理，瀏覽器不要自己猜它是不是別種檔案。
                "X-Content-Type-Options": "nosniff",
            },
        )

    def member_json(row: dict) -> dict:
        """後台看到的成員資料。"""
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "unit_no": row["unit_no"],
            "role": row["role"],
            "status": row["status"],
            "created_at": iso_utc(row["created_at"]),
        }

    def document_json(file: DocumentFile) -> dict:
        return {
            "id": file.id,
            "name": file.name,
            "size": file.size,
            "modified_time": iso_utc(file.modified_time),
        }

    def entry_input() -> tuple[str, str]:
        """修改公告或文章時送來的內容：標題與內文。附圖發出去之後就不能換。"""
        data = validation.json_object(request)
        title = validation.text_field(data, "title", "標題", validation.TITLE_MAX)
        body = validation.text_field(data, "body", "內文", validation.BODY_MAX)
        return title, body

    def new_entry_input() -> tuple[str, str, list[bytes]]:
        """新增公告或文章時送來的內容：標題、內文，以及附圖（可以沒有）。"""
        # 要在讀請求的內容之前放寬，放寬的只有這一個請求。
        request.max_content_length = MAX_ENTRY_BYTES
        data = validation.json_object(request)
        title = validation.text_field(data, "title", "標題", validation.TITLE_MAX)
        body = validation.text_field(data, "body", "內文", validation.BODY_MAX)
        return title, body, validation.images_field(data)

    def announcement_not_found() -> ApiError:
        return ApiError(404, "announcement_not_found", "找不到這則公告，可能已經被刪除")

    def post_not_found() -> ApiError:
        return ApiError(404, "post_not_found", "找不到這篇文章，可能已經被刪除")

    def document_not_found() -> ApiError:
        return ApiError(404, "document_not_found", "找不到這份文件，可能已經被移除")

    def documents_unavailable() -> ApiError:
        app.logger.exception("讀不到文件")
        return ApiError(503, "documents_unavailable", "暫時讀不到文件，請稍後再試")

    def documents_not_writable(error: DocumentsUnavailable) -> ApiError:
        """上傳或刪除文檔沒有成功。設定不對的時候，把該檢查什麼告訴後台的人（error.hint）。"""
        app.logger.exception("文檔寫不進去")
        message = error.hint or "暫時連不上存放文檔的地方，請稍後再試"
        return ApiError(503, "documents_unavailable", message)

    def upload_unavailable() -> ApiError:
        return ApiError(503, "upload_unavailable", "還沒有開啟從後台上傳的功能")

    # ── 保活 ────────────────────────────────────────────────

    @app.get("/health")
    def health():
        # 不碰資料庫：只回答「服務活著」，以及現在跑的是哪一版。
        return {"status": "ok", "version": config.version}

    # ── 登入 ────────────────────────────────────────────────

    @app.post("/auth/login")
    def login():
        """用 Google 的 ID Token 換論壇自己的登入憑證。Google 的憑證只在這裡用這一次。

        任何 Google 帳號都能登入：還沒註冊的人要先登入，才能填註冊表單。
        登入不代表能用論壇，能不能用是每個請求另外檢查的。
        """
        id_token = validation.json_object(request).get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ApiError(400, "id_token_required", "缺少 Google 的登入憑證")
        try:
            identity = verify_google_token(id_token)
        except VerificationUnavailable:
            # 連不上 Google 不代表憑證是假的，回 503，畫面會請使用者稍後再試。
            raise ApiError(503, "google_unavailable", "暫時無法向 Google 確認登入，請稍後再試") from None
        if identity is None:
            raise ApiError(401, "invalid_google_token", "Google 登入沒有成功，請再試一次")

        with db() as conn:
            if identity.email == config.first_admin_email:
                users.ensure_first_admin(conn, identity.email, identity.name)
            sessions.delete_idle(conn, config.session_idle_days)
            token = sessions.create(conn, identity.email)
            sessions.trim(conn, identity.email, guard.MAX_SESSIONS_PER_ACCOUNT)
            account = account_json(identity.email, users.get_by_email(conn, identity.email))
        return {"session_token": token, "google_name": identity.name} | account

    @app.post("/auth/logout")
    def logout():
        token = access.bearer_token(request)
        if token:
            with db() as conn:
                sessions.delete(conn, token)
        return "", 204

    @app.get("/auth/status")
    def auth_status():
        """登入的人現在的狀態。憑證有效就回 200（包含還沒註冊、審核中、已停用），
        憑證無效才回 401：「查過了，結果是不能用」是正常的答案，不是錯誤。"""
        with db() as conn:
            account = signed_in(conn)
        return account_json(account.email, account.user)

    @app.post("/auth/register")
    def register():
        """登入之後填註冊資料。註冊完是「審核中」，要等版主或管理員審核。

        email 取自登入憑證，不看請求裡送來的任何 email：身分只認 Google 驗過的那一個。
        """
        with db() as conn:
            account = signed_in(conn)
            data = validation.json_object(request)
            display_name = validation.text_field(data, "display_name", "顯示名稱", validation.NAME_MAX)
            unit_no = validation.text_field(data, "unit_no", "戶號", validation.UNIT_MAX)
            user = users.register(conn, account.email, display_name, unit_no)
            if user is None:
                raise ApiError(409, "already_registered", "這個帳號已經註冊過了")
        return account_json(account.email, user), 201

    # ── 社區公告：成員都能看，版主與管理員才能發、改、刪 ────────

    @app.get("/announcements")
    def list_announcements():
        before = validation.before_param(request.args)
        with db() as conn:
            member(conn)
            rows, has_more = announcements.list_page(conn, before)
        items = [
            {"id": row["id"], "title": row["title"], "created_at": iso_utc(row["created_at"])}
            for row in rows
        ]
        return {"items": items, "has_more": has_more}

    @app.get("/announcements/<id:announcement_id>")
    def get_announcement(announcement_id: int):
        with db() as conn:
            member(conn)
            row = announcements.get(conn, announcement_id)
            if row is None:
                raise announcement_not_found()
            return announcement_json(conn, row)

    @app.post("/announcements")
    def create_announcement():
        with db() as conn:
            author = staff(conn)
            title, body, images = new_entry_input()
            # 公告、圖片、兩者的對應在同一筆交易裡寫入：要嘛全部成功，要嘛什麼都沒有留下。
            new_id = announcements.create(conn, title, body, author["id"])
            announcements.add_images(conn, new_id, pictures.save(conn, images))
            return announcement_json(conn, announcements.get(conn, new_id)), 201

    @app.get("/announcements/<id:announcement_id>/images/<id:image_id>")
    def get_announcement_image(announcement_id: int, image_id: int):
        with db() as conn:
            member(conn)
            file_id = announcements.get_image_file_id(conn, announcement_id, image_id)
            content = pictures.get(conn, file_id) if file_id else None
        return picture_response(content)

    @app.put("/announcements/<id:announcement_id>")
    def update_announcement(announcement_id: int):
        with db() as conn:
            staff(conn)
            title, body = entry_input()
            if not announcements.update(conn, announcement_id, title, body):
                raise announcement_not_found()
            return announcement_json(conn, announcements.get(conn, announcement_id))

    @app.delete("/announcements/<id:announcement_id>")
    def delete_announcement(announcement_id: int):
        with db() as conn:
            staff(conn)
            images = announcements.list_images(conn, announcement_id)
            if not announcements.delete(conn, announcement_id):
                raise announcement_not_found()
            # 「哪一張圖屬於這則公告」的紀錄會跟著公告一起刪除，圖片的內容要自己刪。
            pictures.delete(conn, [image["file_id"] for image in images])
        return "", 204

    # ── 社區論壇：成員都能看、發文、回覆；改與刪要是作者本人、版主或管理員 ──

    @app.get("/posts")
    def list_posts():
        before = validation.before_param(request.args)
        with db() as conn:
            member(conn)
            rows, has_more = posts.list_page(conn, before)
        items = [
            {
                "id": row["id"],
                "title": row["title"],
                "author_name": row["author_name"],
                "created_at": iso_utc(row["created_at"]),
                "reply_count": row["reply_count"],
            }
            for row in rows
        ]
        return {"items": items, "has_more": has_more}

    @app.get("/posts/<id:post_id>")
    def get_post(post_id: int):
        with db() as conn:
            member(conn)
            row = posts.get(conn, post_id)
            if row is None:
                raise post_not_found()
            return post_json(conn, row)

    @app.post("/posts")
    def create_post():
        with db() as conn:
            author = member(conn)
            if author["role"] != "admin":
                guard.check_write_limits(
                    lambda seconds: posts.count_posts_since(conn, author["id"], seconds),
                    guard.POST_LIMITS,
                    "篇文章",
                )
            title, body, images = new_entry_input()
            # 文章、圖片、兩者的對應在同一筆交易裡寫入：要嘛全部成功，要嘛什麼都沒有留下。
            new_id = posts.create(conn, title, body, author["id"])
            posts.add_images(conn, new_id, pictures.save(conn, images))
            return post_json(conn, posts.get(conn, new_id)), 201

    @app.get("/posts/<id:post_id>/images/<id:image_id>")
    def get_post_image(post_id: int, image_id: int):
        with db() as conn:
            member(conn)
            file_id = posts.get_image_file_id(conn, post_id, image_id)
            content = pictures.get(conn, file_id) if file_id else None
        return picture_response(content)

    @app.put("/posts/<id:post_id>")
    def update_post(post_id: int):
        with db() as conn:
            user = member(conn)
            title, body = entry_input()
            row = posts.get(conn, post_id)
            if row is None:
                raise post_not_found()
            access.require_owner_or_staff(user, row["author_id"])
            posts.update(conn, post_id, title, body)
            return post_json(conn, posts.get(conn, post_id))

    @app.delete("/posts/<id:post_id>")
    def delete_post(post_id: int):
        with db() as conn:
            user = member(conn)
            row = posts.get(conn, post_id)
            if row is None:
                raise post_not_found()
            access.require_owner_or_staff(user, row["author_id"])
            images = posts.list_images(conn, post_id)
            posts.delete(conn, post_id)
            # 「哪一張圖屬於這篇文章」的紀錄會跟著文章一起刪除，圖片的內容要自己刪。
            pictures.delete(conn, [image["file_id"] for image in images])
        return "", 204

    @app.post("/posts/<id:post_id>/replies")
    def create_reply(post_id: int):
        with db() as conn:
            author = member(conn)
            if author["role"] != "admin":
                guard.check_write_limits(
                    lambda seconds: posts.count_replies_since(conn, author["id"], seconds),
                    guard.REPLY_LIMITS,
                    "則回覆",
                )
            data = validation.json_object(request)
            body = validation.text_field(data, "body", "回覆", validation.REPLY_MAX)
            reply_id = posts.create_reply(conn, post_id, body, author["id"])
            if reply_id is None:
                raise post_not_found()
            row = posts.get_reply(conn, reply_id)
        return reply_json(row), 201

    @app.delete("/replies/<id:reply_id>")
    def delete_reply(reply_id: int):
        with db() as conn:
            user = member(conn)
            row = posts.get_reply(conn, reply_id)
            if row is None:
                raise ApiError(404, "reply_not_found", "找不到這則回覆，可能已經被刪除")
            access.require_owner_or_staff(user, row["author_id"])
            posts.delete_reply(conn, reply_id)
        return "", 204

    # ── 社區文檔：成員都能看、下載 ──────────────────────────

    @app.get("/documents")
    def list_documents():
        with db() as conn:
            member(conn)
        try:
            files = documents.list_files()
        except DocumentsUnavailable:
            raise documents_unavailable() from None
        return {"items": [document_json(file) for file in files]}

    @app.get("/documents/<file_id>/download")
    def download_document(file_id: str):
        with db() as conn:
            member(conn)
        try:
            opened = documents.open_file(file_id)
        except DocumentsUnavailable:
            raise documents_unavailable() from None
        if opened is None:
            raise document_not_found()
        file, chunks = opened
        # 檔名可能有中文。用 filename*=UTF-8'' 的寫法，而且整個檔名都編碼過，
        # 檔名裡就算有換行或引號，也不會跑出這個標頭。
        disposition = f"attachment; filename*=UTF-8''{quote(file.name, safe='')}"
        return Response(
            chunks,
            mimetype="application/octet-stream",
            headers={"Content-Disposition": disposition},
        )

    # ── 後台：版主與管理員用的 ──────────────────────────────

    @app.get("/admin/documents")
    def admin_documents():
        """後台管理文檔前要知道的事：能不能從後台上傳、一個檔案最大多少。

        文檔放 Google Drive、卻還沒設定代為存檔的 Apps Script 時不能上傳（can_upload 是 false）。
        這時後台改成請人直接去 Google Drive 的資料夾放檔案，manage_url 就是那個資料夾的網址。
        """
        with db() as conn:
            staff(conn)
        return {
            "can_upload": documents.can_upload,
            "max_bytes": MAX_DOCUMENT_BYTES,
            "manage_url": documents.manage_url,
        }

    @app.post("/admin/documents")
    def upload_document():
        """上傳一份文檔。已經有同名的檔案時，新的會取代舊的。"""
        # 要在讀請求的內容之前放寬，放寬的只有這一個請求。
        request.max_content_length = MAX_DOCUMENT_BYTES + UPLOAD_OVERHEAD_BYTES
        with db() as conn:
            staff(conn)
        if not documents.can_upload:
            raise upload_unavailable()
        name, content = validation.uploaded_file(request, MAX_DOCUMENT_BYTES)
        # 存檔可能要連到外面的服務、等上好幾秒，所以不在資料庫連線開著的時候做。
        try:
            documents.save(name, content)
        except DocumentsUnavailable as error:
            raise documents_not_writable(error) from None
        return {"name": name}, 201

    @app.delete("/admin/documents/<file_id>")
    def delete_document(file_id: str):
        with db() as conn:
            staff(conn)
        if not documents.can_upload:
            raise upload_unavailable()
        try:
            deleted = documents.delete(file_id)
        except DocumentsUnavailable as error:
            raise documents_not_writable(error) from None
        if not deleted:
            raise document_not_found()
        return "", 204

    @app.get("/admin/users")
    def admin_list_users():
        with db() as conn:
            staff(conn)
            rows = users.list_all(conn)
        return {"items": [member_json(row) for row in rows]}

    @app.put("/admin/users/<id:user_id>")
    def admin_update_user(user_id: int):
        """改一個成員的名稱、戶號、角色、狀態。審核就是把狀態從審核中改成已審核。

        回應裡的 changed 說這次有沒有真的改到東西，畫面用它決定要顯示「已儲存」還是「沒有修改」。
        """
        with db() as conn:
            actor = staff(conn)
            data = validation.json_object(request)
            new = {
                "display_name": validation.text_field(data, "display_name", "顯示名稱", validation.NAME_MAX),
                "unit_no": validation.optional_text_field(data, "unit_no", "戶號", validation.UNIT_MAX),
                "role": validation.choice_field(data, "role", "角色", users.ROLES),
                "status": validation.choice_field(data, "status", "狀態", users.STATUSES),
            }
            target = users.get(conn, user_id)
            if target is None:
                raise ApiError(404, "user_not_found", "找不到這位成員")
            access.check_can_manage(actor, target, new["role"], new["status"])
            changed = any(new[key] != target[key] for key in new)
            if changed:
                users.update(conn, user_id, **new)
            item = users.get(conn, user_id)
        return {"changed": changed, "item": member_json(item)}

    return app


def _documents(config: Config):
    """社區文檔放在哪裡：設定了本機資料夾就用它（本機開發與測試），否則是 Google Drive 的資料夾。"""
    if config.documents_dir is not None:
        config.documents_dir.mkdir(parents=True, exist_ok=True)
        return LocalFolder(config.documents_dir)
    try:
        return GoogleDriveFolder.from_service_account(
            config.service_account_info,
            config.drive_folder_id,
            script_url=config.apps_script_url,
            script_secret=config.apps_script_secret,
        )
    except ValueError:
        raise ConfigError("SERVICE_ACCOUNT_JSON 的金鑰讀不進來，請重新下載金鑰檔再貼一次。") from None


def main() -> None:
    """本機開發用的啟動方式。正式環境用 gunicorn，見 wsgi.py。"""
    try:
        config = load_config()
        app = create_app(config)
    except ConfigError as error:
        sys.exit(f"設定有問題：{error}")
    except psycopg.OperationalError:
        sys.exit("連不上資料庫。本機請先執行 tools/local_db.sh start，並確認 .env 的 DATABASE_URL。")
    # 只聽 127.0.0.1：開發用的伺服器不需要讓同一個網路上的其他裝置連進來。
    app.run(host="127.0.0.1", port=config.port)


if __name__ == "__main__":
    main()
