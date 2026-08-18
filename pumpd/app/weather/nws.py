"""NWS (US only) weather provider."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.engine import HourlyForecast
from app.weather.base import WeatherProvider, is_us_location

NWS_BASE = "https://api.weather.gov"
USER_AGENT = "pumpd/1.1 (rain pump controller)"


class NwsProvider(WeatherProvider):
    name = "nws"

    async def fetch(self, latitude: float, longitude: float) -> list[HourlyForecast]:
        if not is_us_location(latitude, longitude):
            return []

        # NWS requires coordinates rounded to 4 decimal places (otherwise 301 redirect).
        lat = round(latitude, 4)
        lon = round(longitude, 4)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            points = await client.get(f"{NWS_BASE}/points/{lat},{lon}")
            points.raise_for_status()
            forecast_url = points.json()["properties"]["forecastHourly"]
            resp = await client.get(forecast_url)
            resp.raise_for_status()
            periods = resp.json()["properties"]["periods"]

        results: list[HourlyForecast] = []
        for period in periods:
            start = datetime.fromisoformat(period["startTime"].replace("Z", "+00:00"))
            pop_obj = period.get("probabilityOfPrecipitation") or {}
            pop = float(pop_obj.get("value") or 0)
            # NWS hourly periods may include quantitative precip in mm via unitCode
            rain_mm = 0.0
            qpf = period.get("quantitativePrecipitation")
            if qpf and qpf.get("value") is not None:
                val = float(qpf["value"])
                unit = (qpf.get("unitCode") or "").lower()
                rain_mm = val / 25.4 if "inch" in unit else val
            results.append(
                HourlyForecast(
                    hour_ts=start.astimezone(UTC),
                    pop_pct=pop,
                    rain_mm=rain_mm,
                )
            )
        return results
