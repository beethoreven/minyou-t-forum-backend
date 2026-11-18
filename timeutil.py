"""時間的格式。資料庫存 UTC，API 也回 UTC，轉成台灣時間是 web 顯示時的事。"""

from datetime import datetime, timezone


def iso_utc(moment: datetime) -> str:
    """轉成 API 用的格式：UTC 的 ISO 8601，例如 2026-11-18T06:30:00Z。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
