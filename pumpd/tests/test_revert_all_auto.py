"""Global Auto button — revert all pumps from manual mode."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppConfig
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
async def test_revert_all_to_auto_clears_manual_modes() -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(
        pumps=[
            {"name": "auto_pump", "enabled": True},
            {"name": "manual_on_pump", "enabled": True},
            {"name": "manual_off_pump", "enabled": True},
        ],
    )
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._ensure_pump_rows()
    service.run_evaluation = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with session_factory() as session:
        auto = session.get(PumpStateRow, "auto_pump")
        manual_on = session.get(PumpStateRow, "manual_on_pump")
        manual_off = session.get(PumpStateRow, "manual_off_pump")
        assert auto is not None and manual_on is not None and manual_off is not None
        auto.mode = "auto"
        manual_on.mode = "manual_on"
        manual_on.manual_revert_at = manual_on.updated_at
        manual_off.mode = "manual_off"
        manual_off.manual_revert_at = manual_off.updated_at
        session.commit()

    result = await service.revert_all_to_auto()

    assert sorted(result["reverted"]) == ["manual_off_pump", "manual_on_pump"]
    assert result["already_auto"] == ["auto_pump"]
    assert "Automatic control restored" in result["message"]
    service.run_evaluation.assert_awaited_once()

    with session_factory() as session:
        manual_on = session.get(PumpStateRow, "manual_on_pump")
        manual_off = session.get(PumpStateRow, "manual_off_pump")
        assert manual_on is not None and manual_off is not None
        assert manual_on.mode == "auto"
        assert manual_off.mode == "auto"
        assert manual_on.manual_revert_at is None
        assert manual_off.manual_revert_at is None
        events = session.scalars(
            select(EventRow).where(EventRow.event_type == "manual_revert_all")
        ).all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_revert_all_to_auto_when_already_auto() -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(pumps=[{"name": "north_pump", "enabled": True}])
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._ensure_pump_rows()
    service.run_evaluation = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await service.revert_all_to_auto()

    assert result["reverted"] == []
    assert result["already_auto"] == ["north_pump"]
    assert "already in automatic mode" in result["message"]
    service.run_evaluation.assert_not_awaited()


def test_user_screen_has_auto_button(client: TestClient) -> None:
    response = client.get("/partials/user/status")
    assert response.status_code == 200
    html = response.text
    assert "auto-mode-btn" in html
    assert html.index("auto-mode-btn") < html.index("drain-puddles-btn")


def test_admin_screen_does_not_have_auto_button(client: TestClient) -> None:
    response = client.get("/partials/admin/status")
    assert response.status_code == 200
    assert "auto-mode-btn" not in response.text


def test_auto_api_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    service = client.app.state.service
    monkeypatch.setattr(
        service,
        "revert_all_to_auto",
        AsyncMock(
            return_value={
                "reverted": ["north_pump"],
                "already_auto": [],
                "skipped": [],
                "message": "Automatic control restored for 1 pump(s)",
            }
        ),
    )

    response = client.post("/api/auto/start")
    assert response.status_code == 200
    data = response.json()
    assert data["reverted"] == ["north_pump"]
    assert "Automatic control restored" in data["message"]
