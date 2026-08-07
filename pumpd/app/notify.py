"""Notification delivery via ntfy and/or SMTP."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def send(self, title: str, message: str, priority: str = "default") -> None:
        await self._ntfy(title, message, priority)
        await self._smtp(title, message)

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
