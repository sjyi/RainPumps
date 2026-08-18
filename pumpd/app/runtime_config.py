"""Resolve max continuous runtime: switch → device → system."""

from __future__ import annotations

from typing import Any, Literal

from app.config import AppConfig, DeviceRuntimeOverride, PumpConfig
from app.display_names import pump_display_label

RuntimeSource = Literal["switch", "device", "system"]


def pump_device_key(cfg: PumpConfig) -> str | None:
    if cfg.meross.device_uuid:
        return f"meross:{cfg.meross.device_uuid}"
    if cfg.tuya.device_id:
        return f"tuya:{cfg.tuya.device_id}"
    if cfg.smartthings.device_id:
        return f"smartthings:{cfg.smartthings.device_id}"
    return None


def device_runtime_map(config: AppConfig) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in config.device_runtime:
        if row.max_runtime_minutes is not None:
            out[f"{row.device_backend}:{row.device_id}"] = row.max_runtime_minutes
    return out


def resolve_max_runtime_minutes(pump: PumpConfig, config: AppConfig) -> int:
    if pump.max_runtime_minutes is not None:
        return pump.max_runtime_minutes
    key = pump_device_key(pump)
    if key:
        device_limit = device_runtime_map(config).get(key)
        if device_limit is not None:
            return device_limit
    return config.safety.max_continuous_runtime_minutes


def resolve_max_runtime_source(pump: PumpConfig, config: AppConfig) -> RuntimeSource:
    if pump.max_runtime_minutes is not None:
        return "switch"
    key = pump_device_key(pump)
    if key and key in device_runtime_map(config):
        return "device"
    return "system"


def max_runtime_by_pump(config: AppConfig) -> dict[str, int]:
    return {p.name: resolve_max_runtime_minutes(p, config) for p in config.pumps}


def runtime_settings_view(config: AppConfig, groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Admin UI payload for max-runtime configuration."""
    device_rows: list[dict[str, Any]] = []
    switch_rows: list[dict[str, Any]] = []
    dev_map = device_runtime_map(config)
    system_default = config.safety.max_continuous_runtime_minutes

    for group in groups:
        pumps = group.get("pumps") or []
        if not pumps:
            continue
        first_cfg = next((p for p in config.pumps if p.name == pumps[0]["name"]), None)
        device_key = pump_device_key(first_cfg) if first_cfg else None
        device_override = dev_map.get(device_key) if device_key else None
        if len(pumps) > 1 and device_key:
            effective = device_override if device_override is not None else system_default
            device_rows.append(
                {
                    "key": device_key,
                    "label": group.get("label") or device_key,
                    "max_runtime_minutes": device_override,
                    "effective_minutes": effective,
                    "source": "device" if device_override is not None else "system",
                }
            )
        for card in pumps:
            cfg = next((p for p in config.pumps if p.name == card["name"]), None)
            if cfg is None:
                continue
            effective = resolve_max_runtime_minutes(cfg, config)
            switch_rows.append(
                {
                    "name": cfg.name,
                    "label": pump_display_label(cfg),
                    "device_key": pump_device_key(cfg),
                    "max_runtime_minutes": cfg.max_runtime_minutes,
                    "effective_minutes": effective,
                    "source": resolve_max_runtime_source(cfg, config),
                }
            )

    return {
        "system_max_runtime_minutes": system_default,
        "devices": device_rows,
        "switches": switch_rows,
        "device_by_key": {row["key"]: row for row in device_rows},
        "switch_by_name": {row["name"]: row for row in switch_rows},
    }


def pump_cards_from_config(config: AppConfig) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for cfg in config.pumps:
        switch_code = (
            cfg.meross.switch_code or f"switch_{cfg.meross.channel + 1}"
            if cfg.meross.device_uuid
            else cfg.tuya.switch_code or "switch_1"
        )
        device_id = cfg.meross.device_uuid or cfg.tuya.device_id or cfg.smartthings.device_id or ""
        cards.append(
            {
                "name": cfg.name,
                "device_id": device_id,
                "device_backend": (
                    "meross"
                    if cfg.meross.device_uuid
                    else "tuya"
                    if cfg.tuya.device_id
                    else "smartthings"
                    if cfg.smartthings.device_id
                    else ""
                ),
                "switch_code": switch_code,
            }
        )
    return cards


def format_runtime_hours(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}h" if hours != 1 else "1h"
    return f"{minutes}m"
