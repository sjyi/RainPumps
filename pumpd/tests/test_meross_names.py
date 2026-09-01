"""Tests for Meross cloud display name sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import (
    AppConfig,
    DeviceLabelOverride,
    MerossConfig,
    PumpConfig,
    load_config,
)
from app.db import init_db
from app.devices.cloud_rename import rename_meross_device, rename_meross_switch
from app.display_names import device_labels_map, pump_display_label
from app.meross_names import (
    collect_meross_cloud_names,
    diff_meross_cloud_names,
    meross_switch_label,
    merge_device_label_rows,
)
from app.service import PumpService


@pytest.fixture
def service(tmp_path: Path) -> PumpService:
    import shutil

    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = load_config(config_path)
    cfg.database_url = f"sqlite:///{tmp_path / 'test.db'}"
    cfg.pumps = [
        PumpConfig(name="pump_a"),
        PumpConfig(name="pump_b"),
        PumpConfig(name="pump_c"),
    ]
    session_factory = init_db(cfg.database_url)
    return PumpService(cfg, session_factory, config_path=str(config_path))


def _meross_info(
    *,
    uuid: str,
    dev_name: str,
    channels: list[dict[str, object]] | None = None,
    device_type: str = "mss620",
) -> MagicMock:
    info = MagicMock()
    info.uuid = uuid
    info.dev_name = dev_name
    info.device_type = device_type
    info.channels = channels or []
    return info


def test_meross_switch_label_uses_channel_name_for_dual_outlet() -> None:
    channels = [
        {"channel": 0, "type": "Switch", "devName": ""},
        {"channel": 1, "type": "Switch", "devName": "2nd fl rm 206"},
        {"channel": 2, "type": "Switch", "devName": "Roof East"},
    ]
    label = meross_switch_label(
        "2nd Fl Roof Sump",
        1,
        channels,
        device_type="mss620",
    )
    assert label == "2nd fl rm 206"


def test_collect_meross_cloud_names_maps_device_and_switch_labels() -> None:
    uuid = "abc123"
    pumps = [
        PumpConfig(
            name="roof_sump",
            label="Old Switch Name",
            meross=MerossConfig(device_uuid=uuid, channel=1, switch_code="switch_1"),
        )
    ]
    cloud = collect_meross_cloud_names(
        [
            _meross_info(
                uuid=uuid,
                dev_name="Roof Plug",
                channels=[
                    {"channel": 1, "type": "Switch", "devName": "2nd fl rm 206"},
                    {"channel": 2, "type": "Switch", "devName": "Roof East"},
                ],
            )
        ],
        pumps,
    )
    assert cloud.device_labels["meross:abc123"] == "Roof Plug"
    assert cloud.pump_labels["roof_sump"] == "2nd fl rm 206"


def test_diff_meross_cloud_names_fills_unlabeled_pumps() -> None:
    config = AppConfig(
        pumps=[
            PumpConfig(
                name="roof_sump",
                meross=MerossConfig(device_uuid="abc123", channel=1),
            )
        ],
    )
    cloud = collect_meross_cloud_names(
        [
            _meross_info(
                uuid="abc123",
                dev_name="Roof Plug",
                channels=[{"channel": 1, "type": "Switch", "devName": "2nd fl rm 206"}],
            )
        ],
        config.pumps,
    )
    pump_updates, device_updates = diff_meross_cloud_names(config, cloud)
    assert pump_updates == {"roof_sump": "2nd fl rm 206"}
    assert device_updates == {"meross:abc123": "Roof Plug"}


def test_diff_meross_cloud_names_preserves_explicit_local_labels() -> None:
    config = AppConfig(
        pumps=[
            PumpConfig(
                name="roof_sump",
                label="User Switch Name",
                meross=MerossConfig(device_uuid="abc123", channel=1),
            )
        ],
        device_labels=[
            DeviceLabelOverride(
                device_backend="meross",
                device_id="abc123",
                label="User Device Name",
            )
        ],
    )
    cloud = collect_meross_cloud_names(
        [
            _meross_info(
                uuid="abc123",
                dev_name="Roof Plug",
                channels=[{"channel": 1, "type": "Switch", "devName": "2nd fl rm 206"}],
            )
        ],
        config.pumps,
    )
    pump_updates, device_updates = diff_meross_cloud_names(config, cloud)
    assert pump_updates == {}
    assert device_updates == {}


@pytest.mark.asyncio
async def test_sync_meross_display_names_from_cloud_persists(tmp_path: Path) -> None:
    from app.db import init_db
    from app.service import PumpService

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "meross:",
                "  email: user@example.com",
                "  password: secret",
                "pumps:",
                "  - name: roof_sump",
                "    meross:",
                "      device_uuid: abc123",
                "      channel: 1",
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
            _meross_info(
                uuid="abc123",
                dev_name="Roof Plug",
                channels=[{"channel": 1, "type": "Switch", "devName": "2nd fl rm 206"}],
            )
        ]
    )

    result = await service.sync_meross_display_names_from_cloud()
    assert result["updated"] is True
    assert result["pump_labels"]["roof_sump"] == "2nd fl rm 206"

    loaded = load_config(cfg_path)
    assert pump_display_label(loaded.pumps[0]) == "2nd fl rm 206"
    assert device_labels_map(loaded)["meross:abc123"] == "Roof Plug"


@pytest.mark.asyncio
async def test_sync_meross_display_names_skips_explicit_user_labels(tmp_path: Path) -> None:
    from app.db import init_db
    from app.service import PumpService

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "meross:",
                "  email: user@example.com",
                "  password: secret",
                "pumps:",
                "  - name: roof_sump",
                "    label: User Switch Name",
                "    meross:",
                "      device_uuid: abc123",
                "      channel: 1",
                "device_labels:",
                "  - device_backend: meross",
                "    device_id: abc123",
                "    label: User Device Name",
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
            _meross_info(
                uuid="abc123",
                dev_name="Roof Plug",
                channels=[{"channel": 1, "type": "Switch", "devName": "2nd fl rm 206"}],
            )
        ]
    )

    result = await service.sync_meross_display_names_from_cloud()
    assert result["updated"] is False

    loaded = load_config(cfg_path)
    assert pump_display_label(loaded.pumps[0]) == "User Switch Name"
    assert device_labels_map(loaded)["meross:abc123"] == "User Device Name"


@pytest.mark.asyncio
async def test_get_pump_cards_syncs_meross_names(service: PumpService) -> None:
    refresh = AsyncMock(return_value={"refreshed": False, "cached": True})
    service.refresh_meross_ui_state = refresh  # type: ignore[method-assign]
    service.probe_pumps_online = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._ensure_pump_rows()

    await service.get_pump_cards(refresh_cloud=True)

    refresh.assert_awaited_once_with(force=False)


@pytest.mark.asyncio
async def test_refresh_all_pump_online_status_calls_name_sync(
    service: PumpService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = AsyncMock(
        return_value={
            "refreshed": True,
            "online_map": {"pump_a": {"status": "online", "detail": "mock:on"}},
        }
    )
    monkeypatch.setattr(service, "refresh_meross_ui_state", sync)
    service.config.pumps[0].meross = MerossConfig(device_uuid="uuid-1", channel=1)
    service.meross_session = MagicMock()
    service.meross_session.configured = True
    service._ensure_pump_rows()
    for name in ("pump_a", "pump_b", "pump_c"):
        service.devices[name] = MagicMock()
        service.devices[name].has_control_path.return_value = True
        service.devices[name].tuya = None
        service.devices[name].probe_connectivity = AsyncMock(
            return_value={"status": "online", "detail": "mock:on"}
        )
    service.meross_session.clear_togglex_cache = MagicMock()  # type: ignore[method-assign]
    service.meross_session.online_status_map = AsyncMock(return_value={})  # type: ignore[method-assign]

    await service.refresh_all_pump_online_status()
    sync.assert_awaited_once_with(force=True)


def test_merge_device_label_rows_preserves_unrelated_devices() -> None:
    existing = [
        DeviceLabelOverride(device_backend="tuya", device_id="x", label="Tuya Plug"),
        DeviceLabelOverride(device_backend="meross", device_id="old", label="Old"),
    ]
    merged = merge_device_label_rows(
        existing,
        {"meross:abc123": "Roof Plug"},
    )
    labels = {
        f"{row.device_backend}:{row.device_id}": row.label for row in merged
    }
    assert labels["tuya:x"] == "Tuya Plug"
    assert labels["meross:old"] == "Old"
    assert labels["meross:abc123"] == "Roof Plug"


@pytest.mark.asyncio
async def test_rename_meross_device_delegates_to_session() -> None:
    session = MagicMock()
    session.configured = True
    session.started = True
    session.startup = AsyncMock()
    session.update_cloud_device_name = AsyncMock(
        return_value={"success": True, "message": "updated device name in Meross cloud"}
    )
    result = await rename_meross_device(session, "uuid-1", "New Device")
    assert result["success"] is True
    session.update_cloud_device_name.assert_awaited_once_with("uuid-1", "New Device")


@pytest.mark.asyncio
async def test_rename_meross_switch_delegates_to_session() -> None:
    session = MagicMock()
    session.configured = True
    session.started = True
    session.startup = AsyncMock()
    session.list_cloud_devices = AsyncMock(
        return_value=[
            _meross_info(
                uuid="uuid-1",
                dev_name="Roof Plug",
                channels=[{"channel": 1, "type": "Switch", "devName": "Old"}],
            )
        ]
    )
    session.update_cloud_switch_name = AsyncMock(
        return_value={"success": True, "message": "updated outlet name in Meross cloud"}
    )
    result = await rename_meross_switch(session, "uuid-1", 1, "2nd fl rm 206")
    assert result["success"] is True
    session.update_cloud_switch_name.assert_awaited_once()
