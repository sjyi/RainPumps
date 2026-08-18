"""Notification delivery via ntfy and/or SMTP."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def send(self, title: str, message: str, priority: str = "default") -> None:
        await self._ntfy(title, message, priority)
        await self._smtp(title, message)

    async def send_admin_email(
        self,
        subject: str,
        message: str,
        *,
        gmail_client: Any | None = None,
    ) -> tuple[bool, str]:
        """Email the configured administrator via Google Gmail or SMTP fallback."""
        admin = (self.config.notifications.admin_email or "").strip()
        smtp = self.config.notifications.smtp
        recipients = [admin] if admin else list(smtp.to_addrs)
        if not recipients:
            return False, "no recipient configured (set administrator email)"

        if gmail_client is not None and getattr(gmail_client, "connected", False):
            return await gmail_client.send_email(
                to=recipients[0],
                subject=subject,
                body=message,
            )

        if not smtp.enabled or not smtp.host:
            return False, "connect Google account or enable SMTP in Email notifications"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = smtp.from_addr or smtp.username
        msg["To"] = ", ".join(recipients)
        msg.set_content(message)
        try:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as server:
                server.starttls()
                if smtp.username:
                    server.login(smtp.username, smtp.password)
                server.send_message(msg)
        except Exception as exc:
            logger.warning("admin email send failed: %s", exc)
            return False, str(exc)
        return True, f"sent to {', '.join(recipients)} via SMTP"

    async def _ntfy(self, title: str, message: str, priority: str) -> None:
        ntfy = self.config.notifications.ntfy
        if not ntfy.enabled or not ntfy.topic:
            return
        url = f"{ntfy.url.rstrip('/')}/{ntfy.topic}"
        headers = {"Title": title, "Priority": priority}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, content=message, headers=headers)
        except Exception as exc:
            logger.warning("ntfy send failed: %s", exc)

    async def _smtp(self, title: str, message: str) -> None:
        smtp = self.config.notifications.smtp
        if not smtp.enabled or not smtp.host or not smtp.to_addrs:
            return
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = smtp.from_addr or smtp.username
        msg["To"] = ", ".join(smtp.to_addrs)
        msg.set_content(message)
        try:
            with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as server:
                server.starttls()
                if smtp.username:
                    server.login(smtp.username, smtp.password)
                server.send_message(msg)
        except Exception as exc:
            logger.warning("smtp send failed: %s", exc)
