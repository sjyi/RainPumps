"""Tests for local timezone formatting."""

from __future__ import annotations

from datetime import UTC, datetime

from app.time_format import format_local


def test_format_local_converts_utc_to_eastern() -> None:
    dt = datetime(2026, 8, 12, 22, 0, 0, tzinfo=UTC)
    assert format_local(dt, "America/New_York") == "2026-08-12 18:00:00"


def test_format_local_handles_naive_as_utc() -> None:
    dt = datetime(2026, 8, 12, 22, 0, 0)
    assert format_local(dt, "America/New_York", "%H:%M") == "18:00"


def test_format_local_none_returns_dash() -> None:
    assert format_local(None, "America/New_York") == "—"
