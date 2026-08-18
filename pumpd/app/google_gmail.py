"""Google OAuth + Gmail API for sending alert email without SMTP passwords."""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/userinfo.email"
)

_pending_states: dict[str, float] = {}
_STATE_TTL_SECONDS = 600


@dataclass
class GmailConnection:
    refresh_token: str
    email: str
    connected_at: str


class GoogleGmailClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: Path,
        redirect_uri: str,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.token_path = token_path
        self.redirect_uri = redirect_uri

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def connection(self) -> GmailConnection | None:
        if not self.token_path.exists():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        refresh = str(data.get("refresh_token", "")).strip()
        email = str(data.get("email", "")).strip()
        if not refresh or not email:
            return None
        return GmailConnection(
            refresh_token=refresh,
            email=email,
            connected_at=str(data.get("connected_at", "")),
        )

    @property
    def connected(self) -> bool:
        return self.connection() is not None

    def start_auth_url(self) -> str:
        if not self.configured:
            raise ValueError("Google OAuth client ID and secret are not configured")
        state = secrets.token_urlsafe(24)
        _pending_states[state] = time.time() + _STATE_TTL_SECONDS
        _cleanup_states()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def validate_state(self, state: str) -> bool:
        expires = _pending_states.pop(state, None)
        if expires is None:
            return False
        return time.time() <= expires

    async def complete_auth(self, code: str) -> GmailConnection:
        token_data = await self._token_request(
            {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        refresh_token = str(token_data.get("refresh_token", "")).strip()
        access_token = str(token_data.get("access_token", "")).strip()
        if not refresh_token:
            raise ValueError(
                "Google did not return a refresh token; revoke pumpd access in your "
                "Google account and sign in again"
            )
        email = await self._fetch_email(access_token)
        conn = GmailConnection(
            refresh_token=refresh_token,
            email=email,
            connected_at=_utc_now_iso(),
        )
        self._save_connection(conn)
        return conn

    def disconnect(self) -> None:
        if self.token_path.exists():
            self.token_path.unlink()

    async def send_email(self, *, to: str, subject: str, body: str) -> tuple[bool, str]:
        conn = self.connection()
        if conn is None:
            return False, "Google account not connected"
        try:
            access_token = await self._access_token(conn.refresh_token)
        except Exception as exc:
            logger.warning("google gmail token refresh failed: %s", exc)
            return False, f"Google token refresh failed: {exc}"
        raw = _encode_message(from_addr=conn.email, to=to, subject=subject, body=body)
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    GMAIL_SEND_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"raw": raw},
                )
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("google gmail send failed: %s", exc)
            return False, str(exc)
        return True, f"sent to {to} via Gmail ({conn.email})"

    def _save_connection(self, conn: GmailConnection) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "refresh_token": conn.refresh_token,
            "email": conn.email,
            "connected_at": conn.connected_at,
        }
        self.token_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def _access_token(self, refresh_token: str) -> str:
        data = await self._token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        )
        token = str(data.get("access_token", "")).strip()
        if not token:
            raise ValueError("missing access_token in Google response")
        return token

    async def _fetch_email(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
        email = str(data.get("email", "")).strip()
        if not email:
            raise ValueError("Google userinfo did not include email")
        return email

    async def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=payload)
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("error_description") or resp.json().get("error") or detail
                except Exception:
                    pass
                raise ValueError(str(detail))
            return resp.json()


def _encode_message(*, from_addr: str, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _cleanup_states() -> None:
    now = time.time()
    expired = [state for state, ts in _pending_states.items() if ts < now]
    for state in expired:
        _pending_states.pop(state, None)


def default_token_path(config_path: str) -> Path:
    return Path(config_path).resolve().parent / "data" / "gmail_oauth.json"


def default_redirect_uri(public_base_url: str) -> str:
    return public_base_url.rstrip("/") + "/api/auth/google/gmail/callback"
