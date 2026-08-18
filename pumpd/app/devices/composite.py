"""Composite pump device — local, cloud, and SmartThings adapters."""

from __future__ import annotations

import asyncio
import logging

from app.devices.base import CommandResult, DeviceState, PumpDevice
from app.devices.tuya_cloud import cloud_error_blocks_local_fallback

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

    async def turn_on(self, *, verify: bool = False) -> CommandResult:
        return await self._execute("on", verify=verify)

    async def turn_off(self, *, verify: bool = False) -> CommandResult:
        return await self._execute("off", verify=verify)

    async def _execute(self, action: str, *, verify: bool = False) -> CommandResult:
        adapters = self._adapters()
        last_error = "no adapters configured"
        for index, (adapter_name, device) in enumerate(adapters):
            attempts = self.retries if adapter_name == "tuya_local" else 1
            for attempt in range(attempts):
                result = await (device.turn_on() if action == "on" else device.turn_off())
                if result.success:
                    self._last_adapter = adapter_name
                    if not verify:
                        return result
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

    async def read_cloud_state(self) -> tuple[DeviceState, str | None]:
        """Read switch state from cloud adapters (preferred for post-command verify)."""
        for adapter_name in ("tuya_cloud", "meross_cloud", "smartthings"):
            device = self._device_for_adapter(adapter_name)
            if device is None:
                continue
            try:
                state = await device.get_state()
            except Exception:
                logger.debug("cloud state read failed on %s for %s", adapter_name, self.name, exc_info=True)
                continue
            if state != DeviceState.UNKNOWN:
                self._last_adapter = adapter_name
                return state, adapter_name
        for adapter_name, device in self._adapters():
            try:
                state = await device.get_state()
            except Exception:
                continue
            if state != DeviceState.UNKNOWN:
                self._last_adapter = adapter_name
                return state, adapter_name
        return DeviceState.UNKNOWN, None

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
        for adapter_name, device in self._adapters():
            state = await device.get_state()
            if state != DeviceState.UNKNOWN:
                self._last_adapter = adapter_name
                return state
        return DeviceState.UNKNOWN

    async def probe_connectivity(
        self,
        *,
        cloud_timeout: float = 8.0,
        local_timeout: float = 4.0,
        local_device: PumpDevice | None = None,
        force: bool = False,
    ) -> dict[str, str]:
        """Cloud-first connectivity probe with optional LAN fallback."""
        last_detail = "unreachable"
        cloud_error_detail: str | None = None

        if self.tuya_cloud is not None:
            status, detail = await self.tuya_cloud.probe_online(timeout=cloud_timeout)
            last_detail = detail
            if status == "online":
                self._last_adapter = "tuya_cloud"
                return {"status": "online", "detail": detail}
            if status == "cloud_error":
                cloud_err = self.tuya_cloud.last_cloud_error()
                cloud_error_detail = detail
                if cloud_error_blocks_local_fallback(cloud_err):
                    return {"status": "cloud_error", "detail": detail}

        if self.meross_cloud is not None:
            try:
                reachable = await asyncio.wait_for(
                    self.meross_cloud.is_reachable(),
                    timeout=cloud_timeout,
                )
            except TimeoutError:
                last_detail = "meross_cloud:timeout"
            else:
                if reachable:
                    try:
                        state = await asyncio.wait_for(
                            self.meross_cloud.get_state(force=force),
                            timeout=cloud_timeout,
                        )
                    except (TimeoutError, Exception):
                        state = DeviceState.UNKNOWN
                    self._last_adapter = "meross_cloud"
                    if state != DeviceState.UNKNOWN:
                        return {"status": "online", "detail": f"meross_cloud:{state.value}"}
                    return {"status": "online", "detail": "meross_cloud:reachable"}
                last_detail = "meross_cloud:offline"

        if self.smartthings is not None:
            try:
                state = await asyncio.wait_for(
                    self.smartthings.get_state(),
                    timeout=cloud_timeout,
                )
            except TimeoutError:
                last_detail = "smartthings:timeout"
            except Exception as exc:
                last_detail = f"smartthings:{exc}"
            else:
                if state != DeviceState.UNKNOWN:
                    self._last_adapter = "smartthings"
                    return {"status": "online", "detail": f"smartthings:{state.value}"}
                last_detail = "smartthings:unreachable"

        local = local_device or self.tuya
        if local is not None:
            try:
                state = await asyncio.wait_for(local.get_state(), timeout=local_timeout)
            except TimeoutError:
                last_detail = "tuya_local:timeout"
            except Exception as exc:
                last_detail = f"tuya_local:{exc}"
            else:
                if state != DeviceState.UNKNOWN:
                    self._last_adapter = "tuya_local"
                    return {"status": "online", "detail": f"tuya_local:{state.value}"}
                last_detail = "tuya_local:unreachable"

        if cloud_error_detail:
            return {"status": "cloud_error", "detail": cloud_error_detail}
        return {"status": "offline", "detail": last_detail}
