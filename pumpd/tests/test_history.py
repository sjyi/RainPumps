"""History — control events and forecast snapshots."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import init_db
from app.models import ForecastHistoryRow
from app.service import CONTROL_EVENT_TYPES, PumpService


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    db_path = tmp_path / "test.db"
    cfg = load_config("config.example.yaml")
    cfg.database_url = f"sqlite:///{db_path}"

    monkeypatch.setattr("app.main.load_config", lambda _path="config.yaml": cfg)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))

    from app.main import create_app

    app = create_app("config.example.yaml")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def service() -> PumpService:
    session_factory = init_db("sqlite:///:memory:")
    config = load_config("config.example.yaml")
    return PumpService(config, session_factory, config_path="config.example.yaml")


def test_control_history_filters_event_types(service: PumpService) -> None:
    service._log_event("pump_a", "turn_on", "manual test", details={"success": True})
    service._log_event("pump_a", "turn_off", "auto dry", details={"success": True})
    service._log_event(
        "pump_a",
        "mode_change",
        "mode set to manual_on",
        details={"mode": "manual_on", "previous_mode": "auto"},
    )
    service._log_event(None, "rain_simulation_start", "sim started")

    rows = service.get_control_history(limit=50)
    types = {r["event_type"] for r in rows}
    assert types <= CONTROL_EVENT_TYPES
    assert "rain_simulation_start" not in types
    assert len(rows) == 3

    on_row = next(r for r in rows if r["event_type"] == "turn_on")
    assert on_row["action"] == "ON"
    assert on_row["success"] is True


def test_forecast_history_returns_recent_snapshots(service: PumpService) -> None:
    now = datetime.now(UTC)
    with service.session_factory() as session:
        session.add(
            ForecastHistoryRow(
                fetched_at=now,
                provider="open_meteo",
                hour_ts=now + timedelta(hours=1),
                pop_pct=42.0,
                rain_mm=1.5,
            )
        )
        session.add(
            ForecastHistoryRow(
                fetched_at=now - timedelta(hours=72),
                provider="open_meteo",
                hour_ts=now - timedelta(hours=71),
                pop_pct=10.0,
                rain_mm=0.0,
            )
        )
        session.commit()

    rows = service.get_forecast_history(limit=50, hours=48)
    assert len(rows) == 1
    assert rows[0]["provider"] == "open_meteo"
    assert rows[0]["pop_pct"] == 42.0
    assert rows[0]["rain_mm"] == 1.5
    assert "fetched_local" in rows[0]
    assert "hour_local" in rows[0]


def test_history_api_endpoints(client: TestClient) -> None:
    controls = client.get("/api/history/controls")
    assert controls.status_code == 200
    assert "controls" in controls.json()

    forecasts = client.get("/api/history/forecasts")
    assert forecasts.status_code == 200
    assert "forecasts" in forecasts.json()


def test_admin_ui_shows_history_section(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert "history-section" in response.text
    assert "history-timeline-root" in response.text
