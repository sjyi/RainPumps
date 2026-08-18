"""Meross UI cloud refresh caching behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import PumpConfig, load_config
from app.db import init_db
from app.service import PumpService


@pytest.fixture
def service(tmp_path):
    import shutil

    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = load_config(config_path)
    cfg.database_url = f"sqlite:///{tmp_path / 'test.db'}"
    cfg.pumps = [PumpConfig(name="pump_a")]
    session_factory = init_db(cfg.database_url)
    svc = PumpService(cfg, session_factory, config_path=str(config_path))
    svc.meross_session = MagicMock()
    svc.meross_session.configured = True
    svc.meross_session.started = True
    return svc


@pytest.mark.asyncio
async def test_refresh_meross_ui_state_uses_sixty_second_cache(service: PumpService) -> None:
    service.config.pumps[0].meross.device_uuid = "uuid-1"
    sync = AsyncMock(return_value={"updated": False})
    probe = AsyncMock(return_value={"pump_a": {"status": "online", "detail": "meross_cloud:on"}})
    service.sync_meross_display_names_from_cloud = sync  # type: ignore[method-assign]
    service.probe_pumps_online = probe  # type: ignore[method-assign]

    first = await service.refresh_meross_ui_state(force=False)
    second = await service.refresh_meross_ui_state(force=False)

    assert first["refreshed"] is True
    assert second["cached"] is True
    sync.assert_awaited_once()
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_meross_ui_state_force_bypasses_cache(service: PumpService) -> None:
    service.config.pumps[0].meross.device_uuid = "uuid-1"
    service.sync_meross_display_names_from_cloud = AsyncMock(  # type: ignore[method-assign]
        return_value={"updated": False}
    )
    service.probe_pumps_online = AsyncMock(  # type: ignore[method-assign]
        return_value={"pump_a": {"status": "online", "detail": "meross_cloud:on"}}
    )

    await service.refresh_meross_ui_state(force=False)
    await service.refresh_meross_ui_state(force=True)

    assert service.sync_meross_display_names_from_cloud.await_count == 2  # type: ignore[attr-defined]
    assert service.probe_pumps_online.await_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_pump_cards_without_refresh_uses_cache_only(service: PumpService) -> None:
    service.refresh_meross_ui_state = AsyncMock()  # type: ignore[method-assign]
    service.probe_pumps_online = AsyncMock(  # type: ignore[method-assign]
        return_value={"pump_a": {"status": "online", "detail": "meross_cloud:on"}}
    )
    service._ensure_pump_rows()

    await service.get_pump_cards(refresh_cloud=False)

    service.refresh_meross_ui_state.assert_not_awaited()
    service.probe_pumps_online.assert_awaited_once_with(use_cache_only=True)


@pytest.mark.asyncio
async def test_get_pump_cards_with_refresh_respects_ui_cache(service: PumpService) -> None:
    refresh = AsyncMock(return_value={"refreshed": False, "cached": True})
    service.refresh_meross_ui_state = refresh  # type: ignore[method-assign]
    service.probe_pumps_online = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._ensure_pump_rows()

    await service.get_pump_cards(refresh_cloud=True)

    refresh.assert_awaited_once_with(force=False)
    service.probe_pumps_online.assert_awaited_once_with(use_cache_only=True)
