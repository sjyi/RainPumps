"""SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ForecastRow(Base):
    __tablename__ = "forecasts"
    __table_args__ = (UniqueConstraint("provider", "hour_ts", name="uq_forecast_provider_hour"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    hour_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    pop_pct: Mapped[float] = mapped_column(Float)
    rain_mm: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ForecastHistoryRow(Base):
    """Append-only forecast snapshots (one row per provider/hour per poll)."""

    __tablename__ = "forecast_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    hour_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    pop_pct: Mapped[float] = mapped_column(Float)
    rain_mm: Mapped[float] = mapped_column(Float)


class ProviderHealthRow(Base):
    __tablename__ = "provider_health"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PumpStateRow(Base):
    __tablename__ = "pump_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), default="idle")
    mode: Mapped[str] = mapped_column(String(32), default="auto")
    device_on: Mapped[bool] = mapped_column(default=False)
    duty_on: Mapped[bool] = mapped_column(default=True)
    runtime_continuous_min: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_revert_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    manual_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_today_min: Mapped[int] = mapped_column(Integer, default=0)
    post_rain_drain_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sensor_dry_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duty_cycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    safety_override_approved: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    pump_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class EngineMetaRow(Base):
    __tablename__ = "engine_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class WeatherCurrentRow(Base):
    __tablename__ = "weather_current"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    temp_c: Mapped[float] = mapped_column(Float)
    humidity_pct: Mapped[float] = mapped_column(Float)
    weather_code: Mapped[int] = mapped_column(Integer)
    precipitation_mm: Mapped[float] = mapped_column(Float)
    rain_mm: Mapped[float] = mapped_column(Float)
    is_day: Mapped[bool] = mapped_column(default=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WeatherDailyRow(Base):
    __tablename__ = "weather_daily"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    weather_code: Mapped[int] = mapped_column(Integer)
    temp_max_c: Mapped[float] = mapped_column(Float)
    temp_min_c: Mapped[float] = mapped_column(Float)
    precip_sum_mm: Mapped[float] = mapped_column(Float)
    pop_max_pct: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HardwareHealthRow(Base):
    __tablename__ = "hardware_health"

    component_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    component_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="ok")
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
