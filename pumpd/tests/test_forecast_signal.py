"""Forecast signal observation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import RulesConfig, WeatherConfig
from app.db import init_db
from app.models import WeatherCurrentRow
from app.signals.forecast_signal import ForecastSignal


@pytest.mark.asyncio
async def test_forecast_signal_uses_current_observation_when_raining() -> None:
    session_factory = init_db("sqlite:///:memory:")
    weather = WeatherConfig(
        poll_minutes=5,
        current_poll_minutes=5,
        providers=["accuweather"],
        display_provider="accuweather",
    )
    signal = ForecastSignal(session_factory, RulesConfig(), weather)
    now = datetime.now(UTC)

    with session_factory() as session:
        session.add(
            WeatherCurrentRow(
                id=1,
                temp_c=18.0,
                humidity_pct=90.0,
                weather_code=61,
                precipitation_mm=1.0,
                rain_mm=1.0,
                is_day=True,
                fetched_at=now,
                provider="accuweather",
                has_precipitation=True,
                weather_text="Light rain",
            )
        )
        session.commit()

    state = await signal.get_state()
    assert state.is_raining is True
    assert state.source == "observation"
    assert state.confidence == 0.95


@pytest.mark.asyncio
async def test_forecast_signal_ignores_stale_observation() -> None:
    session_factory = init_db("sqlite:///:memory:")
    weather = WeatherConfig(current_poll_minutes=5, display_provider="accuweather")
    signal = ForecastSignal(session_factory, RulesConfig(), weather)
    stale = datetime.now(UTC) - timedelta(minutes=30)

    with session_factory() as session:
        session.add(
            WeatherCurrentRow(
                id=1,
                temp_c=18.0,
                humidity_pct=90.0,
                weather_code=61,
                precipitation_mm=1.0,
                rain_mm=1.0,
                is_day=True,
                fetched_at=stale,
                provider="accuweather",
                has_precipitation=True,
                weather_text="Light rain",
            )
        )
        session.commit()

    state = await signal.get_state()
    assert state.source == "forecast"
    assert state.is_raining is False
