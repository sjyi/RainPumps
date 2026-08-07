"""Tuya IoT Cloud device adapter (remote control, no LAN required)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.devices.base import CommandResult, DeviceState, PumpDevice

logger = logging.getLogger(__name__)

SWITCH_CODES = ("switch_1", "switch_2", "switch_3", "switch", "switch_led", "switch_usb1", "switch_usb2")


def _cloud_error(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("Error"):
            return str(result["Error"])
        if not result.get("success"):
            return str(result.get("msg", result))
    return "unknown Tuya cloud error"


def _switch_value(item: dict[str, Any]) -> DeviceState | None:
    val = item.get("value")
    if val in (True, "true", "True", 1, "1"):
        return DeviceState.ON
    if val in (False, "false", "False", 0, "0"):
        return DeviceState.OFF
    return None


def parse_cloud_switch_state(result: Any, *, switch_code: str | None = None) -> DeviceState:
    if not isinstance(result, dict) or not result.get("success"):
        return DeviceState.UNKNOWN
    items = result.get("result")
    if not isinstance(items, list):
        return DeviceState.UNKNOWN

    if switch_code:
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("code", "")) == switch_code:
                state = _switch_value(item)
                return state if state is not None else DeviceState.UNKNOWN
        return DeviceState.UNKNOWN

    for preferred in ("switch_1", "switch"):
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("code", "")) == preferred:
                state = _switch_value(item)
                if state is not None:
                    return state
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", ""))
        if not code.startswith("switch"):
            continue
        state = _switch_value(item)
        if state is not None:
            return state
    return DeviceState.UNKNOWN


class TuyaCloudDevice(PumpDevice):
    def __init__(self, name: str, device_id: str, cloud: Any, *, switch_code: str = "") -> None:
        self.name = name
        self.device_id = device_id
        self._cloud = cloud
        self._configured_switch_code = switch_code.strip()
        self._switch_code: str | None = self._configured_switch_code or None

    async def turn_on(self) -> CommandResult:
        return await self._set(True)

    async def turn_off(self) -> CommandResult:
        return await self._set(False)

    async def _set(self, on: bool) -> CommandResult:
        if self._configured_switch_code:
            codes = [self._configured_switch_code]
        else:
            codes = [self._switch_code] if self._switch_code else []
            codes.extend(c for c in SWITCH_CODES if c not in codes)
        last_error = "no switch code succeeded"
        for code in codes:
            if not code:
                continue
            body = {"commands": [{"code": code, "value": on}]}
            try:
                result = await asyncio.to_thread(
                    self._cloud.sendcommand,
                    self.device_id,
                    body,
                )
            except Exception as exc:
                last_error = str(exc)
                continue
            if isinstance(result, dict) and result.get("success"):
                self._switch_code = code
                return CommandResult(success=True, adapter="tuya_cloud")
            last_error = _cloud_error(result)
        return CommandResult(success=False, adapter="tuya_cloud", message=last_error)

    async def get_state(self) -> DeviceState:
        try:
            result = await asyncio.to_thread(self._cloud.getstatus, self.device_id)
        except Exception as exc:
            logger.debug("tuya cloud getstatus failed for %s: %s", self.name, exc)
            return DeviceState.UNKNOWN
        switch_code = self._configured_switch_code or self._switch_code
        return parse_cloud_switch_state(result, switch_code=switch_code or None)

    async def is_reachable(self) -> bool:
        try:
            online = await asyncio.to_thread(self._cloud.getconnectstatus, self.device_id)
        except Exception:
            return False
        return bool(online)
