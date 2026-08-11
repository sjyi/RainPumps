"""Composite device adapter tests."""

from __future__ import annotations

import pytest

from app.devices.base import CommandResult, DeviceState
from app.devices.composite import CompositePumpDevice


class FakeDevice:
    def __init__(self, name: str, fail: bool = False, state: DeviceState = DeviceState.OFF) -> None:
        self.name = name
        self.fail = fail
        self.state = state
        self.calls = 0

    async def turn_on(self) -> CommandResult:
        self.calls += 1
        if self.fail:
            return CommandResult(False, self.name, "fail")
        self.state = DeviceState.ON
        return CommandResult(True, self.name)

    async def turn_off(self) -> CommandResult:
        self.calls += 1
        if self.fail:
            return CommandResult(False, self.name, "fail")
        self.state = DeviceState.OFF
        return CommandResult(True, self.name)

    async def get_state(self) -> DeviceState:
        return self.state


@pytest.mark.asyncio
async def test_composite_falls_back_to_smartthings() -> None:
    tuya = FakeDevice("tuya_local", fail=True)
    st = FakeDevice("smartthings")
    dev = CompositePumpDevice("p1", tuya, st, retries=1)
    result = await dev.turn_on()
    assert result.success
    assert st.state == DeviceState.ON


@pytest.mark.asyncio
async def test_composite_tuya_success() -> None:
    tuya = FakeDevice("tuya_local")
    st = FakeDevice("smartthings")
    dev = CompositePumpDevice("p1", tuya, st, retries=3)
    result = await dev.turn_on()
    assert result.success
    assert tuya.state == DeviceState.ON
    assert st.calls == 0


@pytest.mark.asyncio
async def test_composite_cloud_only() -> None:
    cloud = FakeDevice("tuya_cloud")
    dev = CompositePumpDevice("p1", None, None, tuya_cloud=cloud, control_mode="cloud")
    result = await dev.turn_on()
    assert result.success
    assert cloud.state == DeviceState.ON


@pytest.mark.asyncio
async def test_composite_meross_cloud() -> None:
    meross = FakeDevice("meross_cloud")
    dev = CompositePumpDevice("p1", None, None, meross_cloud=meross, control_mode="cloud")
    result = await dev.turn_on()
    assert result.success
    assert meross.state == DeviceState.ON
