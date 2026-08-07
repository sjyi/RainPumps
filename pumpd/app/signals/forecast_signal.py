"""Forecast-derived rain signal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import RulesConfig
from app.engine import HourlyForecast, RainState, forecast_is_raining
from app.models import ForecastRow
from app.signals.base import RainSignal
from app.weather.base import merge_conservative


class ForecastSignal(RainSignal):
    def __init__(self, session_factory: sessionmaker[Session], rules: RulesConfig) -> None:
        self.session_factory = session_factory
        self.rules = rules

    async def get_state(self) -> RainState:
        now = datetime.now(UTC)
        with self.session_factory() as session:
            rows = session.scalars(select(ForecastRow).order_by(ForecastRow.hour_ts)).all()

        by_provider: dict[str, list[HourlyForecast]] = {}
        for row in rows:
            hf = HourlyForecast(hour_ts=row.hour_ts, pop_pct=row.pop_pct, rain_mm=row.rain_mm)
            by_provider.setdefault(row.provider, []).append(hf)

        merged: list[HourlyForecast] = []
        for idx, provider_rows in enumerate(by_provider.values()):
            merged = provider_rows if idx == 0 else merge_conservative(merged, provider_rows)

        is_raining = forecast_is_raining(
            merged, now, self.rules.precip_probability_threshold
        )
        rate = 0.0
        now_utc = now
        for f in merged:
            hour = f.hour_ts if f.hour_ts.tzinfo else f.hour_ts.replace(tzinfo=UTC)
            if hour <= now_utc < hour + timedelta(hours=1):
                rate = f.rain_mm
        return RainState(
            is_raining=is_raining,
            rate_mm_h=rate,
            confidence=0.7,
            source="forecast",
            ts=now,
        )
