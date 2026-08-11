"""Composite pump device — local, cloud, and SmartThings adapters."""

from __future__ import annotations

import asyncio
import logging

from app.devices.base import CommandResult, DeviceState, PumpDevice

logger = logging.getLogger(__name__)


class CompositePumpDevice(PumpDevice):
    def __init__(
        self,
        name: str,
        tuya: PumpDevice | None = None,
        smartthings: PumpDevice | None = None,
        *,
        tuya_cloud: PumpDevice | None = None,
        meross_cloud: PumpDevice | None = None,
        control_mode: str = "auto",
        retries: int = 3,
    ) -> None:
        self.name = name
        self.tuya = tuya
        self.tuya_cloud = tuya_cloud
        self.meross_cloud = meross_cloud
        self.smartthings = smartthings
        self.control_mode = control_mode
        self.retries = retries
        self._last_adapter: str | None = None

    def _adapters(self) -> list[tuple[str, PumpDevice]]:
        local = ("tuya_local", self.tuya)
        cloud = ("tuya_cloud", self.tuya_cloud)
        meross = ("meross_cloud", self.meross_cloud)
        st = ("smartthings", self.smartthings)
        if self.control_mode == "cloud":
            chain = [cloud, meross, st]
        elif self.control_mode == "local":
            chain = [local, meross, st]
        else:
            chain = [local, cloud, meross, st]
        return [(name, device) for name, device in chain if device is not None]

    def has_control_path(self) -> bool:
        return bool(self._adapters())

    async def turn_on(self) -> CommandResult:
        return await self._execute("on")

    async def turn_off(self) -> CommandResult:
        return await self._execute("off")

    async def _execute(self, action: str) -> CommandResult:
        adapters = self._adapters()
        last_error = "no adapters configured"
        for index, (adapter_name, device) in enumerate(adapters):
            attempts = self.retries if adapter_name == "tuya_local" else 1
            for attempt in range(attempts):
                result = await (device.turn_on() if action == "on" else device.turn_off())
                if result.success:
                    self._last_adapter = adapter_name
                    verified = await self._verify(adapter_name, action == "on")
                    if verified:
                        return result
                    last_error = f"verify failed on {adapter_name}"
                    fallback = await self._try_fallback(action, adapters, index)
                    if fallback.success:
                        return fallback
                else:
                    last_error = result.message
                if adapter_name == "tuya_local" and attempt < attempts - 1:
                    await asyncio.sleep(0.5)
        return CommandResult(success=False, adapter="composite", message=last_error)

    async def _try_fallback(
        self,
        action: str,
        adapters: list[tuple[str, PumpDevice]],
        current_index: int,
    ) -> CommandResult:
        for adapter_name, device in adapters[current_index + 1 :]:
            result = await (device.turn_on() if action == "on" else device.turn_off())
            if result.success:
                self._last_adapter = adapter_name
                return result
        return CommandResult(success=False, adapter="composite", message="fallback failed")

    def _device_for_adapter(self, adapter_name: str) -> PumpDevice | None:
        mapping = {
            "tuya_local": self.tuya,
            "tuya_cloud": self.tuya_cloud,
            "meross_cloud": self.meross_cloud,
            "smartthings": self.smartthings,
        }
        return mapping.get(adapter_name)

    async def _verify(self, adapter_name: str, want_on: bool) -> bool:
        device = self._device_for_adapter(adapter_name)
        if device is None:
            return False
        state = await device.get_state()
        expected = DeviceState.ON if want_on else DeviceState.OFF
        if state == expected:
            return True
        logger.warning("verify mismatch on %s: wanted %s got %s", self.name, expected, state)
        return False

    async def get_state(self) -> DeviceState:
        if self._last_adapter:
            device = self._device_for_adapter(self._last_adapter)
            if device:
                state = await device.get_state()
                if state != DeviceState.UNKNOWN:
                    return state
        for _, device in self._adapters():
            state = await device.get_state()
            if state != DeviceState.UNKNOWN:
                return state
        return DeviceState.UNKNOWN
