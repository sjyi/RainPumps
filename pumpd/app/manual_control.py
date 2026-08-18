"""Manual pump control — duration, revert, and environmental preemption."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

ManualRevertKind = Literal["duration", "until_auto"]
ManualExpiryKind = Literal["timeout", "env_preempt"]


@dataclass(frozen=True)
class ManualEnvSnapshot:
    is_raining: bool
    preempt: bool
    water_present: bool | None = None


@dataclass(frozen=True)
class ManualContext:
    device_on_before: bool
    revert_kind: ManualRevertKind
    env: ManualEnvSnapshot


def parse_manual_context(raw: str | None) -> ManualContext | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    env_raw = data.get("env") if isinstance(data.get("env"), dict) else {}
    try:
        return ManualContext(
            device_on_before=bool(data["device_on_before"]),
            revert_kind=data.get("revert_kind", "duration"),
            env=ManualEnvSnapshot(
                is_raining=bool(env_raw.get("is_raining", False)),
                preempt=bool(env_raw.get("preempt", False)),
                water_present=env_raw.get("water_present"),
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def dump_manual_context(ctx: ManualContext) -> str:
    payload: dict[str, Any] = {
        "device_on_before": ctx.device_on_before,
        "revert_kind": ctx.revert_kind,
        "env": asdict(ctx.env),
    }
    return json.dumps(payload, separators=(",", ":"))


def resolve_manual_duration_minutes(
    *,
    hours: int = 0,
    minutes: int = 0,
    total_minutes: int | None = None,
    default_minutes: int,
) -> int:
    if total_minutes is not None:
        resolved = int(total_minutes)
    else:
        resolved = max(0, int(hours)) * 60 + max(0, int(minutes))
    if resolved <= 0:
        resolved = default_minutes
    return max(1, resolved)


def compute_manual_revert_at(
    *,
    now: datetime,
    revert_kind: ManualRevertKind,
    duration_minutes: int,
    evaluate_minutes: int,
) -> datetime:
    if revert_kind == "until_auto":
        return now + timedelta(minutes=max(1, evaluate_minutes))
    return now + timedelta(minutes=max(1, duration_minutes))


def should_preempt_manual(
    ctx: ManualContext,
    *,
    is_raining_now: bool,
    preempt_now: bool,
    water_present: bool | None,
) -> bool:
    env = ctx.env
    if water_present is True and env.water_present is not True:
        return True
    if is_raining_now and not env.is_raining:
        return True
    if preempt_now and not env.preempt:
        return True
    return False


def check_manual_expiry(
    *,
    mode: str,
    manual_revert_at: datetime | None,
    manual_context: ManualContext | None,
    now: datetime,
    is_raining_now: bool,
    preempt_now: bool,
    water_present: bool | None,
) -> ManualExpiryKind | None:
    if mode not in ("manual_on", "manual_off"):
        return None
    if manual_context and should_preempt_manual(
        manual_context,
        is_raining_now=is_raining_now,
        preempt_now=preempt_now,
        water_present=water_present,
    ):
        return "env_preempt"
    if manual_revert_at is not None and now >= manual_revert_at:
        if manual_context and manual_context.revert_kind == "until_auto":
            return None
        return "timeout"
    return None
