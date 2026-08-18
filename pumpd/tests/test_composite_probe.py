"""Composite probe_connectivity tests."""

from __future__ import annotations

import pytest

from app.devices.base import DeviceState
from app.devices.composite import CompositePumpDevice


class FakeCloudDevice:
    def __init__(self, *, status: str, detail: str, cloud_error: object = None) -> None:
        self.status = status
        self.detail = detail
        self._cloud_error = cloud_error

    async def probe_online(self, *, timeout: float = 8.0) -> tuple[str, str]:
        return self.status, self.detail

    def last_cloud_error(self) -> object:
        return self._cloud_error


class FakeLocalDevice:
    async def get_state(self) -> DeviceState:
        return DeviceState.ON


@pytest.mark.asyncio
async def test_probe_connectivity_uses_tuya_cloud_first() -> None:
    dev = CompositePumpDevice(
        "p1",
        FakeLocalDevice(),
        None,
        tuya_cloud=FakeCloudDevice(status="online", detail="tuya_cloud:on"),
        control_mode="cloud",
    )
    result = await dev.probe_connectivity()
    assert result == {"status": "online", "detail": "tuya_cloud:on"}


@pytest.mark.asyncio
async def test_probe_connectivity_cloud_error_skips_local() -> None:
    dev = CompositePumpDevice(
        "p1",
        FakeLocalDevice(),
        None,
        tuya_cloud=FakeCloudDevice(
            status="cloud_error",
            detail="tuya_cloud:permission denied",
            cloud_error={"code": 9999, "msg": "permission denied"},
        ),
        control_mode="cloud",
    )
    result = await dev.probe_connectivity()
    assert result["status"] == "cloud_error"


@pytest.mark.asyncio
async def test_probe_connectivity_quota_error_falls_back_to_local() -> None:
    dev = CompositePumpDevice(
        "p1",
        None,
        None,
        tuya_cloud=FakeCloudDevice(
            status="cloud_error",
            detail="tuya_cloud:IoT Core trial quota is exhausted.",
            cloud_error={"code": 28841004, "msg": "IoT Core trial quota is exhausted."},
        ),
        control_mode="cloud",
    )
    result = await dev.probe_connectivity(local_device=FakeLocalDevice())
    assert result == {"status": "online", "detail": "tuya_local:on"}


@pytest.mark.asyncio
async def test_probe_connectivity_falls_back_to_local() -> None:
    dev = CompositePumpDevice(
        "p1",
        None,
        None,
        tuya_cloud=FakeCloudDevice(status="offline", detail="tuya_cloud:device offline"),
        control_mode="cloud",
    )
    result = await dev.probe_connectivity(local_device=FakeLocalDevice())
    assert result == {"status": "online", "detail": "tuya_local:on"}


class FakeMerossDevice:
    def __init__(self, state: DeviceState) -> None:
        self.state = state
        self.force_reads: list[bool] = []

    async def is_reachable(self) -> bool:
        return True

    async def get_state(self, *, force: bool = False) -> DeviceState:
        self.force_reads.append(force)
        return self.state


@pytest.mark.asyncio
async def test_probe_connectivity_force_refresh_for_meross() -> None:
    meross = FakeMerossDevice(DeviceState.OFF)
    dev = CompositePumpDevice(
        "p1",
        None,
        None,
        meross_cloud=meross,
        control_mode="cloud",
    )
    result = await dev.probe_connectivity(force=True)
    assert result == {"status": "online", "detail": "meross_cloud:off"}
    assert meross.force_reads == [True]
