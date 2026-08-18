"""Display name helpers for pumps and physical devices."""

from __future__ import annotations

from typing import Any

from app.config import AppConfig, DeviceLabelOverride, PumpConfig
from app.device_keys import device_label_key
from app.pump_card_groups import device_group_label


def pump_display_label(cfg: PumpConfig) -> str:
    label = (cfg.label or "").strip()
    if label:
        return label
    return cfg.name.replace("_", " ").title()


def device_labels_map(config: AppConfig) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in config.device_labels:
        label = (row.label or "").strip()
        if label:
            out[device_label_key(row.device_backend, row.device_id)] = label
    return out


def resolve_device_label(
    config: AppConfig,
    device_backend: str,
    device_id: str,
    pumps: list[dict[str, Any]],
) -> str:
    key = device_label_key(device_backend, device_id)
    stored = device_labels_map(config).get(key)
    if stored:
        return stored
    if pumps:
        return device_group_label(pumps)
    return ""


def display_name_settings_view(
    config: AppConfig,
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """Admin UI payload for device and switch display names."""
    labels = device_labels_map(config)
    by_name = {p.name: p for p in config.pumps}
    result_groups: list[dict[str, Any]] = []

    for group in groups:
        pumps = group.get("pumps") or []
        if not pumps:
            continue
        backend = pumps[0].get("device_backend") or ""
        device_id = pumps[0].get("device_id") or ""
        key = device_label_key(backend, device_id) if device_id else ""
        cfg_rows = [by_name[p["name"]] for p in pumps if p["name"] in by_name]
        result_groups.append(
            {
                "kind": group.get("kind", "single"),
                "device_backend": backend,
                "device_id": device_id,
                "device_key": key,
                "device_label": labels.get(key, resolve_device_label(config, backend, device_id, pumps)),
                "switch_count": len(pumps),
                "pumps": [
                    {
                        "name": card["name"],
                        "switch_code": card.get("switch_code") or "switch_1",
                        "display_label": (
                            pump_display_label(by_name[card["name"]])
                            if card["name"] in by_name
                            else card["name"].replace("_", " ").title()
                        ),
                        "config_name": card["name"],
                    }
                    for card in pumps
                ],
            }
        )

    return {"groups": result_groups}


def card_display_label(card: dict[str, Any]) -> str:
    label = (card.get("display_label") or "").strip()
    if label:
        return label
    return card.get("name", "").replace("_", " ").title()


def device_order_settings_view(
    config: AppConfig,
    groups: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Ordered device rows for the admin display-order editor."""
    order_map = {
        device_label_key(row.device_backend, row.device_id): index
        for index, row in enumerate(config.device_display_order)
    }
    by_key: dict[str, dict[str, str]] = {}
    for group in groups:
        key = (group.get("device_key") or "").strip()
        if not key:
            pumps = group.get("pumps") or []
            if pumps:
                key = device_label_key(
                    pumps[0].get("device_backend") or "",
                    pumps[0].get("device_id") or "",
                )
        if not key or key in by_key:
            continue
        by_key[key] = {
            "device_key": key,
            "label": (group.get("label") or key).strip(),
        }

    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in config.device_display_order:
        key = device_label_key(row.device_backend, row.device_id)
        if key in by_key and key not in seen:
            ordered.append(by_key[key])
            seen.add(key)

    remaining = sorted(
        (item for key, item in by_key.items() if key not in seen),
        key=lambda item: item["label"].lower(),
    )
    ordered.extend(remaining)
    return ordered
