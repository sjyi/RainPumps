"""Manual device command uses delayed cloud verification."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import ApiConfig, AppConfig, DevicesConfig
from app.devices.base import CommandResult, DeviceState
from app.service import PumpService


class QuickOkDevice:
    async def turn_on(self, *, verify: bool = False) -> CommandResult:
        return CommandResult(success=True, adapter="fake")

    async def turn_off(self, *, verify: bool = False) -> CommandResult:
        return CommandResult(success=True, adapter="fake")

    async def read_cloud_state(self) -> tuple[DeviceState, str | None]:
        return DeviceState.ON, "fake"

    async def get_state(self) -> DeviceState:
        return DeviceState.ON


def _service_with_device(device: QuickOkDevice, *, delay: float = 0.01) -> PumpService:
    service = object.__new__(PumpService)
    service.config = AppConfig(
        api=ApiConfig(device_command_timeout_seconds=5),
        devices=DevicesConfig(command_verify_delay_seconds=delay, command_verify_max_attempts=3),
    )
    service.devices = {"p1": device}
    service.locks = {"p1": asyncio.Lock()}
    service._log_event = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service.hardware = AsyncMock()
    service.hardware.record_pump_success = lambda name: None
    service.hardware.record_pump_failure = lambda *args, **kwargs: None
    service.notifier = AsyncMock()
    service.gmail_client = MagicMock(connected=False)
    service.notifier.send_admin_email = AsyncMock()
    service.notifier.send = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_manual_command_verifies_via_cloud() -> None:
    device = QuickOkDevice()
    service = _service_with_device(device)

    result = await service._manual_device_command("p1", turn_on=True)

    assert result.success is True
    assert result.verify_attempts == 1
