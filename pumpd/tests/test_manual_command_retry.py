"""Manual device command timeout + retry tests."""

from __future__ import annotations

import asyncio

import pytest

from app.config import ApiConfig, AppConfig
from app.devices.base import CommandResult, DeviceState, PumpDevice
from app.service import PumpService


class SlowThenOkDevice(PumpDevice):
    name = "slow"

    def __init__(self) -> None:
        self.calls = 0

    async def turn_on(self) -> CommandResult:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1.0)
        return CommandResult(success=True, adapter="fake")

    async def turn_off(self) -> CommandResult:
        return CommandResult(success=True, adapter="fake")

    async def get_state(self) -> DeviceState:
        return DeviceState.OFF


class AlwaysSlowDevice(PumpDevice):
    name = "slow"

    async def turn_on(self) -> CommandResult:
        await asyncio.sleep(1.0)
        return CommandResult(success=True, adapter="fake")

    async def turn_off(self) -> CommandResult:
        await asyncio.sleep(1.0)
        return CommandResult(success=True, adapter="fake")

    async def get_state(self) -> DeviceState:
        return DeviceState.UNKNOWN


def _service_with_device(device: PumpDevice, *, timeout: float = 0.05) -> PumpService:
    service = object.__new__(PumpService)
    service.config = AppConfig(api=ApiConfig(device_command_timeout_seconds=timeout))
    service.devices = {"p1": device}
    service._log_event = lambda *args, **kwargs: None  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
async def test_manual_command_retries_after_timeout() -> None:
    device = SlowThenOkDevice()
    service = _service_with_device(device)

    result = await service._manual_device_command("p1", turn_on=True)

    assert result.success is True
    assert result.retried is True
    assert result.timed_out is True
    assert result.status_before_retry == "off"
    assert device.calls == 2


@pytest.mark.asyncio
async def test_manual_command_fails_when_retry_also_times_out() -> None:
    device = AlwaysSlowDevice()
    service = _service_with_device(device)

    result = await service._manual_device_command("p1", turn_on=True)

    assert result.success is False
    assert result.retried is True
    assert result.timed_out is True
    assert "retry also timed out" in result.message
