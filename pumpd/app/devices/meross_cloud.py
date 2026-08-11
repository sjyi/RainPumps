"""Meross IoT Cloud device adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.devices.base import CommandResult, DeviceState, PumpDevice

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://iotx-us.meross.com"


def channel_to_switch_code(channel: int) -> str:
    return f"switch_{channel + 1}"


def switch_code_to_channel(switch_code: str) -> int:
    import re

    match = re.match(r"switch_(\d+)$", (switch_code or "switch_1").strip())
    if match:
        return int(match.group(1)) - 1
    return 0


class MerossCloudSession:
    """Shared Meross HTTP + MQTT session for all pumps on an account."""

    def __init__(
        self,
        *,
        email: str = "",
        password: str = "",
        api_base_url: str = DEFAULT_API_BASE,
        mfa_code: str = "",
    ) -> None:
        self._email = email.strip()
        self._password = password
        self._api_base_url = api_base_url.strip() or DEFAULT_API_BASE
        self._mfa_code = mfa_code.strip() or None
        self._http: Any | None = None
        self._manager: Any | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._online_cache: dict[str, bool] = {}

    @staticmethod
    def _info_is_online(info: Any) -> bool:
        online = getattr(info, "online_status", None)
        if online is None:
            return True
        name = getattr(online, "name", None)
        if isinstance(name, str):
            return name == "ONLINE"
        return str(online).endswith("ONLINE")

    async def online_status_map(self, *, refresh: bool = False) -> dict[str, bool]:
        """Cached Meross cloud online flags keyed by device UUID."""
        if refresh or not self._online_cache:
            devices = await self.list_cloud_devices()
            self._online_cache = {
                info.uuid: self._info_is_online(info)
                for info in devices
                if getattr(info, "uuid", "")
            }
        return dict(self._online_cache)

    @property
    def configured(self) -> bool:
        return bool(self._email and self._password)

    @property
    def started(self) -> bool:
        return self._started

    async def startup(self) -> None:
        if not self.configured:
            return
        async with self._lock:
            if self._started:
                return
            from meross_iot.http_api import MerossHttpClient
            from meross_iot.manager import MerossManager

            try:
                self._http = await MerossHttpClient.async_from_user_password(
                    api_base_url=self._api_base_url,
                    email=self._email,
                    password=self._password,
                    mfa_code=self._mfa_code,
                    auto_retry_on_bad_domain=True,
                )
                self._manager = MerossManager(http_client=self._http)
                await self._manager.async_init()
                await self._manager.async_device_discovery()
            except Exception:
                self._manager = None
                self._http = None
                self._started = False
                raise
            self._started = True

    async def shutdown(self) -> None:
        async with self._lock:
            if self._manager is not None:
                self._manager.close()
                self._manager = None
            if self._http is not None:
                try:
                    await self._http.async_logout()
                except Exception:
                    logger.debug("meross logout failed", exc_info=True)
                self._http = None
            self._started = False

    async def rediscover(self) -> None:
        if self._manager is None:
            return
        await self._manager.async_device_discovery()

    def find_device(self, device_uuid: str) -> Any | None:
        if self._manager is None:
            return None
        for dev in self._manager.find_devices():
            if dev.uuid == device_uuid:
                return dev
        return None

    async def list_cloud_devices(self) -> list[Any]:
        if self._http is None:
            from meross_iot.http_api import MerossHttpClient

            self._http = await MerossHttpClient.async_from_user_password(
                api_base_url=self._api_base_url,
                email=self._email,
                password=self._password,
                mfa_code=self._mfa_code,
                auto_retry_on_bad_domain=True,
            )
        return await self._http.async_list_devices()


class MerossCloudDevice(PumpDevice):
    def __init__(
        self,
        name: str,
        device_uuid: str,
        channel: int,
        session: MerossCloudSession,
    ) -> None:
        self.name = name
        self.device_uuid = device_uuid
        self.channel = channel
        self._session = session

    async def _resolve_device(self) -> Any | None:
        if not self._session.started:
            try:
                await self._session.startup()
            except Exception:
                logger.debug("meross session startup failed for %s", self.name, exc_info=True)
                return None
        dev = self._session.find_device(self.device_uuid)
        if dev is None:
            try:
                await self._session.rediscover()
            except Exception:
                logger.debug("meross rediscover failed for %s", self.name, exc_info=True)
                return None
            dev = self._session.find_device(self.device_uuid)
        return dev

    async def turn_on(self) -> CommandResult:
        return await self._set(True)

    async def turn_off(self) -> CommandResult:
        return await self._set(False)

    async def _set(self, on: bool) -> CommandResult:
        if not self._session.configured:
            return CommandResult(success=False, adapter="meross_cloud", message="not configured")
        dev = await self._resolve_device()
        if dev is None:
            return CommandResult(
                success=False,
                adapter="meross_cloud",
                message="device not enrolled (offline or unknown)",
            )
        try:
            await dev.async_update()
            if on:
                await dev.async_turn_on(channel=self.channel)
            else:
                await dev.async_turn_off(channel=self.channel)
        except Exception as exc:
            logger.warning("meross command failed for %s: %s", self.name, exc)
            return CommandResult(success=False, adapter="meross_cloud", message=str(exc))
        return CommandResult(success=True, adapter="meross_cloud")

    async def get_state(self) -> DeviceState:
        if not self._session.configured:
            return DeviceState.UNKNOWN
        dev = await self._resolve_device()
        if dev is None:
            return DeviceState.UNKNOWN
        try:
            await dev.async_update()
            if hasattr(dev, "is_on"):
                try:
                    on = dev.is_on(channel=self.channel)
                except TypeError:
                    on = dev.is_on()
                return DeviceState.ON if on else DeviceState.OFF
        except Exception as exc:
            logger.debug("meross get_state failed for %s: %s", self.name, exc)
        return DeviceState.UNKNOWN

    async def is_reachable(self) -> bool:
        if not self._session.configured:
            return False
        try:
            status_map = await self._session.online_status_map()
            if self.device_uuid in status_map:
                return status_map[self.device_uuid]
        except Exception:
            logger.debug("meross online lookup failed for %s", self.name, exc_info=True)
        return False
