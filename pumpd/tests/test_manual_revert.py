"""Manual mode auto-revert timing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.devices.base import CommandResult
from app.engine import _as_utc
from app.config import RulesConfig, load_config
from app.db import init_db
from app.manual_control import ManualContext, ManualEnvSnapshot, dump_manual_context, parse_manual_context
from app.models import EventRow, PumpStateRow
from app.service import PumpService


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PumpService:
    import shutil

    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = load_config(config_path)
    cfg.database_url = f"sqlite:///{tmp_path / 'test.db'}"
    cfg.rules = RulesConfig(manual_revert_minutes=5)

    session_factory = init_db(cfg.database_url)
    svc = PumpService(cfg, session_factory, config_path=str(config_path))
    svc._ensure_pump_rows()
    monkeypatch.setattr(svc, "run_evaluation", AsyncMock(return_value=None))
    ok = CommandResult(success=True, adapter="mock", message="ok")
    monkeypatch.setattr(svc, "_manual_device_command", AsyncMock(return_value=ok))
    monkeypatch.setattr(svc, "_safety_active", lambda: False)
    return svc


@pytest.mark.asyncio
async def test_set_pump_mode_stores_manual_context(service: PumpService) -> None:
    await service.set_pump_mode(
        "north_pump",
        "manual_on",
        manual_hours=1,
        manual_minutes=15,
    )
    row = service._get_pump_row("north_pump")
    assert row is not None
    ctx = parse_manual_context(row.manual_context_json)
    assert ctx is not None
    assert ctx.device_on_before is False
    assert ctx.revert_kind == "duration"
    delta = (_as_utc(row.manual_revert_at) - datetime.now(UTC)).total_seconds()
    assert 70 * 60 <= delta <= 80 * 60


@pytest.mark.asyncio
async def test_set_pump_mode_until_auto(service: PumpService) -> None:
    before = datetime.now(UTC)
    await service.set_pump_mode("north_pump", "manual_off", manual_until_auto=True)
    row = service._get_pump_row("north_pump")
    assert row is not None
    ctx = parse_manual_context(row.manual_context_json)
    assert ctx is not None
    assert ctx.revert_kind == "until_auto"
    delta = (_as_utc(row.manual_revert_at) - before).total_seconds()
    assert 9 * 60 <= delta <= 11 * 60


@pytest.mark.asyncio
async def test_set_pump_mode_auto_clears_revert(service: PumpService) -> None:
    await service.set_pump_mode("north_pump", "manual_on")
    await service.set_pump_mode("north_pump", "auto")

    row = service._get_pump_row("north_pump")
    assert row is not None
    assert row.mode == "auto"
    assert row.manual_revert_at is None
    assert "north_pump" not in service._manual_revert_tasks


def test_config_default_manual_revert_minutes() -> None:
    assert RulesConfig().manual_revert_minutes == 5


def test_config_migrates_manual_revert_hours() -> None:
    rules = RulesConfig.model_validate({"manual_revert_hours": 4})
    assert rules.manual_revert_minutes == 240


@pytest.mark.asyncio
async def test_restore_clamps_old_manual_revert(service: PumpService) -> None:
    now = datetime.now(UTC)
    with service.session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        row.mode = "manual_off"
        # Legacy 4h schedule from ~10 minutes ago.
        row.manual_revert_at = now + timedelta(hours=4) - timedelta(minutes=10)
        session.commit()

    service._restore_manual_revert_schedules()
    row = service._get_pump_row("north_pump")
    assert row is not None
    assert _as_utc(row.manual_revert_at) <= now + timedelta(seconds=1)
    assert "north_pump" in service._manual_revert_tasks


@pytest.mark.asyncio
async def test_restore_uses_last_manual_mode_event(service: PumpService) -> None:
    now = datetime.now(UTC)
    with service.session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        row.mode = "manual_off"
        row.manual_revert_at = now + timedelta(minutes=3)
        session.add(
            EventRow(
                ts=now - timedelta(minutes=10),
                pump_name="north_pump",
                event_type="mode_change",
                reason="mode set to manual_off",
                details_json='{"mode": "manual_off", "previous_mode": "manual_on"}',
            )
        )
        session.commit()

    service._restore_manual_revert_schedules()
    row = service._get_pump_row("north_pump")
    assert row is not None
    assert _as_utc(row.manual_revert_at) <= now + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_restore_preserves_duration_manual_context(service: PumpService) -> None:
    now = datetime.now(UTC)
    revert_at = now + timedelta(minutes=90)
    ctx = ManualContext(
        device_on_before=False,
        revert_kind="duration",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    with service.session_factory() as session:
        row = session.get(PumpStateRow, "north_pump")
        assert row is not None
        row.mode = "manual_on"
        row.manual_revert_at = revert_at
        row.manual_context_json = dump_manual_context(ctx)
        session.add(
            EventRow(
                ts=now - timedelta(minutes=2),
                pump_name="north_pump",
                event_type="mode_change",
                reason="mode set to manual_on",
                details_json='{"mode": "manual_on", "manual_duration_minutes": 90}',
            )
        )
        session.commit()

    service._restore_manual_revert_schedules()
    row = service._get_pump_row("north_pump")
    assert row is not None
    restored = _as_utc(row.manual_revert_at)
    assert restored >= now + timedelta(minutes=85)
    assert "north_pump" in service._manual_revert_tasks
