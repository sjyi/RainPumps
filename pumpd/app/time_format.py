"""Format datetimes in the configured local timezone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_local(
    dt: datetime | None,
    timezone: str,
    fmt: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    if dt is None:
        return "—"
    return ensure_utc(dt).astimezone(ZoneInfo(timezone)).strftime(fmt)
