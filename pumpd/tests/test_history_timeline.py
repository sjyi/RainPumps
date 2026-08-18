"""Timeline builder for history panel."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import init_db
from app.history_timeline import (
    build_history_timeline,
    event_state_change,
    extract_state_changes,
    format_timeline_marker,
)
from app.models import EventRow


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    db_path = tmp_path / "test.db"
    cfg = load_config("config.example.yaml")
    cfg.database_url = f"sqlite:///{db_path}"

    monkeypatch.setattr("app.main.load_config", lambda _path="config.yaml": cfg)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))

    from app.main import create_app

    app = create_app("config.example.yaml")
    with TestClient(app) as test_client:
        yield test_client


def _event(
    pump: str,
    event_type: str,
    ts: datetime,
    reason: str = "",
    details_json: str | None = None,
) -> EventRow:
    return EventRow(
        ts=ts,
        pump_name=pump,
        event_type=event_type,
        reason=reason,
        details_json=details_json,
    )


def test_extract_state_changes_turn_on_off() -> None:
    now = datetime.now(UTC)
    events = [
        _event("pump_a", "turn_on", now - timedelta(hours=2)),
        _event("pump_a", "turn_off", now - timedelta(hours=1)),
    ]
    changes = extract_state_changes(events)
    assert changes["pump_a"] == [(events[0].ts, True), (events[1].ts, False)]


def test_failed_turn_off_still_counts_as_off() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=6)
    turned_on = start + timedelta(hours=1)
    turned_off = start + timedelta(hours=2)
    events = [
        _event(
            "1st_fl_roof_302_switch_3",
            "turn_on",
            turned_on,
            details_json='{"success": true}',
        ),
        _event(
            "1st_fl_roof_302_switch_3",
            "turn_off",
            turned_off,
            details_json='{"success": false, "message": "verify failed on tuya_cloud"}',
        ),
    ]
    pump_cards = [
        {
            "name": "1st_fl_roof_302_switch_3",
            "device_id": "dev302",
            "device_backend": "tuya",
            "switch_code": "switch_3",
        }
    ]
    timeline = build_history_timeline(
        events,
        pump_cards,
        range_start=start,
        range_end=now,
        current_state={"1st_fl_roof_302_switch_3": False},
    )
    segments = timeline["devices"][0]["switches"][0]["segments"]
    assert segments[-1]["kind"] == "off"
    off_start = datetime.fromisoformat(segments[-1]["start"])
    on_end = datetime.fromisoformat(segments[-2]["end"])
    assert on_end == turned_off
    assert off_start == turned_off


def test_failed_turn_on_is_ignored() -> None:
    now = datetime.now(UTC)
    events = [
        _event(
            "smart_switch",
            "turn_on",
            now - timedelta(hours=1),
            details_json='{"success": false, "message": "timeout"}',
        ),
    ]
    assert extract_state_changes(events) == {}
    assert event_state_change(events[0]) is None


def test_failed_turn_on_inferred_when_followed_by_turn_off() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=4)
    turned_on = start + timedelta(hours=1, minutes=44)
    turned_off = start + timedelta(hours=2, minutes=33)
    events = [
        _event(
            "1st_fl_roof_101_switch_3",
            "turn_on",
            turned_on,
            reason="rain detected now (forecast/MQTT)",
            details_json='{"success": false, "message": "verify failed on tuya_cloud"}',
        ),
        _event(
            "1st_fl_roof_101_switch_3",
            "turn_off",
            turned_off,
            reason="max runtime 60min exceeded",
            details_json='{"success": true}',
        ),
    ]
    pump_cards = [
        {
            "name": "1st_fl_roof_101_switch_3",
            "device_id": "dev101",
            "device_backend": "tuya",
            "switch_code": "switch_3",
        }
    ]
    timeline = build_history_timeline(
        events,
        pump_cards,
        range_start=start,
        range_end=now,
        current_state={"1st_fl_roof_101_switch_3": False},
    )
    segments = timeline["devices"][0]["switches"][0]["segments"]
    on_seg = next(seg for seg in segments if seg["kind"] == "on")
    assert datetime.fromisoformat(on_seg["start"]) == turned_on
    assert datetime.fromisoformat(on_seg["end"]) == turned_off
    markers = timeline["markers"]
    assert len(markers) == 2
    failed_on = next(m for m in markers if m["event_type"] == "turn_on")
    assert failed_on["failed"] is True


def test_all_turn_events_become_markers() -> None:
    now = datetime.now(UTC)
    events = [
        _event(
            "pump_a",
            "turn_on",
            now - timedelta(hours=1),
            details_json='{"success": false}',
        ),
    ]
    marker = format_timeline_marker(events[0])
    assert marker is not None
    assert marker["failed"] is True


def test_timeline_matches_current_device_off() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=4)
    turned_on = start + timedelta(hours=1)
    updated = now - timedelta(minutes=20)
    events = [
        _event("smart_switch", "turn_on", turned_on, details_json='{"success": true}'),
    ]
    pump_cards = [
        {
            "name": "smart_switch",
            "device_id": "dev1",
            "device_backend": "tuya",
            "switch_code": "switch_1",
        }
    ]
    timeline = build_history_timeline(
        events,
        pump_cards,
        range_start=start,
        range_end=now,
        current_state={"smart_switch": False},
        updated_at_by_pump={"smart_switch": updated},
    )
    switch_segments = timeline["devices"][0]["switches"][0]["segments"]
    assert switch_segments[-1]["kind"] == "off"


def test_stale_updated_at_still_ends_off() -> None:
    """updated_at before last turn_on must not leave timeline showing ON at now."""
    now = datetime.now(UTC)
    start = now - timedelta(hours=4)
    turned_on = now - timedelta(hours=1)
    stale_updated = now - timedelta(hours=3)
    events = [
        _event(
            "1st_fl_roof_302_switch_3",
            "turn_on",
            turned_on,
            details_json='{"success": true}',
        ),
    ]
    pump_cards = [
        {
            "name": "1st_fl_roof_302_switch_3",
            "device_id": "dev302",
            "device_backend": "tuya",
            "switch_code": "switch_3",
        }
    ]
    timeline = build_history_timeline(
        events,
        pump_cards,
        range_start=start,
        range_end=now,
        current_state={"1st_fl_roof_302_switch_3": False},
        updated_at_by_pump={"1st_fl_roof_302_switch_3": stale_updated},
    )
    segments = timeline["devices"][0]["switches"][0]["segments"]
    assert segments[-1]["kind"] == "off"
    assert timeline["devices"][0]["switches"][0]["device_on"] is False


def test_device_on_change_event_updates_history() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=2)
    events = [
        _event(
            "1st_fl_roof_302_switch_3",
            "turn_on",
            start + timedelta(minutes=10),
            details_json='{"success": true}',
        ),
        _event(
            "1st_fl_roof_302_switch_3",
            "device_on_change",
            start + timedelta(minutes=40),
            reason="device_on=False",
            details_json='{"previous": true}',
        ),
    ]
    pump_cards = [
        {
            "name": "1st_fl_roof_302_switch_3",
            "device_id": "dev302",
            "device_backend": "tuya",
            "switch_code": "switch_3",
        }
    ]
    timeline = build_history_timeline(
        events,
        pump_cards,
        range_start=start,
        range_end=now,
        current_state={"1st_fl_roof_302_switch_3": False},
    )
    segments = timeline["devices"][0]["switches"][0]["segments"]
    assert segments[-1]["kind"] == "off"


def test_reconcile_commanded_off_parsed() -> None:
    now = datetime.now(UTC)
    events = [
        _event(
            "pump_a",
            "reconcile",
            now - timedelta(minutes=5),
            reason="commanded off: DB intent off, device was on",
        ),
    ]
    assert extract_state_changes(events)["pump_a"] == [(events[0].ts, False)]


def test_system_timeline_collapses_long_idle() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=10)
    events = [
        _event("pump_a", "turn_on", start + timedelta(minutes=10)),
        _event("pump_a", "turn_off", start + timedelta(minutes=20)),
        _event("pump_a", "turn_on", start + timedelta(hours=9)),
    ]
    pump_cards = [
        {
            "name": "pump_a",
            "device_id": "dev1",
            "device_backend": "tuya",
            "switch_code": "switch_1",
        }
    ]
    timeline = build_history_timeline(
        events,
        pump_cards,
        range_start=start,
        range_end=now,
        idle_gap_minutes=30,
    )
    kinds = [seg["kind"] for seg in timeline["system"]["segments"]]
    assert "gap" in kinds
    gap = next(seg for seg in timeline["system"]["segments"] if seg["kind"] == "gap")
    assert gap["label"]


def test_device_expandable_when_multi_switch() -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=2)
    pump_cards = [
        {
            "name": "plug_a_sw1",
            "device_id": "plug1",
            "device_backend": "tuya",
            "switch_code": "switch_1",
        },
        {
            "name": "plug_a_sw2",
            "device_id": "plug1",
            "device_backend": "tuya",
            "switch_code": "switch_2",
        },
    ]
    timeline = build_history_timeline([], pump_cards, range_start=start, range_end=now)
    assert len(timeline["devices"]) == 1
    assert timeline["devices"][0]["expandable"] is True
    assert len(timeline["devices"][0]["switches"]) == 2


def test_timeline_api(client: TestClient) -> None:
    resp = client.get("/api/history/timeline")
    assert resp.status_code == 200
    data = resp.json()
    assert "system" in data
    assert "devices" in data
    assert "markers" in data


def test_admin_ui_has_timeline(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert "history-timeline-root" in response.text
    assert "history_timeline.js" in response.text
