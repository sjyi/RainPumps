"""Pure rules engine — no I/O."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.config import PumpMode, PumpPhaseName, RulesConfig

Phase = PumpPhaseName
Mode = PumpMode


@dataclass(frozen=True)
class HourlyForecast:
    hour_ts: datetime
    pop_pct: float
    rain_mm: float


@dataclass(frozen=True)
class RainState:
    is_raining: bool
    rate_mm_h: float
    confidence: float
    source: Literal["forecast", "mqtt"]
    ts: datetime
    water_present: bool | None = None  # Phase 2 float switch


@dataclass
class PumpPhase:
    name: str
    enabled: bool
    phase: Phase
    mode: Mode
    device_on: bool
    duty_on: bool
    runtime_continuous_min: int
    cooldown_until: datetime | None
    manual_revert_at: datetime | None
    post_rain_drain_started_at: datetime | None = None
    sensor_dry_since: datetime | None = None
    duty_cycle_started_at: datetime | None = None
    safety_tripped: bool = False
    safety_override_approved: bool = False


@dataclass(frozen=True)
class SafetyFlags:
    stale_forecast: bool = False
    engine_watchdog: bool = False
    mqtt_stale_override: bool = False


@dataclass(frozen=True)
class PumpCommand:
    pump_name: str
    action: Literal["turn_on", "turn_off", "no_op"]
    reason: str
    new_phase: Phase | None = None
    notify: bool = False


@dataclass
class EvaluateResult:
    commands: list[PumpCommand]
    pumps: list[PumpPhase]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def forecast_is_raining(
    forecast_window: list[HourlyForecast],
    now: datetime,
    pop_threshold: int,
) -> bool:
    """Forecast-only rain signal for current + next 1 h."""
    now_utc = _as_utc(now)
    end = now_utc + timedelta(hours=1)
    window_start = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    for row in forecast_window:
        hour = _as_utc(row.hour_ts)
        if hour < window_start:
            continue
        if hour > end:
            break
        if row.pop_pct >= pop_threshold or row.rain_mm >= 0.1:
            return True
    return False


def effective_raining(
    rain: RainState,
    forecast_window: list[HourlyForecast],
    now: datetime,
    pop_threshold: int,
    mqtt_min_confidence: float,
) -> bool:
    """Resolve whether it is raining now; MQTT overrides forecast when confident."""
    if rain.source == "mqtt" and rain.confidence >= mqtt_min_confidence:
        return rain.is_raining
    if rain.is_raining:
        return True
    return forecast_is_raining(forecast_window, now, pop_threshold)


def should_preemptive_start(
    forecast_window: list[HourlyForecast],
    now: datetime,
    lookahead_hours: int,
    pop_threshold: int,
    amount_threshold_mm: float,
) -> tuple[bool, str]:
    now_utc = _as_utc(now)
    window_end = now_utc + timedelta(hours=lookahead_hours)
    rows = [h for h in forecast_window if now_utc <= _as_utc(h.hour_ts) < window_end]
    if not rows:
        return False, "no forecast in lookahead window"
    any_pop = any(h.pop_pct >= pop_threshold for h in rows)
    rain_sum = sum(h.rain_mm for h in rows)
    if any_pop and rain_sum >= amount_threshold_mm:
        return True, f"pre-emptive: max pop in window, {rain_sum:.1f}mm sum in {lookahead_hours}h"
    return False, f"below thresholds (sum={rain_sum:.1f}mm)"


def _in_cooldown(pump: PumpPhase, now: datetime) -> bool:
    return pump.cooldown_until is not None and now < pump.cooldown_until


def _apply_manual_revert(pump: PumpPhase, now: datetime) -> PumpPhase:
    if (
        pump.mode in ("manual_on", "manual_off")
        and pump.manual_revert_at
        and now >= pump.manual_revert_at
    ):
        return replace(pump, mode="auto", manual_revert_at=None, safety_override_approved=False)
    return pump


def _duty_should_be_on(pump: PumpPhase, now: datetime, rules: RulesConfig) -> bool:
    dc = rules.duty_cycle
    if not dc.enabled:
        return True
    if pump.duty_cycle_started_at is None:
        return pump.duty_on
    elapsed = (now - pump.duty_cycle_started_at).total_seconds() / 60
    cycle = dc.on_minutes + dc.off_minutes
    if cycle <= 0:
        return True
    pos = elapsed % cycle
    return pos < dc.on_minutes


def _advance_duty(pump: PumpPhase, want_on: bool, now: datetime) -> PumpPhase:
    if pump.duty_on != want_on:
        return replace(pump, duty_on=want_on, duty_cycle_started_at=now)
    if pump.duty_cycle_started_at is None:
        return replace(pump, duty_cycle_started_at=now)
    return pump


def _safety_trip(
    pump: PumpPhase,
    safety: SafetyFlags,
    max_runtime_minutes: int,
) -> tuple[bool, str]:
    if safety.engine_watchdog:
        return True, "engine evaluation watchdog"
    if safety.stale_forecast and not safety.mqtt_stale_override:
        return True, "stale forecast watchdog"
    if pump.runtime_continuous_min >= max_runtime_minutes and pump.device_on:
        return True, f"max runtime {max_runtime_minutes}min exceeded"
    return False, ""


def evaluate(
    *,
    now: datetime,
    rain: RainState,
    forecast_window: list[HourlyForecast],
    pumps: list[PumpPhase],
    rules: RulesConfig,
    safety: SafetyFlags,
    max_runtime_minutes: int,
    min_cooldown_minutes: int = 15,
    mqtt_min_confidence: float = 0.8,
) -> EvaluateResult:
    """Evaluate all pumps and return commands plus updated state."""
    commands: list[PumpCommand] = []
    updated: list[PumpPhase] = []
    preempt, preempt_reason = should_preemptive_start(
        forecast_window,
        now,
        rules.lookahead_hours,
        rules.precip_probability_threshold,
        rules.precip_amount_threshold_mm,
    )
    is_raining_now = effective_raining(
        rain,
        forecast_window,
        now,
        rules.precip_probability_threshold,
        mqtt_min_confidence,
    )
    mqtt_authoritative = rain.source == "mqtt" and rain.confidence >= mqtt_min_confidence

    for pump in pumps:
        p = _apply_manual_revert(pump, now)
        if not p.enabled:
            updated.append(p)
            continue

        want_on = False
        reason = "default off"
        new_phase: Phase | None = None
        notify = False

        manual_overrides_safety = (
            p.mode in ("manual_on", "manual_off") and p.safety_override_approved
        )
        trip, trip_reason = _safety_trip(p, safety, max_runtime_minutes)

        # 1. Safety hard-stops (manual may override with approval)
        if trip and not manual_overrides_safety:
            want_on = False
            reason = trip_reason
            new_phase = "idle"
            notify = True
            if "max runtime" in trip_reason:
                p = replace(
                    p,
                    safety_tripped=True,
                    cooldown_until=now + timedelta(minutes=min_cooldown_minutes),
                )
        elif p.mode == "manual_on":
            want_on = True
            reason = "manual_on override"
        elif p.mode == "manual_off":
            want_on = False
            reason = "manual_off override"
        elif rain.water_present is True:
            want_on = True
            reason = "float switch water present"
            new_phase = "running"
        elif rain.water_present is False and p.phase in ("running", "pre_rain", "post_rain_drain"):
            if not p.sensor_dry_since:
                p = replace(p, sensor_dry_since=now)
            if p.sensor_dry_since and (now - p.sensor_dry_since) >= timedelta(
                minutes=rules.sensor_dry_minutes
            ):
                want_on = False
                reason = "float dry early stop"
                new_phase = "idle"
                p = replace(p, post_rain_drain_started_at=None, sensor_dry_since=None)
            else:
                want_on = p.device_on
                reason = "float dry grace period"
        elif mqtt_authoritative and not rain.is_raining and p.phase in (
            "running",
            "pre_rain",
            "post_rain_drain",
        ):
            if not p.sensor_dry_since:
                p = replace(p, sensor_dry_since=now)
            if p.sensor_dry_since and (now - p.sensor_dry_since) >= timedelta(
                minutes=rules.sensor_dry_minutes
            ):
                want_on = False
                reason = "sensor dry early stop"
                new_phase = "idle"
                p = replace(p, post_rain_drain_started_at=None, sensor_dry_since=None)
            else:
                want_on = p.device_on
                reason = "sensor dry grace period"
        elif mqtt_authoritative and rain.is_raining and p.phase == "idle":
            want_on = True
            reason = "mqtt sensor reports rain"
            new_phase = "running"
        else:
            p = replace(p, sensor_dry_since=None)

            if _in_cooldown(p, now) and p.mode == "auto":
                want_on = False
                reason = "cooldown active"
                new_phase = p.phase
            elif p.phase == "idle":
                if preempt and not _in_cooldown(p, now):
                    want_on = True
                    reason = preempt_reason
                    new_phase = "pre_rain" if not is_raining_now else "running"
                else:
                    want_on = False
                    reason = preempt_reason if not preempt else "idle"
            elif p.phase in ("pre_rain", "running"):
                if is_raining_now or rain.water_present:
                    new_phase = "running"
                    if rules.duty_cycle.enabled:
                        duty_on = _duty_should_be_on(p, now, rules)
                        p = _advance_duty(p, duty_on, now)
                        want_on = duty_on
                        reason = "duty cycle"
                    else:
                        want_on = True
                        reason = "rain active"
                else:
                    new_phase = "post_rain_drain"
                    p = replace(p, phase="post_rain_drain", post_rain_drain_started_at=now)
                    want_on = True
                    reason = "rain ended, post-rain drain started"
            elif p.phase == "post_rain_drain":
                started = p.post_rain_drain_started_at or now
                elapsed = (now - started).total_seconds() / 60
                if is_raining_now:
                    new_phase = "running"
                    want_on = True
                    reason = "rain resumed during drain"
                    p = replace(p, post_rain_drain_started_at=None)
                elif elapsed >= rules.post_rain_drain_minutes:
                    want_on = False
                    reason = "post-rain drain complete"
                    new_phase = "idle"
                    p = replace(p, post_rain_drain_started_at=None)
                else:
                    want_on = True
                    reason = f"post-rain drain ({elapsed:.0f}/{rules.post_rain_drain_minutes} min)"

        if new_phase:
            p = replace(p, phase=new_phase)

        action: Literal["turn_on", "turn_off", "no_op"] = "no_op"
        if want_on and not p.device_on:
            action = "turn_on"
        elif not want_on and p.device_on:
            action = "turn_off"

        if action != "no_op":
            commands.append(
                PumpCommand(
                    pump_name=p.name,
                    action=action,
                    reason=reason,
                    new_phase=p.phase,
                    notify=notify,
                )
            )
            p = replace(p, device_on=want_on)
            if action == "turn_off" and p.safety_tripped:
                p = replace(p, runtime_continuous_min=0, safety_tripped=False)

        updated.append(p)

    return EvaluateResult(commands=commands, pumps=updated)
