"""Tuya local device adapter."""

from __future__ import annotations

import asyncio

import tinytuya

from app.device_import import switch_code_to_dp
from app.devices.base import CommandResult, DeviceState, PumpDevice


class TuyaLocalDevice(PumpDevice):
    def __init__(
        self,
        name: str,
        device_id: str,
        ip: str,
        local_key: str,
        version: float,
        *,
        switch_code: str = "",
        switch_dp: str = "",
    ) -> None:
        self.name = name
        self._switch_dp = switch_dp or switch_code_to_dp(switch_code or "switch_1")
        self._device = tinytuya.OutletDevice(device_id, ip, local_key)
        self._device.set_version(version)

    async def turn_on(self) -> CommandResult:
        return await self._set(True)

    async def turn_off(self) -> CommandResult:
        return await self._set(False)

    async def _set(self, on: bool) -> CommandResult:
        try:
            switch_index = int(self._switch_dp)
            await asyncio.to_thread(self._device.set_status, on, switch_index)
            return CommandResult(success=True, adapter="tuya_local")
        except Exception as exc:
            return CommandResult(success=False, adapter="tuya_local", message=str(exc))

    async def get_state(self) -> DeviceState:
        try:
            status = await asyncio.to_thread(self._device.status)
            if not status or "dps" not in status:
                return DeviceState.UNKNOWN
            dps = status["dps"]
            switch_val = dps.get(self._switch_dp)
            if switch_val is None and self._switch_dp != "1":
                switch_val = dps.get("1")
            if switch_val is None and dps:
                switch_val = next(iter(dps.values()), None)
            if switch_val in (True, "true", "True", 1, "1"):
                return DeviceState.ON
            if switch_val in (False, "false", "False", 0, "0"):
                return DeviceState.OFF
            return DeviceState.UNKNOWN
        except Exception:
            return DeviceState.UNKNOWN
