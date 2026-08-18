"""Manual control duration, revert, and environmental preemption."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import RulesConfig
from app.engine import HourlyForecast, PumpPhase, RainState, SafetyFlags, evaluate
from app.manual_control import (
    ManualContext,
    ManualEnvSnapshot,
    check_manual_expiry,
    compute_manual_revert_at,
    dump_manual_context,
    parse_manual_context,
    resolve_manual_duration_minutes,
    should_preempt_manual,
)


def test_resolve_manual_duration_defaults() -> None:
    assert resolve_manual_duration_minutes(hours=0, minutes=0, default_minutes=5) == 5
    assert resolve_manual_duration_minutes(hours=1, minutes=30, default_minutes=5) == 90


def test_manual_context_roundtrip() -> None:
    ctx = ManualContext(
        device_on_before=False,
        revert_kind="duration",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    parsed = parse_manual_context(dump_manual_context(ctx))
    assert parsed == ctx


def test_should_preempt_when_rain_starts() -> None:
    ctx = ManualContext(
        device_on_before=True,
        revert_kind="duration",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    assert should_preempt_manual(ctx, is_raining_now=True, preempt_now=False, water_present=None)


def test_manual_timeout_restore_prior_state() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    ctx = ManualContext(
        device_on_before=False,
        revert_kind="duration",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    pump = PumpPhase(
        name="p1",
        enabled=True,
        phase="idle",
        mode="manual_on",
        device_on=True,
        duty_on=True,
        runtime_continuous_min=0,
        cooldown_until=None,
        manual_revert_at=now - timedelta(minutes=1),
        manual_context=ctx,
    )
    result = evaluate(
        now=now,
        rain=RainState(False, 0.0, 0.5, "forecast", now),
        forecast_window=[],
        pumps=[pump],
        rules=RulesConfig(),
        safety=SafetyFlags(),
        max_runtime_by_pump={"p1": 180},
    )
    assert result.pumps[0].mode == "auto"
    assert result.pumps[0].device_on is False
    assert result.commands[0].action == "turn_off"


def test_manual_env_preempt_runs_automation() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    ctx = ManualContext(
        device_on_before=False,
        revert_kind="duration",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    pump = PumpPhase(
        name="p1",
        enabled=True,
        phase="idle",
        mode="manual_off",
        device_on=False,
        duty_on=True,
        runtime_continuous_min=0,
        cooldown_until=None,
        manual_revert_at=now + timedelta(hours=1),
        manual_context=ctx,
    )
    hour = now.replace(minute=0, second=0, microsecond=0)
    forecast = [
        HourlyForecast(hour_ts=hour, pop_pct=90.0, rain_mm=5.0),
        HourlyForecast(hour_ts=hour + timedelta(hours=1), pop_pct=90.0, rain_mm=5.0),
    ]
    result = evaluate(
        now=now,
        rain=RainState(True, 1.0, 0.9, "forecast", now),
        forecast_window=forecast,
        pumps=[pump],
        rules=RulesConfig(precip_probability_threshold=70, precip_amount_threshold_mm=2.0),
        safety=SafetyFlags(),
        max_runtime_by_pump={"p1": 180},
    )
    assert result.pumps[0].mode == "auto"
    assert result.commands[0].action == "turn_on"


def test_check_manual_expiry_timeout() -> None:
    now = datetime.now(UTC)
    ctx = ManualContext(
        device_on_before=True,
        revert_kind="duration",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    assert (
        check_manual_expiry(
            mode="manual_on",
            manual_revert_at=now - timedelta(seconds=1),
            manual_context=ctx,
            now=now,
            is_raining_now=False,
            preempt_now=False,
            water_present=None,
        )
        == "timeout"
    )


def test_until_auto_does_not_timeout() -> None:
    now = datetime.now(UTC)
    ctx = ManualContext(
        device_on_before=True,
        revert_kind="until_auto",
        env=ManualEnvSnapshot(is_raining=False, preempt=False, water_present=None),
    )
    assert (
        check_manual_expiry(
            mode="manual_on",
            manual_revert_at=now - timedelta(hours=1),
            manual_context=ctx,
            now=now,
            is_raining_now=False,
            preempt_now=False,
            water_present=None,
        )
        is None
    )


def test_compute_until_auto_revert() -> None:
    now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    revert = compute_manual_revert_at(
        now=now,
        revert_kind="until_auto",
        duration_minutes=30,
        evaluate_minutes=10,
    )
    assert revert == now + timedelta(minutes=10)
