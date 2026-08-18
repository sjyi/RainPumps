"""Max runtime resolution hierarchy."""

from __future__ import annotations

from app.config import AppConfig, DeviceRuntimeOverride, PumpConfig, SafetyConfig, TuyaConfig
from app.pump_card_groups import group_pump_cards
from app.runtime_config import (
    max_runtime_by_pump,
    pump_cards_from_config,
    resolve_max_runtime_minutes,
    resolve_max_runtime_source,
    runtime_settings_view,
)


def test_system_default_is_three_hours() -> None:
    config = AppConfig()
    assert config.safety.max_continuous_runtime_minutes == 180


def test_device_override() -> None:
    config = AppConfig(
        pumps=[PumpConfig(name="switch_a", tuya=TuyaConfig(device_id="dev1", switch_code="switch_1"))],
        device_runtime=[
            DeviceRuntimeOverride(
                device_backend="tuya",
                device_id="dev1",
                max_runtime_minutes=120,
            )
        ],
    )
    assert resolve_max_runtime_minutes(config.pumps[0], config) == 120
    assert resolve_max_runtime_source(config.pumps[0], config) == "device"


def test_switch_override_beats_device_and_system() -> None:
    config = AppConfig(
        safety=SafetyConfig(max_continuous_runtime_minutes=180),
        device_runtime=[
            DeviceRuntimeOverride(
                device_backend="tuya",
                device_id="dev1",
                max_runtime_minutes=120,
            )
        ],
        pumps=[
            PumpConfig(
                name="switch_a",
                max_runtime_minutes=90,
                tuya=TuyaConfig(device_id="dev1", switch_code="switch_1"),
            )
        ],
    )
    assert resolve_max_runtime_minutes(config.pumps[0], config) == 90
    assert resolve_max_runtime_source(config.pumps[0], config) == "switch"


def test_max_runtime_by_pump_map() -> None:
    config = AppConfig(
        pumps=[
            PumpConfig(name="a", max_runtime_minutes=45, tuya=TuyaConfig(device_id="d1")),
            PumpConfig(name="b", tuya=TuyaConfig(device_id="d2")),
        ]
    )
    assert max_runtime_by_pump(config) == {"a": 45, "b": 180}


def test_runtime_settings_view_groups_device_and_switch() -> None:
    config = AppConfig(
        pumps=[
            PumpConfig(name="plug_sw1", tuya=TuyaConfig(device_id="dev1", switch_code="switch_1")),
            PumpConfig(name="plug_sw2", tuya=TuyaConfig(device_id="dev1", switch_code="switch_2")),
        ]
    )
    groups = group_pump_cards(pump_cards_from_config(config))
    view = runtime_settings_view(config, groups)
    assert view["system_max_runtime_minutes"] == 180
    assert len(view["devices"]) == 1
    assert view["devices"][0]["key"] == "tuya:dev1"
    assert len(view["switches"]) == 2
