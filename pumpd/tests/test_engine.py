"""Rules engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import DutyCycleConfig, RulesConfig
from app.engine import (
    HourlyForecast,
    PumpPhase,
    RainState,
    SafetyFlags,
    evaluate,
    forecast_is_raining,
    should_preemptive_start,
)

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _rules(**kwargs) -> RulesConfig:
    base = RulesConfig(
        evaluate_minutes=10,
        precip_probability_threshold=70,
        precip_amount_threshold_mm=2.0,
        lookahead_hours=2,
        post_rain_drain_minutes=30,
        sensor_dry_minutes=10,
        manual_revert_hours=4,
        duty_cycle=DutyCycleConfig(enabled=False, on_minutes=10, off_minutes=20),
    )
    return base.model_copy(update=kwargs)


def _pump(**kwargs) -> PumpPhase:
    defaults = dict(
        name="north_pump",
        enabled=True,
        phase="idle",
        mode="auto",
        device_on=False,
        duty_on=True,
        runtime_continuous_min=0,
        cooldown_until=None,
        manual_revert_at=None,
    )
    defaults.update(kwargs)
    return PumpPhase(**defaults)


def _forecast(hour_offset: int, pop: float, rain: float) -> HourlyForecast:
    return HourlyForecast(hour_ts=NOW + timedelta(hours=hour_offset), pop_pct=pop, rain_mm=rain)


def test_preemptive_start_requires_any_hour_pop_and_sum_rain() -> None:
    window = [_forecast(0, 80, 1.0), _forecast(1, 50, 1.5)]
    ok, reason = should_preemptive_start(window, NOW, 2, 70, 2.0)
    assert ok is True
    assert "pre-emptive" in reason

    window_fail = [_forecast(0, 60, 1.0), _forecast(1, 50, 0.5)]
    ok2, _ = should_preemptive_start(window_fail, NOW, 2, 70, 2.0)
    assert ok2 is False


def test_preemptive_start_triggers_pump() -> None:
    window = [_forecast(0, 82, 2.5), _forecast(1, 40, 1.0)]
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=window,
        pumps=[_pump()],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_on" for c in result.commands)


def test_post_rain_drain_runs_continuously() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    started = NOW - timedelta(minutes=10)
    pump = _pump(phase="post_rain_drain", device_on=True, post_rain_drain_started_at=started)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert result.pumps[0].device_on is True
    assert not any(c.action == "turn_off" for c in result.commands)


def test_post_rain_drain_completes() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(
        phase="post_rain_drain",
        device_on=True,
        post_rain_drain_started_at=NOW - timedelta(minutes=31),
    )
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_off" for c in result.commands)


def test_duty_cycle_not_applied_during_post_rain_drain() -> None:
    rules = _rules(duty_cycle=DutyCycleConfig(enabled=True, on_minutes=10, off_minutes=20))
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(
        phase="post_rain_drain",
        device_on=True,
        post_rain_drain_started_at=NOW - timedelta(minutes=5),
        duty_on=False,
    )
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=rules,
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert result.pumps[0].device_on is True


def test_sensor_overrides_forecast_for_raining() -> None:
    rain = RainState(True, 2.0, 0.95, "mqtt", NOW)
    pump = _pump(phase="running", device_on=False)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_on" for c in result.commands)


def test_stale_forecast_watchdog_turns_off() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(phase="running", device_on=True)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(stale_forecast=True),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_off" for c in result.commands)


def test_watchdog_mqtt_exception_keeps_running() -> None:
    rain = RainState(True, 1.0, 0.9, "mqtt", NOW)
    pump = _pump(phase="running", device_on=True)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(stale_forecast=True, mqtt_stale_override=True),
        max_runtime_minutes=60,
    )
    assert not any(c.action == "turn_off" for c in result.commands)


def test_max_runtime_cutoff() -> None:
    rain = RainState(True, 1.0, 0.9, "mqtt", NOW)
    pump = _pump(phase="running", device_on=True, runtime_continuous_min=60)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
        min_cooldown_minutes=15,
    )
    assert any(c.action == "turn_off" for c in result.commands)
    assert result.pumps[0].cooldown_until is not None


def test_manual_on_overrides_automation() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(mode="manual_on", device_on=False)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_on" for c in result.commands)


def test_stale_forecast_overrides_manual_on_without_approval() -> None:
    """Safety hard-stops apply to manual mode unless safety override is approved."""
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(mode="manual_on", device_on=True, safety_override_approved=False)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(stale_forecast=True),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_off" for c in result.commands)


def test_manual_on_with_safety_override_bypasses_stale_forecast() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(mode="manual_on", device_on=True, safety_override_approved=True)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(stale_forecast=True),
        max_runtime_minutes=60,
    )
    assert not any(c.action == "turn_off" for c in result.commands)


def test_mqtt_dry_overrides_forecast_rain() -> None:
    """Confident MQTT not-raining overrides forecast rain for continue/stop decisions."""
    rain = RainState(False, 0, 0.95, "mqtt", NOW)
    window = [_forecast(0, 90, 3.0)]
    pump = _pump(phase="running", device_on=True)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=window,
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
        mqtt_min_confidence=0.8,
    )
    assert result.pumps[0].phase == "post_rain_drain" or any(
        c.action == "turn_off" for c in result.commands
    ) or result.pumps[0].sensor_dry_since is not None


def test_mqtt_starts_from_idle() -> None:
    rain = RainState(True, 1.5, 0.95, "mqtt", NOW)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[_pump()],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
        mqtt_min_confidence=0.8,
    )
    assert any(c.action == "turn_on" for c in result.commands)


def test_sensor_dry_stops_post_rain_drain() -> None:
    rain = RainState(False, 0, 0.95, "mqtt", NOW)
    dry_since = NOW - timedelta(minutes=11)
    pump = _pump(
        phase="post_rain_drain",
        device_on=True,
        post_rain_drain_started_at=NOW - timedelta(minutes=5),
        sensor_dry_since=dry_since,
    )
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
        mqtt_min_confidence=0.8,
    )
    assert any(c.action == "turn_off" for c in result.commands)
    assert result.pumps[0].phase == "idle"


def test_manual_off_overrides_automation() -> None:
    rain = RainState(True, 2.0, 0.9, "mqtt", NOW)
    pump = _pump(mode="manual_off", device_on=True)
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert any(c.action == "turn_off" for c in result.commands)


def test_manual_revert_to_auto() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    pump = _pump(
        mode="manual_on",
        device_on=True,
        manual_revert_at=NOW - timedelta(minutes=1),
    )
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert result.pumps[0].mode == "auto"


def test_forecast_is_raining_current_hour() -> None:
    window = [_forecast(0, 75, 0.05)]
    assert forecast_is_raining(window, NOW, 70) is True


def test_cooldown_blocks_auto_start() -> None:
    rain = RainState(False, 0, 0.7, "forecast", NOW)
    window = [_forecast(0, 90, 3.0)]
    pump = _pump(cooldown_until=NOW + timedelta(minutes=10))
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=window,
        pumps=[pump],
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    assert not any(c.action == "turn_on" for c in result.commands)


def test_multi_pump_independent() -> None:
    rain = RainState(True, 1.0, 0.9, "mqtt", NOW)
    pumps = [
        _pump(name="p1", phase="running", device_on=True),
        _pump(name="p2", phase="idle", device_on=False, enabled=False),
    ]
    result = evaluate(
        now=NOW,
        rain=rain,
        forecast_window=[],
        pumps=pumps,
        rules=_rules(),
        safety=SafetyFlags(),
        max_runtime_minutes=60,
    )
    p2_commands = [c for c in result.commands if c.pump_name == "p2"]
    assert p2_commands == []
