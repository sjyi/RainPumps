"""Forecast-derived rain signal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import RulesConfig, WeatherConfig
from app.engine import HourlyForecast, RainState, _as_utc, forecast_is_raining
from app.models import ForecastRow, WeatherCurrentRow
from app.signals.base import RainSignal
from app.weather.base import merge_conservative


class ForecastSignal(RainSignal):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        rules: RulesConfig,
        weather: WeatherConfig,
    ) -> None:
        self.session_factory = session_factory
        self.rules = rules
        self.weather = weather

    def _observation_from_current(self, now: datetime) -> RainState | None:
        max_age = timedelta(minutes=max(5, self.weather.current_poll_minutes * 3))
        with self.session_factory() as session:
            row = session.get(WeatherCurrentRow, 1)
            if not row or not row.provider:
                return None
            if row.provider != self.weather.display_provider:
                return None
            fetched_at = _as_utc(row.fetched_at)
            if now - fetched_at > max_age:
                return None
            if not row.has_precipitation:
                return None
            return RainState(
                is_raining=True,
                rate_mm_h=max(row.rain_mm, 0.1),
                confidence=0.95,
                source="observation",
                ts=fetched_at,
            )

    async def get_state(self) -> RainState:
        now = datetime.now(UTC)
        observation = self._observation_from_current(now)
        if observation is not None:
            return observation

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
