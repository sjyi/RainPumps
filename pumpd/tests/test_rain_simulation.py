"""Rain simulation tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import AppConfig, RulesConfig
from app.db import init_db
from app.engine import RainState, evaluate, forecast_is_raining
from app.models import ForecastRow, PumpStateRow
from app.rain_simulation import (
    inject_simulation_forecast,
)
from app.service import PumpService


def test_inject_simulation_forecast_raining() -> None:
    session_factory = init_db("sqlite:///:memory:")
    inject_simulation_forecast(session_factory, raining=True, lookahead_hours=2)
    with session_factory() as session:
        rows = session.scalars(select(ForecastRow)).all()
    assert rows
    assert all(r.provider == "simulation" for r in rows)
    assert all(r.pop_pct == 90.0 for r in rows)
    assert forecast_is_raining(
        [
            type("HF", (), {"hour_ts": r.hour_ts, "pop_pct": r.pop_pct, "rain_mm": r.rain_mm})()
            for r in rows
        ],
        datetime.now(UTC),
        70,
    )


def test_inject_simulation_forecast_dry() -> None:
    session_factory = init_db("sqlite:///:memory:")
    inject_simulation_forecast(session_factory, raining=False, lookahead_hours=2)
    with session_factory() as session:
        rows = session.scalars(select(ForecastRow)).all()
    assert rows
    assert all(r.pop_pct == 0.0 for r in rows)


@pytest.mark.asyncio
async def test_simulation_cycle_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(
        pumps=[{"name": "north_pump", "enabled": True}],
        rules=RulesConfig(post_rain_drain_minutes=30),
    )
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._build_devices()
    service._ensure_pump_rows()

    with session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        row.mode = "auto"
        session.commit()

    service.devices["north_pump"] = AsyncMock()
    service.devices["north_pump"].turn_on = AsyncMock(
        return_value=type("R", (), {"success": True, "adapter": "mock", "message": ""})()
    )
    service.devices["north_pump"].turn_off = AsyncMock(
        return_value=type("R", (), {"success": True, "adapter": "mock", "message": ""})()
    )
    service.devices["north_pump"].get_state = AsyncMock(return_value=type("S", (), {"value": "off"})())
    service.devices["north_pump"].has_control_path = lambda: True

    monkeypatch.setattr("app.service.RAIN_PHASE_SECONDS", 0)
    monkeypatch.setattr("app.service.DRAIN_WAIT_SECONDS", 0)
    monkeypatch.setattr(service, "poll_forecasts", AsyncMock())
    monkeypatch.setattr(service, "_execute_commands", AsyncMock())

    status = await service.start_auto_rain_simulation()
    assert status["active"] is True
    await service._simulation_task
    assert service.rain_simulation.phase == "complete"
    assert service.config.rules.post_rain_drain_minutes == 30


@pytest.mark.asyncio
async def test_simulation_requires_auto_mode() -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(pumps=[{"name": "north_pump", "enabled": True}])
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._ensure_pump_rows()

    with session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        row.mode = "manual_off"
        session.commit()

    with pytest.raises(ValueError, match="Auto mode"):
        await service.start_auto_rain_simulation()
