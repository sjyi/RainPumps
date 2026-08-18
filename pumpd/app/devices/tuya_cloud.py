"""Tuya IoT Cloud device adapter (remote control, no LAN required)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.devices.base import CommandResult, DeviceState, PumpDevice

logger = logging.getLogger(__name__)

SWITCH_CODES = ("switch_1", "switch_2", "switch_3", "switch", "switch_led", "switch_usb1", "switch_usb2")

# Tuya Open API error codes → short remediation hint (shown in probe detail).
TUYA_CLOUD_HINTS: dict[int, str] = {
    28841004: "Upgrade your Tuya IoT plan or switch to local LAN control.",
}


def _parse_error_payload(payload: Any) -> str | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    msg = payload.get("msg")
    if not msg:
        return None
    code = payload.get("code")
    text = f"{msg} (code {code})" if code else str(msg)
    hint = TUYA_CLOUD_HINTS.get(code) if isinstance(code, int) else None
    return f"{text} — {hint}" if hint else text


def _cloud_error(result: Any) -> str:
    if not isinstance(result, dict):
        return "unknown Tuya cloud error"

    if result.get("msg") and not result.get("Error"):
        code = result.get("code")
        msg = str(result["msg"])
        text = f"{msg} (code {code})" if code else msg
        hint = TUYA_CLOUD_HINTS.get(code) if isinstance(code, int) else None
        return f"{text} — {hint}" if hint else text

    payload_msg = _parse_error_payload(result.get("Payload"))
    if payload_msg:
        return payload_msg

    if result.get("Error"):
        return str(result["Error"])

    if not result.get("success"):
        return str(result.get("msg", result))
    return "unknown Tuya cloud error"


def cloud_error_blocks_local_fallback(result: Any) -> bool:
    """Quota/auth failures may still reach devices on LAN."""
    if not isinstance(result, dict):
        return False
    code = result.get("code")
    if code == 28841004:
        return False
    payload = result.get("Payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict) and payload.get("code") == 28841004:
        return False
    return True


def _status_items(result: Any) -> list[dict[str, Any]] | None:
    if not isinstance(result, dict):
        return None
    items = result.get("result")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if isinstance(items, dict):
        nested = items.get("status")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return None


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
    items = _status_items(result)
    if not items:
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

    async def probe_online(self, *, timeout: float = 8.0) -> tuple[str, str]:
        """Return (status, detail) for connectivity: online, offline, or cloud_error."""
        self._last_cloud_error: Any = None
        try:
            online = await asyncio.wait_for(
                asyncio.to_thread(self._cloud.getconnectstatus, self.device_id),
                timeout=timeout,
            )
        except TimeoutError:
            return "offline", "tuya_cloud:timeout"
        except Exception as exc:
            return "cloud_error", f"tuya_cloud:{exc}"

        if isinstance(online, dict):
            self._last_cloud_error = online
            return "cloud_error", f"tuya_cloud:{_cloud_error(online)}"
        if not online:
            return "offline", "tuya_cloud:device offline"

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._cloud.getstatus, self.device_id),
                timeout=timeout,
            )
        except TimeoutError:
            return "online", "tuya_cloud:reachable"
        except Exception as exc:
            return "online", f"tuya_cloud:reachable ({exc})"

        if isinstance(result, dict) and not result.get("success"):
            self._last_cloud_error = result
            return "online", f"tuya_cloud:reachable ({_cloud_error(result)})"

        switch_code = self._configured_switch_code or self._switch_code
        state = parse_cloud_switch_state(result, switch_code=switch_code or None)
        if state != DeviceState.UNKNOWN:
            return "online", f"tuya_cloud:{state.value}"
        return "online", "tuya_cloud:reachable"

    def last_cloud_error(self) -> Any:
        return getattr(self, "_last_cloud_error", None)

    async def is_reachable(self) -> bool:
        status, _ = await self.probe_online()
        return status == "online"
