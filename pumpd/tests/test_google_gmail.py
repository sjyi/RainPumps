"""Google Gmail OAuth helpers."""

from __future__ import annotations

from app.google_gmail import GoogleGmailClient, _encode_message


def test_encode_message() -> None:
    raw = _encode_message(
        from_addr="sender@example.com",
        to="admin@example.com",
        subject="Test",
        body="Hello",
    )
    assert raw
    assert "=" not in raw


def test_oauth_state_validation() -> None:
    client = GoogleGmailClient(
        client_id="id",
        client_secret="secret",
        token_path="/tmp/unused.json",
        redirect_uri="http://localhost/callback",
    )
    url = client.start_auth_url()
    state = url.split("state=")[1].split("&")[0]
    assert client.validate_state(state) is True
    assert client.validate_state(state) is False
