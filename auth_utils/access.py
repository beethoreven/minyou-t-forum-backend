"""這個請求是誰發的、他可以做什麼。

畫面上把按鈕藏起來只是顯示效果，任何人都可以不經過畫面直接呼叫 API。
真正的把關在這裡：每一個要保護的路由，都要先呼叫 require_member 或 require_staff。

檢查分三層，前一層過了才看下一層：

    已登入   憑證查得到、沒過期             不過 → 401
    已審核   users 表裡有他，而且狀態是已審核  不過 → 403（代碼說明是哪一種）
    角色     夠不夠格做這件事               不過 → 403 forbidden

角色有三種，權限一層包一層：管理員 ⊃ 版主 ⊃ 一般使用者。

    一般使用者  看公告、看文章、發文章、回覆、下載文件，改或刪自己寫的東西
    版主        再加上：發公告、改或刪任何人的東西、使用後台管理帳號（管理員的帳號除外）
    管理員      全部都能做，包含指定誰是管理員
"""

from dataclasses import dataclass

from flask import Request

from auth_utils import sessions
from db import users
from errors import ApiError

ROLE_LEVEL = {"member": 1, "moderator": 2, "admin": 3}


@dataclass(frozen=True)
class Account:
    """一位已經登入的人。user 是他在 users 表的資料，還沒註冊就是 None。"""

    email: str
    user: dict | None


def bearer_token(request: Request) -> str | None:
    """從 `Authorization: Bearer <憑證>` 取出憑證。沒帶或格式不對回傳 None。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header.removeprefix("Bearer ").strip() or None


def current_account(conn, request: Request, idle_days: int) -> Account:
    """第一層：已登入。回傳這個請求的帳號，沒登入就是 401。"""
    token = bearer_token(request)
    email = sessions.resolve(conn, token, idle_days) if token else None
    if email is None:
        raise ApiError(401, "not_signed_in", "請先登入")
    return Account(email=email, user=users.get_by_email(conn, email))


def require_member(conn, request: Request, idle_days: int) -> dict:
    """第二層：已審核。回傳這位成員的資料。

    三種不通過的情況都回 403，用代碼分開，畫面才知道該顯示註冊表單、審核中，還是已停用。
    """
    user = current_account(conn, request, idle_days).user
    if user is None:
        raise ApiError(403, "unregistered", "還沒有完成註冊")
    if user["status"] == "pending":
        raise ApiError(403, "pending", "帳號還在審核中")
    if user["status"] != "approved":
        raise ApiError(403, "disabled", "帳號已經停用")
    return user


def require_staff(conn, request: Request, idle_days: int) -> dict:
    """第三層：版主或管理員。"""
    user = require_member(conn, request, idle_days)
    if not is_staff(user):
        raise ApiError(403, "forbidden", "權限不足")
    return user


def is_staff(user: dict) -> bool:
    return ROLE_LEVEL[user["role"]] >= ROLE_LEVEL["moderator"]


def require_owner_or_staff(user: dict, author_id: int) -> None:
    """改或刪一篇東西：要是作者本人，或是版主、管理員。"""
    if user["id"] != author_id and not is_staff(user):
        raise ApiError(403, "forbidden", "只有作者本人、版主或管理員可以這麼做")


def check_can_manage(actor: dict, target: dict, new_role: str, new_status: str) -> None:
    """後台改一個帳號之前的檢查。actor 是操作的人，target 是被改的帳號。"""
    role_changed = new_role != target["role"]
    status_changed = new_status != target["status"]

    # 不能改自己的角色與狀態：唯一的管理員如果把自己降級或停用，就沒有人能改回來了。
    if actor["id"] == target["id"] and (role_changed or status_changed):
        raise ApiError(403, "cannot_change_self", "不能改自己的角色或狀態")

    # 版主不能動管理員：不能改管理員的帳號，也不能把別人變成管理員。
    if actor["role"] != "admin":
        if target["role"] == "admin":
            raise ApiError(403, "cannot_change_admin", "版主不能修改管理員的帳號")
        if new_role == "admin":
            raise ApiError(403, "cannot_grant_admin", "只有管理員可以指定管理員")

    if status_changed and new_status == "pending":
        raise ApiError(400, "status_invalid", "不能把帳號改回審核中")
    if new_role != "member" and new_status != "approved":
        raise ApiError(400, "role_requires_approved", "版主與管理員必須是已審核的帳號")
