"""Tests for configurable device group display order."""

from __future__ import annotations

from pathlib import Path

from app.config import (
    AppConfig,
    DeviceDisplayOrderEntry,
    MerossConfig,
    PumpConfig,
    load_config,
    save_device_display_order,
)
from app.display_names import device_order_settings_view
from app.pump_card_groups import group_pump_cards, sort_device_groups


def _cards() -> list[dict]:
    return [
        {
            "name": "second_device_sw1",
            "display_label": "Second Sw1",
            "device_label": "2nd fl rm 206",
            "device_id": "uuid-second",
            "device_backend": "meross",
            "switch_code": "switch_1",
            "online_status": "online",
        },
        {
            "name": "second_device_sw2",
            "display_label": "Second Sw2",
            "device_label": "2nd fl rm 206",
            "device_id": "uuid-second",
            "device_backend": "meross",
            "switch_code": "switch_2",
            "online_status": "online",
        },
        {
            "name": "first_device_sw1",
            "display_label": "First Sw1",
            "device_label": "1st fl office",
            "device_id": "uuid-first",
            "device_backend": "meross",
            "switch_code": "switch_1",
            "online_status": "online",
        },
        {
            "name": "first_device_sw2",
            "display_label": "First Sw2",
            "device_label": "1st fl office",
            "device_id": "uuid-first",
            "device_backend": "meross",
            "switch_code": "switch_2",
            "online_status": "online",
        },
    ]


def test_sort_device_groups_uses_configured_order() -> None:
    config = AppConfig(
        device_display_order=[
            DeviceDisplayOrderEntry(device_backend="meross", device_id="uuid-first"),
            DeviceDisplayOrderEntry(device_backend="meross", device_id="uuid-second"),
        ],
        pumps=[
            PumpConfig(name="second_device_sw1", meross=MerossConfig(device_uuid="uuid-second", channel=1)),
            PumpConfig(name="first_device_sw1", meross=MerossConfig(device_uuid="uuid-first", channel=1)),
        ],
    )
    groups = group_pump_cards(_cards(), config=config)
    labels = [g["label"] for g in groups]
    assert labels == ["1st fl office", "2nd fl rm 206"]


def test_device_order_settings_view_lists_configured_first() -> None:
    config = AppConfig(
        device_display_order=[
            DeviceDisplayOrderEntry(device_backend="meross", device_id="uuid-first"),
        ],
    )
    groups = group_pump_cards(_cards())
    items = device_order_settings_view(config, groups)
    assert items[0]["device_key"] == "meross:uuid-first"
    assert items[0]["label"] == "1st fl office"


def test_save_device_display_order_persists(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("pumps: []\n", encoding="utf-8")
    save_device_display_order(
        cfg_path,
        [
            DeviceDisplayOrderEntry(device_backend="meross", device_id="uuid-a"),
            DeviceDisplayOrderEntry(device_backend="meross", device_id="uuid-b"),
        ],
    )
    loaded = load_config(cfg_path)
    assert len(loaded.device_display_order) == 2
    assert loaded.device_display_order[0].device_id == "uuid-a"


def test_sort_device_groups_without_config_keeps_discovery_order() -> None:
    groups = group_pump_cards(_cards())
    assert [g["label"] for g in groups] == ["2nd fl rm 206", "1st fl office"]


def test_sort_device_groups_helper() -> None:
    groups = [
        {"device_key": "meross:b", "label": "B"},
        {"device_key": "meross:a", "label": "A"},
    ]
    config = AppConfig(
        device_display_order=[
            DeviceDisplayOrderEntry(device_backend="meross", device_id="a"),
        ]
    )
    sorted_groups = sort_device_groups(groups, config)
    assert [g["device_key"] for g in sorted_groups] == ["meross:a", "meross:b"]
