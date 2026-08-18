"""Meross IoT Cloud device adapter."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.devices.base import CommandResult, DeviceState, PumpDevice

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://iotx-us.meross.com"

# Meross dual-outlet outdoor plugs (MSS620 family): ToggleX channels 1 and 2.
# Channel 0 is a virtual "Main channel"; physical outlets use 1 and 2.
_DUAL_OUTLET_PREFIXES = ("mss620",)


def channel_to_switch_code(channel: int) -> str:
    return f"switch_{channel + 1}"


def switch_code_to_channel(switch_code: str) -> int:
    import re

    match = re.match(r"switch_(\d+)$", (switch_code or "switch_1").strip())
    if match:
        return int(match.group(1)) - 1
    return 0


def _is_dual_outlet_device(device_type: str) -> bool:
    normalized = (device_type or "").lower()
    return any(normalized.startswith(prefix) for prefix in _DUAL_OUTLET_PREFIXES)


def iter_meross_outlets(
    channels: list[Any] | None,
    *,
    device_type: str = "",
) -> list[tuple[int, str]]:
    """Return (control_channel, label_suffix) for each physical Meross outlet.

    Meross cloud often lists three metadata entries for MSS620 dual-outlet plugs:
    an empty root slot plus two named Switch sub-devices. ToggleX control uses
    channels 1 and 2 (channel 0 is a virtual main channel).
    """
    if not isinstance(channels, list) or not channels:
        return [(0, "")]

    named: list[tuple[int, str]] = []
    for index, ch in enumerate(channels):
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("devName") or "").strip()
        ch_type = str(ch.get("type") or "").strip()
        if not name and not ch_type:
            continue
        if ch_type and ch_type.lower() != "switch":
            continue
        channel = int(ch.get("channel", index))
        named.append((channel, name or f"Switch {len(named) + 1}"))

    if _is_dual_outlet_device(device_type):
        if len(named) >= 2:
            return named[:2]
        labels = [name for _, name in named]
        while len(labels) < 2:
            labels.append(f"Switch {len(labels) + 1}")
        return [(1, labels[0]), (2, labels[1])]

    if named:
        return named
    if len(channels) > 1:
        return [(index, f"Switch {index + 1}") for index in range(len(channels))]
    return [(0, "")]


class MerossCloudSession:
    """Shared Meross HTTP + MQTT session for all pumps on an account."""

    def __init__(
        self,
        *,
        email: str = "",
        password: str = "",
        api_base_url: str = DEFAULT_API_BASE,
        mfa_code: str = "",
        lan_first: bool = False,
    ) -> None:
        self._email = email.strip()
        self._password = password
        self._api_base_url = api_base_url.strip() or DEFAULT_API_BASE
        self._mfa_code = mfa_code.strip() or None
        self._lan_first = lan_first
        self._http: Any | None = None
        self._manager: Any | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._online_cache: dict[str, bool] = {}
        self._togglex_cache: dict[str, tuple[float, dict[int, bool]]] = {}

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
            from meross_iot.manager import MerossManager, TransportMode

            try:
                self._http = await MerossHttpClient.async_from_user_password(
                    api_base_url=self._api_base_url,
                    email=self._email,
                    password=self._password,
                    mfa_code=self._mfa_code,
                    auto_retry_on_bad_domain=True,
                )
                self._manager = MerossManager(http_client=self._http)
                if self._lan_first:
                    self._manager.default_transport_mode = TransportMode.LAN_HTTP_FIRST_ONLY_GET
                else:
                    self._manager.default_transport_mode = TransportMode.MQTT_ONLY
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

    async def wait_for_devices(
        self,
        device_uuids: set[str],
        *,
        timeout: float = 30.0,
        interval: float = 2.0,
    ) -> dict[str, bool]:
        """Wait until configured UUIDs are enrolled in the local manager."""
        if not device_uuids or not self.configured:
            return {}
        if not self._started:
            await self.startup()

        deadline = time.monotonic() + timeout
        enrolled = {uuid: False for uuid in device_uuids}
        while time.monotonic() < deadline:
            pending = [uuid for uuid, ok in enrolled.items() if not ok]
            if not pending:
                break
            for uuid in pending:
                if self.find_device(uuid) is not None:
                    enrolled[uuid] = True
            if all(enrolled.values()):
                break
            try:
                await self.rediscover()
            except Exception:
                logger.debug("meross rediscover while waiting for devices failed", exc_info=True)
            for uuid in pending:
                if self.find_device(uuid) is not None:
                    enrolled[uuid] = True
            if all(enrolled.values()):
                break
            await asyncio.sleep(interval)
        return enrolled

    async def read_channel_state(
        self,
        device_uuid: str,
        channel: int,
        *,
        cache_ttl: float = 3.0,
        force: bool = False,
    ) -> bool | None:
        """Read live ON/OFF for one outlet via Meross cloud (MQTT)."""
        if not self.configured:
            return None
        now = time.monotonic()
        if not force:
            cached = self._togglex_cache.get(device_uuid)
            if cached and now - cached[0] < cache_ttl and channel in cached[1]:
                return cached[1][channel]

        dev = await self._resolve_enrolled_device(device_uuid)
        if dev is None:
            return None

        if force and hasattr(dev, "_channel_togglex_status"):
            dev._channel_togglex_status = {}

        from meross_iot.model.enums import Namespace

        try:
            await asyncio.wait_for(dev.async_update(), timeout=12.0)
        except Exception:
            logger.debug("meross async_update failed for %s", device_uuid[:8], exc_info=True)

        on: bool | None = None
        try:
            result = await dev._execute_command(
                method="GET",
                namespace=Namespace.CONTROL_TOGGLEX,
                payload={"togglex": {"channel": channel}},
                timeout=10.0,
            )
            togglex = result.get("togglex")
            if isinstance(togglex, list):
                togglex = togglex[0] if togglex else {}
            if isinstance(togglex, dict) and "onoff" in togglex:
                on = togglex["onoff"] == 1
                status_map: dict[int, bool] = dict(getattr(dev, "_channel_togglex_status", {}) or {})
                status_map[channel] = on
                dev._channel_togglex_status = status_map
        except Exception:
            logger.debug(
                "meross togglex GET failed for %s ch%s",
                device_uuid[:8],
                channel,
                exc_info=True,
            )

        if on is None and not force and hasattr(dev, "is_on"):
            try:
                cached_on = dev.is_on(channel=channel)
            except TypeError:
                cached_on = dev.is_on()
            if cached_on is not None:
                on = bool(cached_on)

        if on is not None:
            prior = self._togglex_cache.get(device_uuid)
            states = dict(prior[1]) if prior else {}
            states[channel] = on
            self._togglex_cache[device_uuid] = (now, states)
        return on

    async def _resolve_enrolled_device(self, device_uuid: str) -> Any | None:
        if not self._started:
            try:
                await self.startup()
            except Exception:
                logger.debug("meross startup before state read failed", exc_info=True)
                return None
        dev = self.find_device(device_uuid)
        if dev is None:
            try:
                await self.rediscover()
            except Exception:
                logger.debug("meross rediscover before state read failed", exc_info=True)
                return None
            dev = self.find_device(device_uuid)
        return dev

    def clear_togglex_cache(self) -> None:
        self._togglex_cache.clear()

    async def cloud_post(self, api_path: str, params: dict[str, Any]) -> Any:
        """Authenticated Meross HTTP POST (same signing as devList)."""
        if self._http is None:
            await self.startup()
        if self._http is None:
            raise RuntimeError("Meross HTTP client not available")
        from meross_iot.http_api import MerossHttpClient

        path = api_path if api_path.startswith("/") else f"/{api_path}"
        url = f"{self._api_base_url}{path}"
        return await MerossHttpClient._async_authenticated_post(
            url=url,
            params_data=params,
            cloud_creds=self._http.cloud_credentials,
            http_proxy=self._http._http_proxy,
            ua_header=self._http._ua_header,
            app_type=self._http._app_type,
            app_version=self._http._app_version,
            stats_counter=self._http._stats_counter,
        )

    async def update_cloud_device_name(self, device_uuid: str, dev_name: str) -> dict[str, Any]:
        cleaned = (dev_name or "").strip()
        if not cleaned:
            return {"success": False, "message": "missing device name"}
        last_error = "Meross cloud did not accept device name update"
        for path in (
            "/v1/Device/setInfo",
            "/v1/Device/devInfo/set",
            "/v1/Device/changeDevName",
        ):
            try:
                await self.cloud_post(path, {"uuid": device_uuid, "devName": cleaned})
                return {"success": True, "message": "updated device name in Meross cloud"}
            except Exception as exc:
                last_error = str(exc)
                logger.debug(
                    "meross device rename via %s failed for %s: %s",
                    path,
                    device_uuid[:8],
                    exc,
                )
        return {"success": False, "message": last_error}

    async def update_cloud_switch_name(
        self,
        device_uuid: str,
        channel: int,
        name: str,
        *,
        device_type: str = "",
        channels: list[Any] | None = None,
    ) -> dict[str, Any]:
        cleaned = (name or "").strip()
        if not cleaned:
            return {"success": False, "message": "missing switch name"}
        outlets = iter_meross_outlets(channels, device_type=device_type)
        if len(outlets) <= 1:
            return await self.update_cloud_device_name(device_uuid, cleaned)
        if not isinstance(channels, list):
            return {"success": False, "message": "missing Meross channel metadata"}
        updated_channels: list[Any] = []
        changed = False
        for entry in channels:
            if not isinstance(entry, dict):
                updated_channels.append(entry)
                continue
            row = dict(entry)
            entry_channel = int(row.get("channel", -1))
            if entry_channel == channel:
                row["devName"] = cleaned
                changed = True
            updated_channels.append(row)
        if not changed:
            return {"success": False, "message": f"Meross channel {channel} not found"}
        last_error = "Meross cloud did not accept outlet name update"
        for path in ("/v1/Device/setInfo", "/v1/Device/devInfo/set"):
            try:
                await self.cloud_post(
                    path,
                    {"uuid": device_uuid, "channels": updated_channels},
                )
                return {"success": True, "message": "updated outlet name in Meross cloud"}
            except Exception as exc:
                last_error = str(exc)
                logger.debug(
                    "meross switch rename via %s failed for %s ch%s: %s",
                    path,
                    device_uuid[:8],
                    channel,
                    exc,
                )
        return {"success": False, "message": last_error}

    def set_cached_channel_state(self, device_uuid: str, channel: int, on: bool) -> None:
        prior = self._togglex_cache.get(device_uuid)
        states = dict(prior[1]) if prior else {}
        states[channel] = on
        self._togglex_cache[device_uuid] = (time.monotonic(), states)


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
        self._session.set_cached_channel_state(self.device_uuid, self.channel, on)
        return CommandResult(success=True, adapter="meross_cloud")

    async def get_state(self, *, force: bool = False) -> DeviceState:
        if not self._session.configured:
            return DeviceState.UNKNOWN
        on = await self._session.read_channel_state(
            self.device_uuid,
            self.channel,
            force=force,
        )
        if on is None:
            return DeviceState.UNKNOWN
        return DeviceState.ON if on else DeviceState.OFF

    async def is_reachable(self) -> bool:
        if not self._session.configured:
            return False
        try:
            status_map = await self._session.online_status_map()
            if self.device_uuid in status_map and not status_map[self.device_uuid]:
                return False
        except Exception:
            logger.debug("meross online lookup failed for %s", self.name, exc_info=True)
        dev = await self._resolve_device()
        return dev is not None
