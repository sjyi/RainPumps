"""AccuWeather weather provider (hourly forecast + current conditions)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.engine import HourlyForecast
from app.weather.base import WeatherProvider
from app.weather.display import CurrentConditions

logger = logging.getLogger(__name__)

ACCUWEATHER_BASE = "https://dataservice.accuweather.com"
RAIN_PRECIP_TYPES = frozenset({"Rain", "Mixed", "Ice"})


def accuweather_icon_to_code(icon: int) -> int:
    """Map AccuWeather icon codes to WMO-style codes used by the dashboard."""
    if icon in (1, 33):
        return 0
    if icon in (2, 34):
        return 1
    if icon in (3, 4, 6, 7, 35, 36, 38):
        return 3
    if icon in (5, 37):
        return 2
    if icon in (11, 37):
        return 45
    if icon in (12, 13, 14, 18, 39, 40):
        return 61
    if icon in (15, 16, 17, 41, 42):
        return 95
    if icon in (19, 20, 21, 22, 23, 43, 44):
        return 71
    if icon in (24, 25, 26, 29):
        return 67
    if icon in (30, 31, 32):
        return 2
    return 3


def _metric_value(obj: dict[str, Any] | None) -> float:
    if not obj:
        return 0.0
    metric = obj.get("Metric") or {}
    value = metric.get("Value")
    return float(value) if value is not None else 0.0


def _is_raining_now(has_precipitation: bool, precipitation_type: str | None) -> bool:
    if has_precipitation:
        return True
    if precipitation_type in RAIN_PRECIP_TYPES:
        return True
    return False


class AccuWeatherProvider(WeatherProvider):
    name = "accuweather"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self._location_key_cache: dict[tuple[float, float], str] = {}

    def _cache_key(self, latitude: float, longitude: float) -> tuple[float, float]:
        return (round(latitude, 4), round(longitude, 4))

    async def resolve_location_key(self, latitude: float, longitude: float) -> str:
        key = self._cache_key(latitude, longitude)
        cached = self._location_key_cache.get(key)
        if cached:
            return cached

        params = {
            "apikey": self.api_key,
            "q": f"{key[0]},{key[1]}",
            "language": "en-us",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{ACCUWEATHER_BASE}/locations/v1/cities/geoposition/search",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        location_key = data.get("Key")
        if not location_key:
            raise ValueError("AccuWeather location key not found for coordinates")
        self._location_key_cache[key] = location_key
        return location_key

    async def fetch(self, latitude: float, longitude: float) -> list[HourlyForecast]:
        location_key = await self.resolve_location_key(latitude, longitude)
        params = {
            "apikey": self.api_key,
            "metric": "true",
            "details": "true",
            "language": "en-us",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{ACCUWEATHER_BASE}/forecasts/v1/hourly/12hour/{location_key}",
                params=params,
            )
            resp.raise_for_status()
            hours = resp.json()

        results: list[HourlyForecast] = []
        for hour in hours:
            dt_raw = hour.get("DateTime")
            if not dt_raw:
                continue
            hour_ts = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00")).astimezone(UTC)
            pop = float(hour.get("PrecipitationProbability") or 0)
            rain_mm = _metric_value(hour.get("TotalLiquid"))
            if rain_mm <= 0:
                rain_mm = _metric_value(hour.get("Rain"))
            if rain_mm <= 0 and hour.get("HasPrecipitation"):
                intensity = (hour.get("PrecipitationIntensity") or "").lower()
                rain_mm = {"light": 0.5, "moderate": 2.0, "heavy": 5.0}.get(intensity, 0.5)
            results.append(
                HourlyForecast(
                    hour_ts=hour_ts,
                    pop_pct=pop,
                    rain_mm=rain_mm,
                )
            )
        return results

    async def fetch_current(
        self, latitude: float, longitude: float
    ) -> CurrentConditions:
        location_key = await self.resolve_location_key(latitude, longitude)
        params = {
            "apikey": self.api_key,
            "details": "true",
            "language": "en-us",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{ACCUWEATHER_BASE}/currentconditions/v1/{location_key}",
                params=params,
            )
            resp.raise_for_status()
            rows = resp.json()

        if not rows:
            raise ValueError("AccuWeather current conditions response was empty")

        row = rows[0]
        icon = int(row.get("WeatherIcon") or 0)
        has_precipitation = _is_raining_now(
            bool(row.get("HasPrecipitation")),
            row.get("PrecipitationType"),
        )
        precip_mm = _metric_value(row.get("Precip1hr"))
        if precip_mm <= 0:
            summary = row.get("PrecipitationSummary") or {}
            precip_mm = _metric_value(summary.get("PastHour"))
        rain_mm = precip_mm if has_precipitation else 0.0
        if has_precipitation and rain_mm <= 0:
            rain_mm = 0.1

        observed_raw = row.get("LocalObservationDateTime")
        if observed_raw:
            fetched_at = datetime.fromisoformat(
                str(observed_raw).replace("Z", "+00:00")
            ).astimezone(UTC)
        else:
            fetched_at = datetime.now(UTC)

        return CurrentConditions(
            temp_c=float((row.get("Temperature") or {}).get("Metric", {}).get("Value") or 0),
            humidity_pct=float(row.get("RelativeHumidity") or 0),
            weather_code=accuweather_icon_to_code(icon),
            precipitation_mm=precip_mm,
            rain_mm=rain_mm,
            is_day=bool(row.get("IsDayTime", True)),
            fetched_at=fetched_at,
            weather_text=str(row.get("WeatherText") or ""),
            has_precipitation=has_precipitation,
            provider=self.name,
        )
