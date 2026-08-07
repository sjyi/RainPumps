"""Simulated forecast rain for testing auto mode (≤3 minute cycle)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.models import ForecastRow

SIM_PROVIDER = "simulation"
RAIN_PHASE_SECONDS = 45
DRAIN_WAIT_SECONDS = 65
SIM_POST_RAIN_DRAIN_MINUTES = 1


@dataclass
class RainSimulationState:
    active: bool = False
    phase: str = "idle"
    message: str = ""
    started_at: datetime | None = None
    auto_pumps: list[str] = field(default_factory=list)
    skipped_pumps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "phase": self.phase,
            "message": self.message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "auto_pumps": self.auto_pumps,
            "skipped_pumps": self.skipped_pumps,
            "rain_phase_seconds": RAIN_PHASE_SECONDS,
            "drain_wait_seconds": DRAIN_WAIT_SECONDS,
            "estimated_total_seconds": RAIN_PHASE_SECONDS + DRAIN_WAIT_SECONDS + 5,
        }


def inject_simulation_forecast(
    session_factory: sessionmaker[Session],
    *,
    raining: bool,
    lookahead_hours: int = 2,
) -> None:
    """Replace all forecast rows with a simulated rain or dry window."""
    now = datetime.now(UTC)
    hour_base = now.replace(minute=0, second=0, microsecond=0)
    with session_factory() as session:
        session.execute(delete(ForecastRow))
        for offset in range(-1, lookahead_hours + 3):
            hour_ts = hour_base + timedelta(hours=offset)
            session.add(
                ForecastRow(
                    provider=SIM_PROVIDER,
                    hour_ts=hour_ts,
                    pop_pct=90.0 if raining else 0.0,
                    rain_mm=1.5 if raining else 0.0,
                    fetched_at=now,
                )
            )
        session.commit()
