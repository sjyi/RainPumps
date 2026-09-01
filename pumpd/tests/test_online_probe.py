"""Online probe cache, recovery, and API."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import PumpConfig, load_config
from app.db import init_db
from app.devices.base import DeviceState
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
    svc = PumpService(cfg, session_factory, config_path=str(config_path))
    return svc


def _mock_device(state: DeviceState) -> MagicMock:
    device = MagicMock()
    device.has_control_path.return_value = True
    device.get_state = AsyncMock(return_value=state)
    device._last_adapter = "mock"
    return device


@pytest.mark.asyncio
async def test_probe_partial_updates_only_requested_names(service: PumpService) -> None:
    async def fake_probe(**kwargs: object) -> dict[str, str]:
        return {"status": "online", "detail": "mock:on"}

    for name in ("pump_a", "pump_b", "pump_c"):
        service.devices[name] = MagicMock()
        service.devices[name].has_control_path.return_value = True
        service.devices[name].tuya = None
        service.devices[name].probe_connectivity = AsyncMock(side_effect=fake_probe)

    service._online_probe_cache = {
        "pump_a": {"status": "offline", "detail": "timeout"},
        "pump_b": {"status": "offline", "detail": "timeout"},
        "pump_c": {"status": "online", "detail": "mock:on"},
    }

    result = await service.probe_pumps_online(force=True, names=["pump_a", "pump_b"])

    assert result["pump_a"]["status"] == "online"
    assert result["pump_b"]["status"] == "online"
    assert result["pump_c"]["status"] == "online"
    assert service.devices["pump_a"].probe_connectivity.await_count == 1
    assert service.devices["pump_a"].probe_connectivity.await_args.kwargs.get("force") is True
    assert service.devices["pump_c"].probe_connectivity.await_count == 0


@pytest.mark.asyncio
async def test_recover_offline_reprobes_offline_only(service: PumpService) -> None:
    for name in ("pump_a", "pump_b", "pump_c"):
        service.devices[name] = MagicMock()
        service.devices[name].has_control_path.return_value = True
        service.devices[name].tuya = None
        service.devices[name].probe_connectivity = AsyncMock(
            return_value={"status": "online", "detail": "mock:on"}
        )

    service._online_probe_cache = {
        "pump_a": {"status": "offline", "detail": "timeout"},
        "pump_b": {"status": "unknown", "detail": ""},
        "pump_c": {"status": "online", "detail": "mock:on"},
    }

    result = await service.recover_offline_pump_status()

    assert result["pump_a"]["status"] == "online"
    assert result["pump_b"]["status"] == "online"
    assert result["pump_c"]["status"] == "online"
    assert service.devices["pump_a"].probe_connectivity.await_count == 1
    assert service.devices["pump_b"].probe_connectivity.await_count == 1
    assert service.devices["pump_c"].probe_connectivity.await_count == 0


@pytest.mark.asyncio
async def test_recover_offline_with_empty_cache_forces_full_probe(service: PumpService) -> None:
    for name in ("pump_a", "pump_b", "pump_c"):
        service.devices[name] = MagicMock()
        service.devices[name].has_control_path.return_value = True
        service.devices[name].tuya = None
        service.devices[name].probe_connectivity = AsyncMock(
            return_value={"status": "online", "detail": "mock:on"}
        )
    service._online_probe_cache = None

    result = await service.recover_offline_pump_status()

    assert len(result) == 3
    assert all(entry["status"] == "online" for entry in result.values())


@pytest.mark.asyncio
async def test_get_pump_cards_uses_live_probe_switch_state(service: PumpService) -> None:
    service._ensure_pump_rows()
    service.refresh_meross_ui_state = AsyncMock(  # type: ignore[method-assign]
        return_value={"refreshed": False, "cached": True}
    )
    service.probe_pumps_online = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "pump_a": {"status": "online", "detail": "meross_cloud:on"},
            "pump_b": {"status": "online", "detail": "meross_cloud:off"},
            "pump_c": {"status": "offline", "detail": "meross_cloud:timeout"},
        }
    )

    cards = await service.get_pump_cards()
    by_name = {card["name"]: card for card in cards}
    assert by_name["pump_a"]["device_on"] is True
    assert by_name["pump_b"]["device_on"] is False
    assert by_name["pump_c"]["device_on"] is False

    row = service._get_pump_row("pump_a")
    assert row is not None
    assert row.device_on is True


@pytest.mark.asyncio
async def test_get_pump_cards_shows_live_switch_state_in_manual_mode(service: PumpService) -> None:
    service._ensure_pump_rows()
    row = service._get_pump_row("pump_a")
    assert row is not None
    row.mode = "manual_on"
    row.device_on = False
    with service.session_factory() as session:
        session.merge(row)
        session.commit()

    service.refresh_meross_ui_state = AsyncMock(  # type: ignore[method-assign]
        return_value={"refreshed": False, "cached": True}
    )
    service.probe_pumps_online = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "pump_a": {"status": "online", "detail": "meross_cloud:on"},
            "pump_b": {"status": "online", "detail": "meross_cloud:off"},
            "pump_c": {"status": "offline", "detail": "meross_cloud:timeout"},
        }
    )

    cards = await service.get_pump_cards()
    by_name = {card["name"]: card for card in cards}
    assert by_name["pump_a"]["mode"] == "manual_on"
    assert by_name["pump_a"]["device_on"] is True

    row = service._get_pump_row("pump_a")
    assert row is not None
    assert row.device_on is False


@pytest.mark.asyncio
async def test_get_pump_cards_offline_clears_stale_on(service: PumpService) -> None:
    service._ensure_pump_rows()
    row = service._get_pump_row("pump_a")
    assert row is not None
    row.device_on = True
    with service.session_factory() as session:
        session.merge(row)
        session.commit()

    service.refresh_meross_ui_state = AsyncMock(  # type: ignore[method-assign]
        return_value={"refreshed": False, "cached": True}
    )
    service.probe_pumps_online = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "pump_a": {"status": "offline", "detail": "meross_cloud:offline"},
        }
    )

    cards = await service.get_pump_cards()
    assert cards[0]["device_on"] is False
    assert cards[0]["online_status"] == "offline"


@pytest.mark.asyncio
async def test_refresh_all_pump_online_status_syncs_switch_states(service: PumpService) -> None:
    service._ensure_pump_rows()
    for name in ("pump_a", "pump_b", "pump_c"):
        service.devices[name] = MagicMock()
        service.devices[name].has_control_path.return_value = True
        service.devices[name].tuya = None

    service.devices["pump_a"].probe_connectivity = AsyncMock(
        return_value={"status": "online", "detail": "meross_cloud:on"}
    )
    service.devices["pump_b"].probe_connectivity = AsyncMock(
        return_value={"status": "online", "detail": "meross_cloud:off"}
    )
    service.devices["pump_c"].probe_connectivity = AsyncMock(
        return_value={"status": "offline", "detail": "meross_cloud:timeout"}
    )
    service.meross_session.clear_togglex_cache = MagicMock()  # type: ignore[method-assign]
    service.meross_session.online_status_map = AsyncMock(return_value={})  # type: ignore[method-assign]

    await service.refresh_all_pump_online_status()

    assert service._get_pump_row("pump_a").device_on is True
    assert service._get_pump_row("pump_b").device_on is False
    assert service._get_pump_row("pump_c").device_on is False


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    import shutil

    from app.config import load_config as real_load_config

    db_path = tmp_path / "test.db"
    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = real_load_config(config_path)
    cfg.database_url = f"sqlite:///{db_path}"

    def _load_config(_path: str = "config.yaml"):
        loaded = real_load_config(config_path)
        loaded.database_url = f"sqlite:///{db_path}"
        return loaded

    monkeypatch.setattr("app.main.load_config", _load_config)
    monkeypatch.setattr("app.config.load_config", _load_config)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))

    from app.main import create_app

    app = create_app(str(config_path))
    with TestClient(app) as test_client:
        yield test_client


def test_probe_status_api(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.service.PumpService.refresh_all_pump_online_status",
        AsyncMock(
            return_value={
                "pump_a": {"status": "online", "detail": "mock:on"},
                "pump_b": {"status": "offline", "detail": "timeout"},
            }
        ),
    )

    response = client.post("/api/devices/probe-status")
    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["online"] == 1
    assert data["summary"]["offline"] == 1
    assert "Checked 2 pump(s)" in data["message"]
