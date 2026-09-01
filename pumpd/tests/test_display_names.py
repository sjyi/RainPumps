"""Tests for display name configuration and cloud rename helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import (
    AppConfig,
    DeviceLabelOverride,
    PumpConfig,
    TuyaConfig,
    load_config,
    save_display_names,
)
from app.devices.cloud_rename import (
    rename_smartthings_device,
    rename_tuya_device,
    rename_tuya_switch,
    tuya_switch_index,
)
from app.display_names import (
    device_labels_map,
    display_name_settings_view,
    pump_display_label,
)
from app.db import init_db
from app.pump_card_groups import group_pump_cards
from app.runtime_config import pump_cards_from_config
from app.service import PumpService


def test_pump_display_label_uses_config_label() -> None:
    cfg = PumpConfig(name="north_pump", label="Roof North")
    assert pump_display_label(cfg) == "Roof North"


def test_save_display_names_persists_labels(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "pumps:\n  - name: north_pump\n    tuya:\n      device_id: abc\n",
        encoding="utf-8",
    )
    save_display_names(
        cfg_path,
        pump_labels={"north_pump": "North Outlet"},
        device_labels=[
            DeviceLabelOverride(device_backend="tuya", device_id="abc", label="Roof Plug")
        ],
    )
    loaded = load_config(cfg_path)
    assert loaded.pumps[0].label == "North Outlet"
    assert device_labels_map(loaded)["tuya:abc"] == "Roof Plug"


def test_save_display_names_merges_device_labels(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "pumps:",
                "  - name: p1",
                "  - name: p2",
                "device_labels:",
                "  - device_backend: meross",
                "    device_id: dev-a",
                "    label: Device A",
                "  - device_backend: meross",
                "    device_id: dev-b",
                "    label: Device B",
            ]
        ),
        encoding="utf-8",
    )
    save_display_names(
        cfg_path,
        pump_labels={"p1": "Outlet 1"},
        device_labels=[
            DeviceLabelOverride(device_backend="meross", device_id="dev-a", label="Device A Renamed")
        ],
    )
    loaded = load_config(cfg_path)
    labels = device_labels_map(loaded)
    assert labels["meross:dev-a"] == "Device A Renamed"
    assert labels["meross:dev-b"] == "Device B"
    assert loaded.pumps[0].label == "Outlet 1"


def test_display_name_settings_view_groups_devices() -> None:
    config = AppConfig(
        pumps=[
            PumpConfig(
                name="p1",
                label="Outlet 1",
                tuya=TuyaConfig(device_id="dev1", switch_code="switch_1"),
            ),
            PumpConfig(
                name="p2",
                label="Outlet 2",
                tuya=TuyaConfig(device_id="dev1", switch_code="switch_2"),
            ),
        ],
        device_labels=[
            DeviceLabelOverride(device_backend="tuya", device_id="dev1", label="Roof East")
        ],
    )
    cards = pump_cards_from_config(config)
    groups = group_pump_cards(cards)
    view = display_name_settings_view(config, groups)
    assert len(view["groups"]) == 1
    assert view["groups"][0]["device_label"] == "Roof East"
    assert view["groups"][0]["pumps"][0]["display_label"] == "Outlet 1"


@pytest.mark.asyncio
async def test_update_display_names_propagates_meross_switch(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "meross:",
                "  email: user@example.com",
                "  password: secret",
                "pumps:",
                "  - name: fl202_sw1",
                "    label: Switch 1",
                "    meross:",
                "      device_uuid: uuid-202",
                "      channel: 1",
                "  - name: fl202_sw2",
                "    label: dead",
                "    meross:",
                "      device_uuid: uuid-202",
                "      channel: 2",
                "device_labels:",
                "  - device_backend: meross",
                "    device_id: uuid-202",
                "    label: 2nd fl 202",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    session_factory = init_db(f"sqlite:///{tmp_path / 'pumpd.db'}")
    service = PumpService(cfg, session_factory, config_path=str(cfg_path))
    service.meross_session = MagicMock()
    service.meross_session.configured = True
    service.meross_session.started = True
    service.meross_session.startup = AsyncMock()
    service.meross_session.list_cloud_devices = AsyncMock(
        return_value=[
            MagicMock(
                uuid="uuid-202",
                device_type="mss620",
                channels=[
                    {},
                    {"type": "Switch", "devName": "Switch 1"},
                    {"type": "Switch", "devName": "dead"},
                ],
            )
        ]
    )
    service.meross_session.update_cloud_device_name = AsyncMock(
        return_value={
            "success": False,
            "unsupported": True,
            "message": "Meross cloud API does not persist name changes",
        }
    )
    service.meross_session.update_cloud_switch_name = AsyncMock(
        return_value={
            "success": True,
            "message": "updated outlet name in Meross cloud",
            "api_path": "/v1/Device/devInfo",
        }
    )

    result = await service.update_display_names(
        device_labels={"meross:uuid-202": "2nd fl 202"},
        switch_labels={"fl202_sw1": "Switch 1", "fl202_sw2": "live"},
        propagate_cloud=True,
    )

    assert result["saved"] is True
    switch_calls = service.meross_session.update_cloud_switch_name.await_args_list
    assert len(switch_calls) == 2
    assert switch_calls[1].args[1:4] == (2, "live")
    cloud_switch_results = [item for item in result["cloud"] if item.get("kind") == "switch"]
    assert len(cloud_switch_results) == 2
    assert all(item["success"] for item in cloud_switch_results)
    assert "trace" in result
    switch_trace = [t for t in result["trace"] if t.get("step") == "switch_rename"]
    assert len(switch_trace) == 2
    assert switch_trace[1]["channel"] == 2
    assert switch_trace[1]["label"] == "live"


@pytest.mark.asyncio
async def test_update_display_names_preserves_other_device_labels(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "pumps:",
                "  - name: p1",
                "    label: Outlet 1",
                "    meross:",
                "      device_uuid: dev-a",
                "      channel: 0",
                "  - name: p2",
                "    label: Outlet 2",
                "    meross:",
                "      device_uuid: dev-a",
                "      channel: 1",
                "  - name: p3",
                "    label: Other",
                "    meross:",
                "      device_uuid: dev-b",
                "      channel: 0",
                "device_labels:",
                "  - device_backend: meross",
                "    device_id: dev-a",
                "    label: Device A",
                "  - device_backend: meross",
                "    device_id: dev-b",
                "    label: Device B",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    session_factory = init_db(f"sqlite:///{tmp_path / 'pumpd.db'}")
    service = PumpService(cfg, session_factory, config_path=str(cfg_path))
    service.meross_session = MagicMock()
    service.meross_session.configured = False

    await service.update_display_names(
        device_labels={"meross:dev-a": "2nd fl 202"},
        switch_labels={"p1": "Switch 1", "p2": "live"},
        propagate_cloud=False,
    )

    loaded = load_config(cfg_path)
    labels = device_labels_map(loaded)
    assert labels["meross:dev-a"] == "2nd fl 202"
    assert labels["meross:dev-b"] == "Device B"
    by_name = {p.name: p for p in loaded.pumps}
    assert by_name["p2"].label == "live"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("switch_1", 1),
        ("switch_2", 2),
        ("switch_led", None),
    ],
)
def test_tuya_switch_index(code: str, expected: int | None) -> None:
    assert tuya_switch_index(code) == expected


@pytest.mark.asyncio
async def test_rename_tuya_device_success() -> None:
    cloud = MagicMock()
    cloud.cloudrequest.return_value = {"success": True}
    result = await rename_tuya_device(cloud, "dev123", "New Name")
    assert result["success"] is True
    cloud.cloudrequest.assert_called_once()


@pytest.mark.asyncio
async def test_rename_tuya_switch_falls_back_to_device_rename() -> None:
    cloud = MagicMock()
    cloud.cloudrequest.side_effect = [
        {"success": False, "msg": "not supported"},
        {"success": True},
    ]
    result = await rename_tuya_switch(cloud, "dev123", "switch_1", "Outlet A")
    assert result["success"] is True
    assert cloud.cloudrequest.call_count == 2


@pytest.mark.asyncio
async def test_rename_smartthings_device(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def put(self, url: str, headers: dict[str, str], json: dict[str, str]) -> FakeResp:
            assert "devices/st-1" in url
            assert json["label"] == "Garage Pump"
            return FakeResp()

    monkeypatch.setattr("app.devices.cloud_rename.httpx.AsyncClient", lambda **_: FakeClient())
    result = await rename_smartthings_device("pat", "st-1", "Garage Pump")
    assert result["success"] is True
