"""Composite read_cloud_state tests."""

from __future__ import annotations

import pytest

from app.devices.base import CommandResult, DeviceState
from app.devices.composite import CompositePumpDevice


class FakeDevice:
    def __init__(self, name: str, state: DeviceState = DeviceState.OFF) -> None:
        self.name = name
        self.state = state

    async def turn_on(self) -> CommandResult:
        self.state = DeviceState.ON
        return CommandResult(True, self.name)

    async def turn_off(self) -> CommandResult:
        self.state = DeviceState.OFF
        return CommandResult(True, self.name)

    async def get_state(self) -> DeviceState:
        return self.state


@pytest.mark.asyncio
async def test_read_cloud_state_prefers_cloud_adapter() -> None:
    local = FakeDevice("tuya_local", DeviceState.ON)
    cloud = FakeDevice("tuya_cloud", DeviceState.OFF)
    dev = CompositePumpDevice("p1", local, None, tuya_cloud=cloud, control_mode="auto")
    state, adapter = await dev.read_cloud_state()
    assert state == DeviceState.OFF
    assert adapter == "tuya_cloud"

@pytest.mark.asyncio
async def test_turn_on_without_verify_skips_immediate_check() -> None:
    local = FakeDevice("tuya_local", DeviceState.OFF)
    dev = CompositePumpDevice("p1", local, None, control_mode="local")
    result = await dev.turn_on(verify=False)
    assert result.success is True
