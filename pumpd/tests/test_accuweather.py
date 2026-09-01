"""AccuWeather provider tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.weather.accuweather import AccuWeatherProvider, accuweather_icon_to_code


def test_accuweather_icon_maps_rain() -> None:
    assert accuweather_icon_to_code(18) == 61
    assert accuweather_icon_to_code(42) == 95


@pytest.mark.asyncio
async def test_accuweather_fetch_current_parses_precipitation() -> None:
    provider = AccuWeatherProvider("test-key")
    provider.resolve_location_key = AsyncMock(return_value="349727")  # type: ignore[method-assign]

    current_resp = MagicMock()
    current_resp.json.return_value = [
        {
            "WeatherText": "Light rain",
            "WeatherIcon": 18,
            "HasPrecipitation": True,
            "PrecipitationType": "Rain",
            "IsDayTime": True,
            "RelativeHumidity": 88,
            "LocalObservationDateTime": "2026-08-30T20:00:00-04:00",
            "Temperature": {"Metric": {"Value": 18.5}},
            "Precip1hr": {"Metric": {"Value": 1.2}},
        }
    ]
    current_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=current_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.weather.accuweather.httpx.AsyncClient", return_value=mock_client):
        current = await provider.fetch_current(40.7128, -74.0060)

    assert current.weather_text == "Light rain"
    assert current.has_precipitation is True
    assert current.rain_mm == 1.2
    assert current.provider == "accuweather"


@pytest.mark.asyncio
async def test_accuweather_fetch_hourly_normalizes_fields() -> None:
    provider = AccuWeatherProvider("test-key")
    provider.resolve_location_key = AsyncMock(return_value="349727")  # type: ignore[method-assign]

    hourly_resp = MagicMock()
    hourly_resp.json.return_value = [
        {
            "DateTime": "2026-08-30T20:00:00-04:00",
            "PrecipitationProbability": 80,
            "HasPrecipitation": True,
            "TotalLiquid": {"Metric": {"Value": 2.5}},
        }
    ]
    hourly_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=hourly_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.weather.accuweather.httpx.AsyncClient", return_value=mock_client):
        rows = await provider.fetch(40.7128, -74.0060)

    assert len(rows) == 1
    assert rows[0].pop_pct == 80
    assert rows[0].rain_mm == 2.5
