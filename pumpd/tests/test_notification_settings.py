"""Notification settings save and hot reload."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_config
from app.db import init_db
from app.service import PumpService


@pytest.fixture
def service(tmp_path: Path) -> PumpService:
    import shutil

    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = load_config(config_path)
    cfg.database_url = f"sqlite:///{tmp_path / 'test.db'}"
    session_factory = init_db(cfg.database_url)
    return PumpService(cfg, session_factory, config_path=str(config_path))


def test_update_notification_settings_applies_without_restart(service: PumpService) -> None:
    assert service.notifier.config.notifications.admin_email == ""

    result = service.update_notification_settings(
        admin_email="ops@example.com",
        public_base_url="http://localhost:8080",
        smtp_enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="user@example.com",
        smtp_from_addr="pumpd@example.com",
        smtp_to_addrs=["backup@example.com"],
    )

    assert result["admin_email"] == "ops@example.com"
    assert result["smtp"]["enabled"] is True
    assert result["smtp"]["host"] == "smtp.example.com"
    assert service.config.notifications.admin_email == "ops@example.com"
    assert service.notifier.config.notifications.admin_email == "ops@example.com"

    reloaded = load_config(service.config_path)
    assert reloaded.notifications.admin_email == "ops@example.com"
    assert reloaded.notifications.smtp.host == "smtp.example.com"


def test_update_command_verify_does_not_require_restart(service: PumpService) -> None:
    service.update_command_verify_settings(command_verify_delay_seconds=22)
    assert service.config.devices.command_verify_delay_seconds == 22
