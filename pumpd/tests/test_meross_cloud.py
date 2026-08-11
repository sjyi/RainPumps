"""Meross cloud adapter tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.base import DeviceState
from app.devices.meross_cloud import (
    MerossCloudDevice,
    MerossCloudSession,
    channel_to_switch_code,
    switch_code_to_channel,
)


def test_channel_switch_mapping() -> None:
    assert channel_to_switch_code(0) == "switch_1"
    assert channel_to_switch_code(1) == "switch_2"
    assert switch_code_to_channel("switch_3") == 2


@pytest.mark.asyncio
async def test_meross_device_turn_on() -> None:
    dev = MagicMock()
    dev.async_update = AsyncMock()
    dev.async_turn_on = AsyncMock()
    dev.is_on = MagicMock(return_value=True)

    session = MerossCloudSession(email="a@b.com", password="secret")
    session._started = True
    session.find_device = MagicMock(return_value=dev)  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 1, session)
    result = await pump.turn_on()
    assert result.success
    dev.async_turn_on.assert_awaited_once_with(channel=1)


@pytest.mark.asyncio
async def test_meross_device_get_state_offline() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    session._started = True
    session.find_device = MagicMock(return_value=None)  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 0, session)
    assert await pump.get_state() == DeviceState.UNKNOWN
