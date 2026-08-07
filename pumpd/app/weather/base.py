"""Weather provider abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.engine import HourlyForecast


@dataclass(frozen=True)
class ProviderResult:
    forecasts: list[HourlyForecast]
    provider: str


class WeatherProvider(ABC):
    name: str

    @abstractmethod
    async def fetch(self, latitude: float, longitude: float) -> list[HourlyForecast]:
        raise NotImplementedError


def is_us_location(latitude: float, longitude: float) -> bool:
    """Rough US bounding box for NWS eligibility."""
    return 24.0 <= latitude <= 49.5 and -125.0 <= longitude <= -66.0


def merge_conservative(
    primary: list[HourlyForecast], secondary: list[HourlyForecast]
) -> list[HourlyForecast]:
    """Merge two forecast lists using the more conservative (higher pop/rain) values."""
    by_hour: dict[datetime, HourlyForecast] = {f.hour_ts: f for f in primary}
    for row in secondary:
        existing = by_hour.get(row.hour_ts)
        if existing is None:
            by_hour[row.hour_ts] = row
        else:
            by_hour[row.hour_ts] = HourlyForecast(
                hour_ts=row.hour_ts,
                pop_pct=max(existing.pop_pct, row.pop_pct),
                rain_mm=max(existing.rain_mm, row.rain_mm),
            )
    return sorted(by_hour.values(), key=lambda f: f.hour_ts)
