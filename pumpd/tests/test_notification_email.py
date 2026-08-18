"""Test notification email delivery."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import AppConfig, NotificationsConfig, SmtpConfig
from app.notify import Notifier
from app.service import PumpService


@pytest.mark.asyncio
async def test_send_admin_email_returns_failure_when_misconfigured() -> None:
    notifier = Notifier(
        AppConfig(
            notifications=NotificationsConfig(
                admin_email="",
                smtp=SmtpConfig(enabled=False),
            )
        )
    )
    ok, detail = await notifier.send_admin_email("test", "body", gmail_client=None)
    assert ok is False
    assert "recipient" in detail or "Google" in detail or "SMTP" in detail


@pytest.mark.asyncio
async def test_send_test_notification_email(monkeypatch: pytest.MonkeyPatch) -> None:
    service = object.__new__(PumpService)
    service.notifier = AsyncMock()
    service.gmail_client = MagicMock(connected=False)
    service.notifier.send_admin_email = AsyncMock(
        return_value=(True, "sent to admin@example.com")
    )

    result = await PumpService.send_test_notification_email(service)

    assert result["success"] is True
    assert "admin@example.com" in result["message"]
