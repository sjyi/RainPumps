"""Stagger turn-on across switches on the same Tuya device."""

from __future__ import annotations

import re
from collections import defaultdict

from app.config import PumpConfig
from app.engine import PumpCommand


def switch_index(switch_code: str) -> int:
    """Zero-based outlet order: switch_1 → 0, switch_2 → 1, …"""
    match = re.match(r"switch_(\d+)$", (switch_code or "switch_1").strip())
    if match:
        return int(match.group(1)) - 1
    return 0


def pump_tuya_device_id(pump: PumpConfig | None) -> str:
    if pump is None:
        return ""
    return pump.tuya.device_id.strip()


def group_turn_on_commands(
    commands: list[PumpCommand],
    pumps_by_name: dict[str, PumpConfig],
) -> tuple[list[PumpCommand], dict[str, list[PumpCommand]]]:
    """Split turn_on commands into singles vs groups sharing a Tuya device_id."""
    singles: list[PumpCommand] = []
    by_device: dict[str, list[PumpCommand]] = defaultdict(list)
    for cmd in commands:
        if cmd.action != "turn_on":
            continue
        pump = pumps_by_name.get(cmd.pump_name)
        device_id = pump_tuya_device_id(pump)
        if device_id:
            by_device[device_id].append(cmd)
        else:
            singles.append(cmd)
    return singles, dict(by_device)


def sort_commands_by_switch(
    commands: list[PumpCommand],
    pumps_by_name: dict[str, PumpConfig],
) -> list[PumpCommand]:
    return sorted(
        commands,
        key=lambda cmd: switch_index(
            pumps_by_name[cmd.pump_name].tuya.switch_code
            if cmd.pump_name in pumps_by_name
            else "switch_1"
        ),
    )
