"""Switch stagger helper tests."""

from __future__ import annotations

from app.config import PumpConfig, TuyaConfig
from app.engine import PumpCommand
from app.switch_stagger import group_turn_on_commands, sort_commands_by_switch, switch_index


def test_switch_index_order() -> None:
    assert switch_index("switch_1") == 0
    assert switch_index("switch_2") == 1
    assert switch_index("switch_3") == 2


def test_group_turn_on_commands_by_device() -> None:
    pumps = {
        "a1": PumpConfig(name="a1", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_1")),
        "a2": PumpConfig(name="a2", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_2")),
        "b1": PumpConfig(name="b1", tuya=TuyaConfig(device_id="dev-b", switch_code="switch_1")),
    }
    commands = [
        PumpCommand("a2", "turn_on", "test"),
        PumpCommand("a1", "turn_on", "test"),
        PumpCommand("b1", "turn_on", "test"),
        PumpCommand("a1", "turn_off", "test"),
    ]
    singles, by_device = group_turn_on_commands(commands, pumps)
    assert singles == []
    assert len(by_device["dev-a"]) == 2
    assert len(by_device["dev-b"]) == 1
    ordered = sort_commands_by_switch(by_device["dev-a"], pumps)
    assert [c.pump_name for c in ordered] == ["a1", "a2"]
