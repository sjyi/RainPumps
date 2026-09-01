"""Meross cloud adapter tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.base import DeviceState
from app.devices.meross_cloud import (
    MerossCloudDevice,
    MerossCloudSession,
    channel_to_switch_code,
    iter_meross_outlets,
    meross_entry_channel,
    switch_code_to_channel,
)


def test_channel_switch_mapping() -> None:
    assert channel_to_switch_code(0) == "switch_1"
    assert channel_to_switch_code(1) == "switch_2"
    assert switch_code_to_channel("switch_3") == 2


def test_iter_meross_outlets_mss620_dual() -> None:
    channels = [
        {},
        {"type": "Switch", "devName": "Rm 303"},
        {"type": "Switch", "devName": "Switch 2"},
    ]
    outlets = iter_meross_outlets(channels, device_type="mss620")
    assert outlets == [(1, "Rm 303"), (2, "Switch 2")]


def test_meross_entry_channel_uses_index_when_missing() -> None:
    assert meross_entry_channel({"type": "Switch", "devName": "Outlet 2"}, 2) == 2
    assert meross_entry_channel({"channel": 1, "type": "Switch"}, 2) == 1


@pytest.mark.asyncio
async def test_update_cloud_switch_name_mss620_second_outlet() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    channels = [
        {},
        {"type": "Switch", "devName": "Switch 1"},
        {"type": "Switch", "devName": "dead"},
    ]
    base_info = {
        "uuid": "uuid-202",
        "devName": "2nd fl 202",
        "deviceType": "mss620",
        "channels": channels,
    }
    updated_info = {
        **base_info,
        "channels": [
            {},
            {"type": "Switch", "devName": "Switch 1"},
            {"type": "Switch", "devName": "live", "channel": 2},
        ],
    }
    session.cloud_post = AsyncMock(side_effect=[base_info, {}, updated_info])

    result = await session.update_cloud_switch_name(
        "uuid-202",
        2,
        "live",
        device_type="mss620",
        channels=channels,
    )
    assert result["success"] is True
    assert result["api_path"] == "/v1/Device/devInfo"
    post_call = session.cloud_post.await_args_list[1]
    assert post_call.args[0] == "/v1/Device/devInfo"
    payload = post_call.args[1]
    assert payload["channels"][2]["devName"] == "live"
    assert payload["channels"][2]["channel"] == 2


@pytest.mark.asyncio
async def test_update_cloud_switch_name_unsupported_when_not_persisted() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    channels = [
        {},
        {"type": "Switch", "devName": "Switch 1"},
        {"type": "Switch", "devName": "dead"},
    ]
    base_info = {
        "uuid": "uuid-202",
        "devName": "2nd fl 202",
        "deviceType": "mss620",
        "channels": channels,
    }
    session.cloud_post = AsyncMock(side_effect=[base_info, {}, base_info])

    result = await session.update_cloud_switch_name(
        "uuid-202",
        2,
        "live",
        device_type="mss620",
        channels=channels,
    )
    assert result["success"] is False
    assert result.get("unsupported") is True
    assert "read-only" in result["message"].lower()


@pytest.mark.asyncio
async def test_update_cloud_switch_name_missing_channel_returns_error() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    channels = [
        {},
        {"type": "Switch", "devName": "Switch 1"},
    ]
    session.cloud_post = AsyncMock(
        return_value={
            "uuid": "uuid-202",
            "devName": "2nd fl 202",
            "deviceType": "mss620",
            "channels": channels,
        }
    )
    result = await session.update_cloud_switch_name(
        "uuid-202",
        2,
        "live",
        device_type="mss620",
        channels=channels,
    )
    assert result["success"] is False
    assert "channel 2 not found" in result["message"].lower()
    assert session.cloud_post.await_count == 1


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
    session.read_channel_state = AsyncMock(return_value=None)  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 0, session)
    assert await pump.get_state() == DeviceState.UNKNOWN


@pytest.mark.asyncio
async def test_meross_device_get_state_on() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    session.read_channel_state = AsyncMock(return_value=True)  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 1, session)
    assert await pump.get_state() == DeviceState.ON
    session.read_channel_state.assert_awaited_once_with("uuid-1", 1, force=False)


@pytest.mark.asyncio
async def test_meross_device_get_state_force_refresh() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    session.read_channel_state = AsyncMock(return_value=False)  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 2, session)
    assert await pump.get_state(force=True) == DeviceState.OFF
    session.read_channel_state.assert_awaited_once_with("uuid-1", 2, force=True)


@pytest.mark.asyncio
async def test_meross_is_reachable_requires_enrollment() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    session._started = True
    session.online_status_map = AsyncMock(return_value={"uuid-1": True})  # type: ignore[method-assign]
    session.find_device = MagicMock(return_value=None)  # type: ignore[method-assign]
    session.rediscover = AsyncMock()  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 0, session)
    assert await pump.is_reachable() is False


@pytest.mark.asyncio
async def test_meross_is_reachable_when_enrolled() -> None:
    dev = MagicMock()
    session = MerossCloudSession(email="a@b.com", password="secret")
    session._started = True
    session.online_status_map = AsyncMock(return_value={"uuid-1": True})  # type: ignore[method-assign]
    session.find_device = MagicMock(return_value=dev)  # type: ignore[method-assign]

    pump = MerossCloudDevice("p1", "uuid-1", 0, session)
    assert await pump.is_reachable() is True


@pytest.mark.asyncio
async def test_meross_wait_for_devices() -> None:
    session = MerossCloudSession(email="a@b.com", password="secret")
    session._started = True
    session.startup = AsyncMock()  # type: ignore[method-assign]
    session.rediscover = AsyncMock()  # type: ignore[method-assign]

    seen: list[int] = []

    def find(uuid: str) -> MagicMock | None:
        seen.append(1)
        return MagicMock() if len(seen) >= 2 else None

    session.find_device = find  # type: ignore[method-assign]

    result = await session.wait_for_devices({"uuid-1"}, timeout=5.0, interval=0.01)
    assert result == {"uuid-1": True}
