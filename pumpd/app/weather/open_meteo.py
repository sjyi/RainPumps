"""Open-Meteo weather provider."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx

from app.engine import HourlyForecast
from app.weather.base import WeatherProvider
from app.weather.display import CurrentConditions, DailyForecast

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoProvider(WeatherProvider):
    name = "open_meteo"

    async def fetch(self, latitude: float, longitude: float) -> list[HourlyForecast]:
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "precipitation_probability,rain",
            "forecast_days": 2,
            "timezone": "UTC",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        times: list[str] = data["hourly"]["time"]
        pops: list[float | None] = data["hourly"]["precipitation_probability"]
        rains: list[float | None] = data["hourly"]["rain"]
        results: list[HourlyForecast] = []
        for t, pop, rain in zip(times, pops, rains, strict=True):
            hour_ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
            results.append(
                HourlyForecast(
                    hour_ts=hour_ts,
                    pop_pct=float(pop or 0),
                    rain_mm=float(rain or 0),
                )
            )
        return results

    async def fetch_display(
        self, latitude: float, longitude: float, *, timezone: str = "UTC"
    ) -> tuple[CurrentConditions | None, list[DailyForecast]]:
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,relative_humidity_2m,precipitation,rain,weather_code,is_day"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_sum,precipitation_probability_max"
            ),
            "forecast_days": 7,
            "timezone": timezone,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        now = datetime.now(UTC)
        current_data = data.get("current") or {}
        current: CurrentConditions | None = None
        if current_data:
            current = CurrentConditions(
                temp_c=float(current_data.get("temperature_2m") or 0),
                humidity_pct=float(current_data.get("relative_humidity_2m") or 0),
                weather_code=int(current_data.get("weather_code") or 0),
                precipitation_mm=float(current_data.get("precipitation") or 0),
                rain_mm=float(current_data.get("rain") or 0),
                is_day=bool(current_data.get("is_day", 1)),
                fetched_at=now,
            )

        daily = data.get("daily") or {}
        dates: list[str] = daily.get("time") or []
        codes: list[int | None] = daily.get("weather_code") or []
        tmax: list[float | None] = daily.get("temperature_2m_max") or []
        tmin: list[float | None] = daily.get("temperature_2m_min") or []
        precip: list[float | None] = daily.get("precipitation_sum") or []
        pops: list[float | None] = daily.get("precipitation_probability_max") or []

        daily_forecasts: list[DailyForecast] = []
        for d, code, mx, mn, pr, pop in zip(dates, codes, tmax, tmin, precip, pops, strict=True):
            daily_forecasts.append(
                DailyForecast(
                    day=date.fromisoformat(d),
                    weather_code=int(code or 0),
                    temp_max_c=float(mx or 0),
                    temp_min_c=float(mn or 0),
                    precip_sum_mm=float(pr or 0),
                    pop_max_pct=float(pop or 0),
                )
            )
        return current, daily_forecasts
