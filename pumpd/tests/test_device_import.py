"""Tests for device discovery and import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.config import PumpConfig, save_pumps
from app.device_import import (
    DiscoveredDevice,
    discover_all,
    expand_tuya_switch_devices,
    load_tuya_cloud_credentials,
    load_tuya_devices_json,
    merge_discovered,
    slugify_pump_name,
    tuya_switch_codes_from_mapping,
)


def test_slugify_pump_name() -> None:
    assert slugify_pump_name("North Roof Pump") == "north_roof_pump"
    assert slugify_pump_name("Pump #1!!!") == "pump_1"


def test_merge_pairs_by_match_key() -> None:
    st = [
        DiscoveredDevice(
            source="smartthings",
            label="North Pump",
            smartthings_device_id="st-abc",
            match_key="northpump",
        )
    ]
    tuya = [
        DiscoveredDevice(
            source="tuya_cloud",
            label="North Pump Switch",
            tuya_device_id="tuya-123",
            tuya_ip="192.168.1.50",
            tuya_local_key="localkey123456",
            match_key="northpumpswitch",
        )
    ]
    merged = merge_discovered(st, tuya)
    assert len(merged) == 2  # keys differ — no exact match
    by_st = [m for m in merged if m.smartthings_device_id]
    by_tuya = [m for m in merged if m.tuya_device_id]
    assert len(by_st) == 1
    assert len(by_tuya) == 1


def test_merge_exact_match_key() -> None:
    st = [
        DiscoveredDevice(
            source="smartthings",
            label="East Pump",
            smartthings_device_id="st-east",
            match_key="eastpump",
        )
    ]
    tuya = [
        DiscoveredDevice(
            source="tuya_json",
            label="East Pump",
            tuya_device_id="tuya-east",
            tuya_ip="10.0.0.5",
            tuya_local_key="abcdefghijklmnop",
            match_key="eastpump",
        )
    ]
    merged = merge_discovered(st, tuya)
    assert len(merged) == 1
    assert merged[0].smartthings_device_id == "st-east"
    assert merged[0].tuya_device_id == "tuya-east"


def test_load_tuya_devices_json(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Roof A",
                    "id": "abc123",
                    "key": "key123456789012",
                    "ip": "192.168.1.2",
                    "version": "3.4",
                }
            ]
        ),
        encoding="utf-8",
    )
    devices = load_tuya_devices_json(path)
    assert len(devices) == 1
    assert devices[0].tuya_device_id == "abc123"
    assert devices[0].tuya_local_key == "key123456789012"


def test_save_pumps_merge(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.dump({"pumps": [{"name": "existing", "enabled": True}]}),
        encoding="utf-8",
    )
    new_pump = PumpConfig(name="new_pump")
    result = save_pumps(cfg, [new_pump], mode="merge")
    assert {p.name for p in result} == {"existing", "new_pump"}


@pytest.mark.asyncio
async def test_discover_all_without_credentials() -> None:
    result = await discover_all(smartthings_pat="", lan_scan=False)
    assert "devices" in result
    assert result["sources"]["smartthings"]["status"] == "skipped"
    assert result["sources"]["tuya_cloud"]["status"] == "skipped"
    assert result["errors"] == {}


def test_load_tuya_credentials_from_file(tmp_path: Path) -> None:
    cfg = tmp_path / "tinytuya.json"
    cfg.write_text(
        json.dumps(
            {
                "apiKey": "key123",
                "apiSecret": "secret456",
                "apiRegion": "us",
                "apiDeviceID": "dev789",
            }
        ),
        encoding="utf-8",
    )
    key, secret, region, device_id, _ = load_tuya_cloud_credentials(config_file=cfg)
    assert key == "key123"
    assert secret == "secret456"
    assert region == "us"
    assert device_id == "dev789"


def test_tuya_switch_codes_from_mapping() -> None:
    mapping = {
        "1": {"code": "switch_1", "type": "Boolean", "values": {}},
        "2": {"code": "switch_2", "type": "Boolean", "values": {}},
        "3": {"code": "switch_3", "type": "Boolean", "values": {}},
        "38": {"code": "relay_status", "type": "Enum", "values": {}},
    }
    switches = tuya_switch_codes_from_mapping(mapping)
    assert switches == [("1", "switch_1"), ("2", "switch_2"), ("3", "switch_3")]


def test_expand_tuya_switch_devices_multi_outlet() -> None:
    dev = DiscoveredDevice(
        source="tuya_json",
        label="1st Fl Roof 302",
        tuya_device_id="eb228bcc151ba6b1377wbr",
        tuya_local_key="secret",
        match_key="1stflroof302",
        raw={
            "mapping": {
                "1": {"code": "switch_1", "type": "Boolean", "values": {}},
                "2": {"code": "switch_2", "type": "Boolean", "values": {}},
                "3": {"code": "switch_3", "type": "Boolean", "values": {}},
            }
        },
    )
    expanded = expand_tuya_switch_devices([dev])
    assert len(expanded) == 3
    assert expanded[0].tuya_switch_code == "switch_1"
    assert expanded[1].tuya_switch_code == "switch_2"
    assert expanded[2].tuya_switch_code == "switch_3"
    assert expanded[1].tuya_device_id == dev.tuya_device_id
    assert expanded[1].label.endswith("(switch_2)")


def test_load_tuya_devices_json_expands_via_discover_all(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "Triple Plug",
                    "id": "dev-multi",
                    "key": "key123456789012",
                    "mapping": {
                        "1": {"code": "switch_1", "type": "Boolean", "values": {}},
                        "2": {"code": "switch_2", "type": "Boolean", "values": {}},
                        "3": {"code": "switch_3", "type": "Boolean", "values": {}},
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    async def run() -> dict:
        return await discover_all(
            smartthings_pat="",
            lan_scan=False,
            tuya_devices_file=path,
        )

    import asyncio

    result = asyncio.run(run())
    assert len(result["devices"]) == 3
    codes = {d["tuya_switch_code"] for d in result["devices"]}
    assert codes == {"switch_1", "switch_2", "switch_3"}
