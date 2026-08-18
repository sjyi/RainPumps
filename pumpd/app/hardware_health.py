"""Hardware health monitoring for pumps and sensors."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.config import AppConfig
from app.engine import RainState
from app.models import HardwareHealthRow
from app.time_format import format_local

logger = logging.getLogger(__name__)


class CommandLockError(Exception):
    """Raised when a per-pump command lock cannot be acquired in time."""


class HardwareMonitor:
    def __init__(self, config: AppConfig, session_factory: sessionmaker[Session]) -> None:
        self.config = config
        self.session_factory = session_factory

    def _get_or_create(
        self, session: Session, component_id: str, component_type: str
    ) -> HardwareHealthRow:
        row = session.get(HardwareHealthRow, component_id)
        if row is None:
            row = HardwareHealthRow(
                component_id=component_id,
                component_type=component_type,
                status="ok",
                consecutive_failures=0,
            )
            session.add(row)
        return row

    def record_pump_success(self, pump_name: str) -> None:
        if not self.config.hardware_monitor.enabled:
            return
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = self._get_or_create(session, pump_name, "pump")
            row.status = "ok"
            row.last_ok_at = now
            row.last_error = None
            row.consecutive_failures = 0
            session.commit()

    def record_pump_failure(
        self, pump_name: str, error: str, *, verify_mismatch: bool = False
    ) -> None:
        if not self.config.hardware_monitor.enabled:
            return
        now = datetime.now(UTC)
        threshold = self.config.hardware_monitor.pump_failure_threshold
        with self.session_factory() as session:
            row = self._get_or_create(session, pump_name, "pump")
            row.last_error = error
            row.last_error_at = now
            row.consecutive_failures += 1
            mismatch_threshold = self.config.hardware_monitor.verify_mismatch_threshold
            if verify_mismatch and row.consecutive_failures >= mismatch_threshold:
                row.status = "fault"
                row.details_json = json.dumps(
                    {"kind": "verify_mismatch", "count": row.consecutive_failures}
                )
            elif row.consecutive_failures >= threshold:
                row.status = "fault"
                row.details_json = json.dumps(
                    {"kind": "control_failure", "count": row.consecutive_failures}
                )
            else:
                row.status = "degraded"
            failures = row.consecutive_failures
            session.commit()
        logger.warning(
            "hardware fault pump=%s error=%s failures=%d",
            pump_name,
            error,
            failures,
        )

    def record_sensor_message(self, rain: RainState) -> None:
        if not self.config.hardware_monitor.enabled or not self.config.mqtt.enabled:
            return
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = self._get_or_create(session, "mqtt_sensor", "sensor")
            row.status = "ok"
            row.last_ok_at = now
            row.last_error = None
            row.consecutive_failures = 0
            row.details_json = json.dumps(
                {
                    "is_raining": rain.is_raining,
                    "rate_mm_h": rain.rate_mm_h,
                    "confidence": rain.confidence,
                    "water_present": rain.water_present,
                }
            )
            session.commit()

    def check_sensor_stale(self, rain: RainState) -> None:
        if not self.config.hardware_monitor.enabled or not self.config.mqtt.enabled:
            return
        stale_minutes = self.config.hardware_monitor.sensor_stale_minutes
        now = datetime.now(UTC)
        with self.session_factory() as session:
            row = self._get_or_create(session, "mqtt_sensor", "sensor")
            if rain.confidence <= 0:
                row.status = "fault"
                row.last_error = "no MQTT sensor data received"
                row.last_error_at = now
                row.consecutive_failures += 1
                session.commit()
                return
            if row.last_ok_at and (now - row.last_ok_at) > timedelta(minutes=stale_minutes):
                row.status = "fault"
                row.last_error = f"sensor stale > {stale_minutes} min"
                row.last_error_at = now
                session.commit()

    def get_all(self) -> list[HardwareHealthRow]:
        from sqlalchemy import select

        with self.session_factory() as session:
            rows = list(session.scalars(select(HardwareHealthRow)).all())
            for row in rows:
                session.expunge(row)
            return rows

    def status_summary(self, *, timezone: str = "UTC") -> list[dict[str, Any]]:
        return [
            {
                "component_id": r.component_id,
                "component_type": r.component_type,
                "status": r.status,
                "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None,
                "last_ok_at_local": (
                    format_local(r.last_ok_at, timezone, "%Y-%m-%d %H:%M")
                    if r.last_ok_at
                    else None
                ),
                "last_error": r.last_error,
                "consecutive_failures": r.consecutive_failures,
            }
            for r in self.get_all()
        ]
