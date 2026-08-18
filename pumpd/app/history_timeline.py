"""Build hierarchical ON/OFF timeline segments for the history panel."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.config import AppConfig
from app.engine import _as_utc
from app.models import EventRow
from app.pump_card_groups import group_pump_cards


def _format_duration(delta: timedelta) -> str:
    total_min = int(delta.total_seconds() // 60)
    if total_min < 60:
        return f"{total_min}m"
    hours = total_min // 60
    rem = total_min % 60
    if rem:
        return f"{hours}h {rem}m"
    return f"{hours}h"


def _parse_reconcile_state(reason: str) -> bool | None:
    lowered = reason.lower()
    if "commanded off" in lowered:
        return False
    if "device_on=true" in lowered:
        return True
    if "device_on=false" in lowered:
        return False
    if "device_on=True" in reason:
        return True
    if "device_on=False" in reason:
        return False
    return None


def _parse_event_details(row: EventRow) -> dict[str, Any]:
    if not row.details_json:
        return {}
    try:
        return json.loads(row.details_json)
    except json.JSONDecodeError:
        return {}


def event_state_change(row: EventRow) -> bool | None:
    """Return ON/OFF state after this event, or None if it should be ignored."""
    details = _parse_event_details(row)
    if row.event_type == "turn_on":
        if details.get("success") is False:
            return None
        return True
    if row.event_type == "turn_off":
        # Engine logs turn_off and updates DB intent even when hardware verify fails.
        return False
    if row.event_type == "reconcile":
        return _parse_reconcile_state(row.reason)
    if row.event_type == "device_on_change":
        return _parse_reconcile_state(row.reason)
    return None


def event_is_timeline_marker(row: EventRow) -> bool:
    """All turn_on/turn_off rows appear as markers (failed ones use a distinct style)."""
    return row.event_type in ("turn_on", "turn_off") and bool(row.pump_name)


def format_timeline_marker(row: EventRow) -> dict[str, Any] | None:
    if not event_is_timeline_marker(row):
        return None
    details = _parse_event_details(row)
    failed = details.get("success") is False
    return {
        "ts": _as_utc(row.ts).isoformat(),
        "pump_name": row.pump_name,
        "event_type": row.event_type,
        "action": "ON" if row.event_type == "turn_on" else "OFF",
        "reason": row.reason,
        "failed": failed,
    }


def apply_inferred_on_from_failed_commands(
    events: list[EventRow],
    changes_by_pump: dict[str, list[tuple[datetime, bool]]],
    *,
    max_gap: timedelta = timedelta(hours=3),
) -> dict[str, list[tuple[datetime, bool]]]:
    """When turn_on verify failed but a turn_off follows, infer the pump ran."""
    by_pump: dict[str, list[EventRow]] = {}
    for row in events:
        if not row.pump_name or row.event_type not in ("turn_on", "turn_off"):
            continue
        by_pump.setdefault(row.pump_name, []).append(row)

    result = {pump: list(changes) for pump, changes in changes_by_pump.items()}
    for pump, rows in by_pump.items():
        rows.sort(key=lambda r: r.ts)
        changes = result.setdefault(pump, [])
        for i, row in enumerate(rows):
            if row.event_type != "turn_on":
                continue
            if _parse_event_details(row).get("success") is not False:
                continue
            ts = _as_utc(row.ts)
            for later in rows[i + 1 :]:
                if later.event_type == "turn_off":
                    off_ts = _as_utc(later.ts)
                    if off_ts - ts <= max_gap and not any(t == ts and on for t, on in changes):
                        changes.append((ts, True))
                        changes.sort(key=lambda item: item[0])
                    break
                if later.event_type == "turn_on":
                    if _parse_event_details(later).get("success") is not False:
                        break
    return result


def align_changes_to_current_state(
    changes: list[tuple[datetime, bool]],
    *,
    current: bool,
    range_end: datetime,
    state_updated_at: datetime | None,
) -> list[tuple[datetime, bool]]:
    """Match event-derived history to live device_on at range_end."""
    aligned = list(changes)
    if _state_at_time(aligned, range_end) == current:
        return aligned

    last_ts = aligned[-1][0] if aligned else None
    correction_ts = range_end
    if state_updated_at is not None:
        updated = _as_utc(state_updated_at)
        if updated <= range_end and (last_ts is None or updated >= last_ts):
            correction_ts = updated

    # updated_at moves on every eval; ignore if it predates the last state change.
    if last_ts is not None and correction_ts < last_ts:
        correction_ts = range_end

    if aligned and aligned[-1][0] == correction_ts:
        aligned[-1] = (correction_ts, current)
    else:
        aligned.append((correction_ts, current))
    aligned.sort(key=lambda item: item[0])
    return aligned


def apply_current_pump_states(
    changes_by_pump: dict[str, list[tuple[datetime, bool]]],
    *,
    current_state: dict[str, bool],
    updated_at_by_pump: dict[str, datetime | None],
    range_end: datetime,
) -> dict[str, list[tuple[datetime, bool]]]:
    result: dict[str, list[tuple[datetime, bool]]] = {}
    for pump, current in current_state.items():
        result[pump] = align_changes_to_current_state(
            changes_by_pump.get(pump, []),
            current=current,
            range_end=range_end,
            state_updated_at=updated_at_by_pump.get(pump),
        )
    for pump, changes in changes_by_pump.items():
        if pump not in result:
            result[pump] = list(changes)
    return result


def _state_at_time(changes: list[tuple[datetime, bool]], moment: datetime) -> bool:
    state = False
    for ts, on in changes:
        if ts <= moment:
            state = on
        else:
            break
    return state


def extract_state_changes(events: list[EventRow]) -> dict[str, list[tuple[datetime, bool]]]:
    by_pump: dict[str, list[tuple[datetime, bool]]] = {}
    for row in events:
        pump = row.pump_name
        if not pump:
            continue
        on = event_state_change(row)
        if on is None:
            continue
        by_pump.setdefault(pump, []).append((_as_utc(row.ts), on))
    for changes in by_pump.values():
        changes.sort(key=lambda item: item[0])
    return by_pump


def _merge_adjacent_raw(raw: list[tuple[bool, datetime, datetime]]) -> list[tuple[bool, datetime, datetime]]:
    merged: list[tuple[bool, datetime, datetime]] = []
    for on, start, end in raw:
        if end <= start:
            continue
        if merged and merged[-1][0] == on and merged[-1][2] == start:
            merged[-1] = (on, merged[-1][1], end)
        else:
            merged.append((on, start, end))
    return merged


def _compress_raw(
    raw: list[tuple[bool, datetime, datetime]], idle_gap: timedelta
) -> list[dict[str, Any]]:
    merged = _merge_adjacent_raw(raw)
    segments: list[dict[str, Any]] = []
    for on, start, end in merged:
        if not on and (end - start) >= idle_gap:
            segments.append(
                {
                    "kind": "gap",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "label": _format_duration(end - start),
                }
            )
        else:
            segments.append(
                {
                    "kind": "on" if on else "off",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "label": None,
                }
            )
    return segments


def build_track_segments(
    changes: list[tuple[datetime, bool]],
    range_start: datetime,
    range_end: datetime,
    *,
    idle_gap: timedelta | None = None,
    current_at_end: bool | None = None,
) -> list[dict[str, Any]]:
    if range_end <= range_start:
        return []

    initial = _state_at_time(changes, range_start)
    relevant = [(ts, on) for ts, on in changes if range_start < ts <= range_end]

    raw: list[tuple[bool, datetime, datetime]] = []
    cursor = range_start
    state = initial

    for ts, on in relevant:
        if ts > cursor:
            raw.append((state, cursor, ts))
        state = on
        cursor = ts
    if cursor < range_end:
        raw.append((state, cursor, range_end))
    elif not raw:
        raw.append((initial, range_start, range_end))

    merged = _merge_adjacent_raw(raw)
    if current_at_end is not None and merged:
        on, start, end = merged[-1]
        if on != current_at_end:
            merged[-1] = (current_at_end, start, end)

    if idle_gap is not None:
        return _compress_raw(merged, idle_gap)
    return [
        {
            "kind": "on" if on else "off",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": None,
        }
        for on, start, end in merged
    ]


def merge_or_segments(
    changes_by_pump: dict[str, list[tuple[datetime, bool]]],
    pump_names: list[str],
    range_start: datetime,
    range_end: datetime,
    *,
    idle_gap: timedelta,
    current_at_end: bool | None = None,
) -> list[dict[str, Any]]:
    if not pump_names or range_end <= range_start:
        return []

    boundaries = {range_start, range_end}
    for name in pump_names:
        for ts, _ in changes_by_pump.get(name, []):
            if range_start < ts < range_end:
                boundaries.add(ts)

    points = sorted(boundaries)
    raw: list[tuple[bool, datetime, datetime]] = []
    for start, end in zip(points, points[1:], strict=False):
        if start >= end:
            continue
        any_on = any(_state_at_time(changes_by_pump.get(name, []), start) for name in pump_names)
        raw.append((any_on, start, end))

    merged = _merge_adjacent_raw(raw)
    if current_at_end is not None and merged:
        on, start, end = merged[-1]
        if on != current_at_end:
            merged[-1] = (current_at_end, start, end)
    return _compress_raw(merged, idle_gap)


def _display_pump_label(name: str) -> str:
    return name.replace("_", " ").title()


def _switch_label(card: dict[str, Any]) -> str:
    code = card.get("switch_code") or "switch_1"
    label = card.get("display_label") or _display_pump_label(card["name"])
    if code.replace("_", " ").lower() in label.lower():
        return label
    return f"{label} ({code.replace('_', ' ').title()})"


def build_or_track_segments(
    changes_by_pump: dict[str, list[tuple[datetime, bool]]],
    pump_names: list[str],
    range_start: datetime,
    range_end: datetime,
    *,
    current_at_end: bool | None = None,
) -> list[dict[str, Any]]:
    if not pump_names or range_end <= range_start:
        return []

    boundaries = {range_start, range_end}
    for name in pump_names:
        for ts, _ in changes_by_pump.get(name, []):
            if range_start <= ts <= range_end:
                boundaries.add(ts)

    points = sorted(boundaries)
    raw: list[tuple[bool, datetime, datetime]] = []
    for start, end in zip(points, points[1:], strict=False):
        if start >= end:
            continue
        any_on = any(_state_at_time(changes_by_pump.get(name, []), start) for name in pump_names)
        raw.append((any_on, start, end))

    merged = _merge_adjacent_raw(raw)
    if current_at_end is not None and merged:
        on, start, end = merged[-1]
        if on != current_at_end:
            merged[-1] = (current_at_end, start, end)

    return [
        {
            "kind": "on" if on else "off",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "label": None,
        }
        for on, start, end in merged
    ]



def build_history_timeline(
    events: list[EventRow],
    pump_cards: list[dict[str, Any]],
    *,
    range_start: datetime,
    range_end: datetime,
    idle_gap_minutes: int = 30,
    current_state: dict[str, bool] | None = None,
    updated_at_by_pump: dict[str, datetime | None] | None = None,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    idle_gap = timedelta(minutes=idle_gap_minutes)
    changes_by_pump = extract_state_changes(events)
    changes_by_pump = apply_inferred_on_from_failed_commands(events, changes_by_pump)
    if current_state:
        changes_by_pump = apply_current_pump_states(
            changes_by_pump,
            current_state=current_state,
            updated_at_by_pump=updated_at_by_pump or {},
            range_end=range_end,
        )

    all_pump_names = [card["name"] for card in pump_cards]
    system_current = (
        any(current_state.get(name, False) for name in all_pump_names)
        if current_state
        else None
    )

    system_segments = merge_or_segments(
        changes_by_pump,
        all_pump_names,
        range_start,
        range_end,
        idle_gap=idle_gap,
        current_at_end=system_current,
    )

    groups = group_pump_cards(pump_cards, config=config)
    devices: list[dict[str, Any]] = []

    for group in groups:
        pumps = group["pumps"]
        pump_names = [p["name"] for p in pumps]
        device_current = (
            any(current_state.get(name, False) for name in pump_names)
            if current_state
            else None
        )
        device_segments = build_or_track_segments(
            changes_by_pump,
            pump_names,
            range_start,
            range_end,
            current_at_end=device_current,
        )
        switches = [
            {
                "name": pump["name"],
                "label": _switch_label(pump),
                "switch_code": pump.get("switch_code") or "switch_1",
                "device_on": current_state.get(pump["name"]) if current_state else None,
                "segments": build_track_segments(
                    changes_by_pump.get(pump["name"], []),
                    range_start,
                    range_end,
                    current_at_end=(
                        current_state.get(pump["name"]) if current_state else None
                    ),
                ),
            }
            for pump in pumps
        ]
        if group["kind"] == "group":
            device_key = f"{group.get('device_backend', 'device')}:{group.get('device_id', '')}"
            label = group.get("label") or device_key
            expandable = len(pumps) > 1
        else:
            pump = pumps[0]
            device_key = pump["name"]
            label = _display_pump_label(pump["name"])
            expandable = False

        devices.append(
            {
                "key": device_key,
                "label": label,
                "expandable": expandable,
                "switch_count": len(pumps),
                "segments": device_segments,
                "switches": switches,
            }
        )

    markers: list[dict[str, Any]] = []
    range_start_utc = _as_utc(range_start)
    range_end_utc = _as_utc(range_end)
    for row in events:
        marker = format_timeline_marker(row)
        if marker is None:
            continue
        ts = _as_utc(row.ts)
        if ts < range_start_utc or ts > range_end_utc:
            continue
        markers.append(marker)
    markers.sort(key=lambda item: item["ts"])

    return {
        "range_start": range_start.isoformat(),
        "range_end": range_end.isoformat(),
        "idle_gap_minutes": idle_gap_minutes,
        "system": {"label": "System", "segments": system_segments},
        "devices": devices,
        "markers": markers,
    }
