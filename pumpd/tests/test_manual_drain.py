"""Manual post-rain drain (user Drain button)."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppConfig, RulesConfig
from app.db import init_db
from app.models import EventRow, PumpStateRow
from app.service import PumpService


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    import shutil

    from app.config import load_config as real_load_config

    db_path = tmp_path / "test.db"
    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = real_load_config(config_path)
    cfg.database_url = f"sqlite:///{db_path}"

    def _load_config(_path: str = "config.yaml"):
        loaded = real_load_config(config_path)
        loaded.database_url = f"sqlite:///{db_path}"
        return loaded

    monkeypatch.setattr("app.main.load_config", _load_config)
    monkeypatch.setattr("app.config.load_config", _load_config)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))

    from app.main import create_app

    app = create_app(str(config_path))
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_start_manual_drain_sets_post_rain_phase() -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(
        pumps=[{"name": "north_pump", "enabled": True}],
        rules=RulesConfig(post_rain_drain_minutes=30),
    )
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._ensure_pump_rows()
    service.run_evaluation = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._execute_commands = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        row.mode = "auto"
        row.phase = "idle"
        row.device_on = False
        session.commit()

    result = await service.start_manual_drain()

    assert result["started"] == ["north_pump"]
    assert result["drain_minutes"] == 30
    assert result["commands_sent"] == 1
    assert "Draining 1 pump" in result["message"]
    service._execute_commands.assert_awaited_once()
    service.run_evaluation.assert_awaited_once()

    with session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        assert row.phase == "post_rain_drain"
        assert row.post_rain_drain_started_at is not None
        events = session.scalars(
            select(EventRow).where(EventRow.event_type == "manual_drain_start")
        ).all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_start_manual_drain_skips_manual_modes() -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(
        pumps=[
            {"name": "auto_pump", "enabled": True},
            {"name": "manual_off_pump", "enabled": True},
        ],
    )
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._ensure_pump_rows()
    service.run_evaluation = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with session_factory() as session:
        auto = session.get(PumpStateRow, "auto_pump")
        manual_off = session.get(PumpStateRow, "manual_off_pump")
        assert auto is not None and manual_off is not None
        auto.mode = "auto"
        manual_off.mode = "manual_off"
        session.commit()

    result = await service.start_manual_drain()

    assert result["started"] == ["auto_pump"]
    assert any(s["name"] == "manual_off_pump" for s in result["skipped"])


def test_user_screen_has_drain_button(client: TestClient) -> None:
    response = client.get("/partials/user/status")
    assert response.status_code == 200
    html = response.text
    assert "drain-puddles-btn" in html
    assert "auto-mode-btn" in html
    assert "Drain" in html
    assert html.index("auto-mode-btn") < html.index("drain-puddles-btn")


def test_admin_screen_does_not_have_drain_button(client: TestClient) -> None:
    response = client.get("/partials/admin/status")
    assert response.status_code == 200
    assert "drain-puddles-btn" not in response.text
    assert "auto-mode-btn" not in response.text


def test_drain_api_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    service = client.app.state.service
    monkeypatch.setattr(
        service,
        "start_manual_drain",
        AsyncMock(
            return_value={
                "started": ["north_pump"],
                "skipped": [],
                "drain_minutes": 30,
                "message": "Draining 1 pump(s) for up to 30 min",
            }
        ),
    )

    response = client.post("/api/drain/start")
    assert response.status_code == 200
    data = response.json()
    assert data["started"] == ["north_pump"]
    assert "Draining 1 pump" in data["message"]
