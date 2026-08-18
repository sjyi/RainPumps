"""Delayed cloud verification and command retries."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import ApiConfig, AppConfig, DevicesConfig, NotificationsConfig, SmtpConfig
from app.devices.base import CommandResult, DeviceState
from app.service import PumpService


class LaggingCloudDevice:
    """Simulates cloud state that updates after the second command."""

    def __init__(self) -> None:
        self.commands = 0
        self._on = False

    async def turn_on(self, *, verify: bool = False) -> CommandResult:
        self.commands += 1
        self._on = True
        return CommandResult(success=True, adapter="tuya_cloud")

    async def turn_off(self, *, verify: bool = False) -> CommandResult:
        self.commands += 1
        self._on = False
        return CommandResult(success=True, adapter="tuya_cloud")

    async def read_cloud_state(self) -> tuple[DeviceState, str | None]:
        if self.commands >= 2:
            return (DeviceState.ON if self._on else DeviceState.OFF), "tuya_cloud"
        return DeviceState.OFF if self._on else DeviceState.OFF, "tuya_cloud"

    async def get_state(self) -> DeviceState:
        state, _ = await self.read_cloud_state()
        return state


class NeverChangesDevice(LaggingCloudDevice):
    async def read_cloud_state(self) -> tuple[DeviceState, str | None]:
        return DeviceState.OFF, "tuya_cloud"


class SlowSendDevice:
    """Send hangs (Tuya API slow) but cloud state updates on the device."""

    def __init__(self) -> None:
        self._on = False

    async def turn_on(self, *, verify: bool = False) -> CommandResult:
        self._on = True
        await asyncio.sleep(60)
        return CommandResult(success=True, adapter="tuya_cloud")

    async def turn_off(self, *, verify: bool = False) -> CommandResult:
        self._on = False
        await asyncio.sleep(60)
        return CommandResult(success=True, adapter="tuya_cloud")

    async def read_cloud_state(self) -> tuple[DeviceState, str | None]:
        return (DeviceState.ON if self._on else DeviceState.OFF), "tuya_cloud"

    async def get_state(self) -> DeviceState:
        state, _ = await self.read_cloud_state()
        return state


def _service(device: LaggingCloudDevice, *, delay: float = 0.01) -> PumpService:
    service = object.__new__(PumpService)
    service.config = AppConfig(
        api=ApiConfig(device_command_timeout_seconds=5),
        devices=DevicesConfig(
            command_verify_delay_seconds=delay,
            command_verify_max_attempts=3,
        ),
        notifications=NotificationsConfig(
            admin_email="admin@example.com",
            smtp=SmtpConfig(enabled=True, host="smtp.test", to_addrs=["fallback@example.com"]),
        ),
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
async def test_command_succeeds_on_second_cloud_verify() -> None:
    device = LaggingCloudDevice()
    service = _service(device)

    result = await service._command_with_cloud_verify(
        "p1",
        turn_on=True,
        reason="test",
        event_type="turn_on",
    )

    assert result.success is True
    assert result.verify_attempts == 2
    assert device.commands == 2
    service.notifier.send_admin_email.assert_not_called()


@pytest.mark.asyncio
async def test_command_succeeds_after_send_timeout_via_cloud_verify() -> None:
    device = SlowSendDevice()
    service = _service(device, delay=0.01)

    result = await service._command_with_cloud_verify(
        "p1",
        turn_on=True,
        reason="manual turn_on",
        event_type="turn_on",
    )

    assert result.success is True
    assert result.verify_attempts == 1
    assert "send timed out" in (result.message or "")
    service.notifier.send_admin_email.assert_not_called()


@pytest.mark.asyncio
async def test_command_emails_admin_after_three_failures() -> None:
    device = NeverChangesDevice()
    service = _service(device)

    result = await service._command_with_cloud_verify(
        "p1",
        turn_on=True,
        reason="rain detected",
        event_type="turn_on",
    )

    assert result.success is False
    assert result.verify_attempts == 3
    assert device.commands == 3
    service.notifier.send_admin_email.assert_awaited_once()
    subject = service.notifier.send_admin_email.await_args.args[0]
    assert "p1" in subject
    assert "turn_on" in subject
