"""Stagger turn-on across switches on the same physical plug."""

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


def pump_switch_code(pump: PumpConfig | None) -> str:
    if pump is None:
        return "switch_1"
    if pump.meross.device_uuid:
        return pump.meross.switch_code or f"switch_{pump.meross.channel + 1}"
    return pump.tuya.switch_code or "switch_1"


def pump_physical_device_id(pump: PumpConfig | None) -> str:
    """Shared device id for stagger grouping (Tuya device_id or Meross uuid)."""
    if pump is None:
        return ""
    if pump.tuya.device_id.strip():
        return pump.tuya.device_id.strip()
    return pump.meross.device_uuid.strip()


def pump_tuya_device_id(pump: PumpConfig | None) -> str:
    """Backward-compatible alias."""
    return pump_physical_device_id(pump)


def group_turn_on_commands(
    commands: list[PumpCommand],
    pumps_by_name: dict[str, PumpConfig],
) -> tuple[list[PumpCommand], dict[str, list[PumpCommand]]]:
    """Split turn_on commands into singles vs groups sharing one physical plug."""
    singles: list[PumpCommand] = []
    by_device: dict[str, list[PumpCommand]] = defaultdict(list)
    for cmd in commands:
        if cmd.action != "turn_on":
            continue
        pump = pumps_by_name.get(cmd.pump_name)
        device_id = pump_physical_device_id(pump)
        if device_id:
            by_device[device_id].append(cmd)
        else:
            singles.append(cmd)
    return singles, dict(by_device)


def sort_pumps_by_switch(pumps: list[PumpConfig]) -> list[PumpConfig]:
    """Order pump configs by outlet (switch_1 before switch_2, …)."""
    return sorted(
        pumps,
        key=lambda p: (switch_index(pump_switch_code(p)), p.name),
    )


def sort_commands_by_switch(
    commands: list[PumpCommand],
    pumps_by_name: dict[str, PumpConfig],
) -> list[PumpCommand]:
    def sort_key(cmd: PumpCommand) -> int:
        pump = pumps_by_name.get(cmd.pump_name)
        if pump and pump.meross.device_uuid:
            if pump.meross.switch_code:
                return switch_index(pump.meross.switch_code)
            return pump.meross.channel
        return switch_index(pump_switch_code(pump))

    return sorted(commands, key=sort_key)
