"""Group pump dashboard cards by shared physical device (multi-outlet plugs)."""

from __future__ import annotations

import re
from typing import Any

from app.config import AppConfig
from app.device_keys import device_label_key

_ONLINE_RANK = {
    "offline": 0,
    "cloud_error": 0,
    "unknown": 1,
    "degraded": 2,
    "unconfigured": 3,
    "online": 4,
}


def _switch_sort_key(card: dict[str, Any]) -> tuple[int, str]:
    code = card.get("switch_code") or "switch_1"
    match = re.match(r"switch_(\d+)$", code)
    if match:
        return (int(match.group(1)), card.get("name", ""))
    return (999, card.get("name", ""))


def _display_name(name: str) -> str:
    return name.replace("_", " ").title()


def parse_probe_switch_state(detail: str) -> bool | None:
    """Return live switch ON/OFF parsed from probe detail (e.g. meross_cloud:on)."""
    if not detail:
        return None
    suffix = detail.rsplit(":", 1)[-1].strip().lower()
    if suffix == "on":
        return True
    if suffix == "off":
        return False
    return None


def device_group_label(pumps: list[dict[str, Any]]) -> str:
    labels = [
        (p.get("display_label") or _display_name(p["name"])) for p in pumps
    ]
    if len(labels) == 1:
        return labels[0]
    first = labels[0]
    stripped = re.sub(r"\s*\(Switch_\d+\)$", "", first, flags=re.I)
    stripped = re.sub(r"\s*\(Switch \d+\)$", "", stripped, flags=re.I)
    stripped = re.sub(r"\s+Switch\s+\d+$", "", stripped, flags=re.I)
    if stripped and stripped != first:
        return stripped
    parts = [label.split() for label in labels]
    common: list[str] = []
    min_len = min(len(part) for part in parts)
    for i in range(min_len):
        if len({part[i] for part in parts}) == 1:
            common.append(parts[0][i])
        else:
            break
    if len(common) >= 2:
        return " ".join(common)
    device_id = pumps[0].get("device_id") or ""
    backend = pumps[0].get("device_backend") or "device"
    short = f"{device_id[:10]}…" if len(device_id) > 10 else device_id
    return f"{backend.title()} {short}".strip()


def aggregate_online_status(pumps: list[dict[str, Any]]) -> str:
    statuses = [p.get("online_status") or "unknown" for p in pumps]
    return min(statuses, key=lambda s: _ONLINE_RANK.get(s, 1))


def device_display_order_map(config: AppConfig) -> dict[str, int]:
    out: dict[str, int] = {}
    for index, row in enumerate(config.device_display_order):
        key = device_label_key(row.device_backend, row.device_id)
        if key not in out:
            out[key] = index
    return out


def _group_device_key(group: dict[str, Any]) -> str:
    key = (group.get("device_key") or "").strip()
    if key:
        return key
    pumps = group.get("pumps") or []
    if not pumps:
        return ""
    return device_label_key(
        pumps[0].get("device_backend") or "",
        pumps[0].get("device_id") or "",
    )


def sort_device_groups(
    groups: list[dict[str, Any]],
    config: AppConfig | None,
) -> list[dict[str, Any]]:
    """Apply configured device display order; unknown devices follow by label."""
    if not config or not groups:
        return groups
    order_map = device_display_order_map(config)
    if not order_map:
        return groups

    def sort_key(group: dict[str, Any]) -> tuple[int, str, str]:
        key = _group_device_key(group)
        if key in order_map:
            return (0, f"{order_map[key]:06d}", key)
        label = (group.get("label") or key or "").lower()
        return (1, label, key)

    return sorted(groups, key=sort_key)


def group_pump_cards(
    cards: list[dict[str, Any]],
    *,
    config: AppConfig | None = None,
) -> list[dict[str, Any]]:
    """Return ordered groups: single pumps or multi-switch device groups."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        device_id = (card.get("device_id") or "").strip()
        if not device_id:
            continue
        key = f"{card.get('device_backend') or 'unknown'}:{device_id}"
        by_key.setdefault(key, []).append(card)

    emitted: set[str] = set()
    groups: list[dict[str, Any]] = []
    for card in cards:
        device_id = (card.get("device_id") or "").strip()
        if not device_id:
            groups.append({"kind": "single", "pumps": [card]})
            continue
        key = f"{card.get('device_backend') or 'unknown'}:{device_id}"
        if key in emitted:
            continue
        emitted.add(key)
        pumps = sorted(by_key[key], key=_switch_sort_key)
        if len(pumps) == 1:
            device_key = ""
            if device_id:
                device_key = f"{pumps[0].get('device_backend') or 'unknown'}:{device_id}"
            groups.append(
                {
                    "kind": "single",
                    "device_id": device_id,
                    "device_backend": pumps[0].get("device_backend", ""),
                    "device_key": device_key,
                    "label": pumps[0].get("device_label") or pumps[0].get("display_label") or "",
                    "pumps": pumps,
                }
            )
        else:
            device_label = (pumps[0].get("device_label") or "").strip()
            device_key = f"{pumps[0].get('device_backend') or 'unknown'}:{device_id}"
            groups.append(
                {
                    "kind": "group",
                    "device_id": device_id,
                    "device_backend": pumps[0].get("device_backend", ""),
                    "device_key": device_key,
                    "label": device_label or device_group_label(pumps),
                    "online_status": aggregate_online_status(pumps),
                    "switch_count": len(pumps),
                    "pumps": pumps,
                }
            )
    return sort_device_groups(groups, config)
