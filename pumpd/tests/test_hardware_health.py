"""Hardware health monitor unit tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import AppConfig, HardwareMonitorConfig, MqttConfig
from app.db import init_db
from app.engine import RainState
from app.hardware_health import HardwareMonitor

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


@pytest.fixture
def monitor() -> HardwareMonitor:
    cfg = AppConfig(
        mqtt=MqttConfig(enabled=True),
        hardware_monitor=HardwareMonitorConfig(
            enabled=True,
            sensor_stale_minutes=15,
            pump_failure_threshold=3,
            verify_mismatch_threshold=2,
        )
    )
    factory = init_db("sqlite:///:memory:")
    return HardwareMonitor(cfg, factory)


def test_pump_success_resets_failures(monitor: HardwareMonitor) -> None:
    monitor.record_pump_failure("p1", "fail")
    monitor.record_pump_failure("p1", "fail")
    monitor.record_pump_success("p1")
    rows = monitor.get_all()
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].consecutive_failures == 0


def test_pump_failures_reach_fault_at_threshold(monitor: HardwareMonitor) -> None:
    for _ in range(3):
        monitor.record_pump_failure("p1", "timeout")
    row = monitor.get_all()[0]
    assert row.status == "fault"
    assert row.consecutive_failures == 3


def test_verify_mismatch_fault_at_lower_threshold(monitor: HardwareMonitor) -> None:
    monitor.record_pump_failure("p1", "verify failed", verify_mismatch=True)
    monitor.record_pump_failure("p1", "verify failed", verify_mismatch=True)
    row = monitor.get_all()[0]
    assert row.status == "fault"


def test_sensor_message_updates_ok(monitor: HardwareMonitor) -> None:
    rain = RainState(True, 1.0, 0.95, "mqtt", NOW, water_present=False)
    monitor.record_sensor_message(rain)
    row = monitor.get_all()[0]
    assert row.component_id == "mqtt_sensor"
    assert row.status == "ok"


def test_sensor_stale_when_no_confidence(monitor: HardwareMonitor) -> None:
    rain = RainState(False, 0, 0.0, "mqtt", NOW)
    monitor.check_sensor_stale(rain)
    row = monitor.get_all()[0]
    assert row.status == "fault"


def test_status_summary_includes_local_timestamp(monitor: HardwareMonitor) -> None:
    monitor.record_pump_success("p1")
    summary = monitor.status_summary(timezone="America/New_York")
    assert len(summary) == 1
    assert summary[0]["last_ok_at_local"] is not None
    assert "T" not in summary[0]["last_ok_at_local"]
