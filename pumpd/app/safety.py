"""Safety enforcement helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.engine import RainState, SafetyFlags, _as_utc
from app.models import EngineMetaRow, ForecastRow


def get_engine_meta(session: Session, key: str) -> str | None:
    row = session.get(EngineMetaRow, key)
    return row.value if row else None


def set_engine_meta(session: Session, key: str, value: str) -> None:
    row = session.get(EngineMetaRow, key)
    if row:
        row.value = value
    else:
        session.add(EngineMetaRow(key=key, value=value))


def compute_safety_flags(
    session: Session,
    config: AppConfig,
    rain: RainState,
) -> SafetyFlags:
    now = datetime.now(UTC)
    stale_hours = config.safety.watchdog_stale_forecast_hours
    latest = session.scalar(select(func.max(ForecastRow.fetched_at)))
    stale_forecast = latest is None or (now - _as_utc(latest)) > timedelta(hours=stale_hours)

    last_eval_str = get_engine_meta(session, "engine_last_eval_at")
    eval_stale = True
    if last_eval_str:
        last_eval = _as_utc(datetime.fromisoformat(last_eval_str))
        max_age = timedelta(minutes=2 * config.rules.evaluate_minutes)
        eval_stale = (now - last_eval) > max_age

    mqtt_override = False
    if config.mqtt.enabled and rain.source == "mqtt":
        if rain.is_raining and rain.confidence >= config.mqtt.min_confidence:
            mqtt_override = True

    return SafetyFlags(
        stale_forecast=stale_forecast,
        engine_watchdog=eval_stale,
        mqtt_stale_override=mqtt_override and stale_forecast,
    )
