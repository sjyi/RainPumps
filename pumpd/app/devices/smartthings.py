"""SmartThings cloud device adapter."""

from __future__ import annotations

import httpx

from app.devices.base import CommandResult, DeviceState, PumpDevice

ST_BASE = "https://api.smartthings.com/v1"


class SmartThingsDevice(PumpDevice):
    def __init__(self, name: str, device_id: str, pat: str) -> None:
        self.name = name
        self.device_id = device_id
        self.pat = pat

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.pat}",
            "Content-Type": "application/json",
        }

    async def turn_on(self) -> CommandResult:
        return await self._command("on")

    async def turn_off(self) -> CommandResult:
        return await self._command("off")

    async def _command(self, command: str) -> CommandResult:
        if not self.pat or not self.device_id:
            return CommandResult(success=False, adapter="smartthings", message="not configured")
        url = f"{ST_BASE}/devices/{self.device_id}/commands"
        body = {"commands": [{"component": "main", "capability": "switch", "command": command}]}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
                resp.raise_for_status()
            return CommandResult(success=True, adapter="smartthings")
        except Exception as exc:
            return CommandResult(success=False, adapter="smartthings", message=str(exc))

    async def get_state(self) -> DeviceState:
        if not self.pat or not self.device_id:
            return DeviceState.UNKNOWN
        url = f"{ST_BASE}/devices/{self.device_id}/status"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
            data = resp.json()
            switch = (
                data.get("components", {})
                .get("main", {})
                .get("switch", {})
                .get("switch", {})
                .get("value")
            )
            if switch == "on":
                return DeviceState.ON
            if switch == "off":
                return DeviceState.OFF
            return DeviceState.UNKNOWN
        except Exception:
            return DeviceState.UNKNOWN
