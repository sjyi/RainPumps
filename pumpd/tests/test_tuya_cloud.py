"""Tuya cloud device adapter tests."""

from __future__ import annotations

import pytest

from app.devices.base import DeviceState
from app.devices.tuya_cloud import TuyaCloudDevice, _cloud_error, parse_cloud_switch_state


def test_cloud_error_parses_quota_response() -> None:
    result = {
        "success": False,
        "code": 28841004,
        "msg": "IoT Core trial quota is exhausted.",
    }
    text = _cloud_error(result)
    assert "IoT Core trial quota is exhausted." in text
    assert "28841004" in text
    assert "LAN" in text


def test_cloud_error_parses_tinytuya_payload() -> None:
    result = {
        "Error": "Error Response from Tuya Cloud",
        "Err": "913",
        "Payload": '{"code":28841004,"msg":"IoT Core trial quota is exhausted.","success":false}',
    }
    text = _cloud_error(result)
    assert "IoT Core trial quota is exhausted." in text


def test_parse_cloud_switch_state_on() -> None:
    result = {"success": True, "result": [{"code": "switch_1", "value": True}]}
    assert parse_cloud_switch_state(result) == DeviceState.ON


def test_parse_cloud_switch_state_off() -> None:
    result = {"success": True, "result": [{"code": "switch", "value": False}]}
    assert parse_cloud_switch_state(result) == DeviceState.OFF


def test_parse_cloud_switch_state_specific_switch() -> None:
    result = {
        "success": True,
        "result": [
            {"code": "switch_1", "value": True},
            {"code": "switch_2", "value": False},
            {"code": "switch_3", "value": True},
        ],
    }
    assert parse_cloud_switch_state(result, switch_code="switch_2") == DeviceState.OFF
    assert parse_cloud_switch_state(result, switch_code="switch_3") == DeviceState.ON


class FakeCloud:
    def __init__(self) -> None:
        self.on = False
        self.online = True

    def sendcommand(self, device_id: str, body: dict) -> dict:
        code = body["commands"][0]["code"]
        if code == "switch_1":
            self.on = bool(body["commands"][0]["value"])
            return {"success": True}
        return {"success": False, "msg": "bad code"}

    def getstatus(self, device_id: str) -> dict:
        return {"success": True, "result": [{"code": "switch_1", "value": self.on}]}

    def getconnectstatus(self, device_id: str) -> bool:
        return self.online


@pytest.mark.asyncio
async def test_tuya_cloud_turn_on() -> None:
    cloud = FakeCloud()
    dev = TuyaCloudDevice("p1", "dev123", cloud)
    result = await dev.turn_on()
    assert result.success
    assert cloud.on is True


@pytest.mark.asyncio
async def test_tuya_cloud_get_state() -> None:
    cloud = FakeCloud()
    cloud.on = True
    dev = TuyaCloudDevice("p1", "dev123", cloud)
    assert await dev.get_state() == DeviceState.ON


def test_parse_cloud_switch_state_nested_result() -> None:
    result = {
        "success": True,
        "result": {
            "status": [
                {"code": "switch_1", "value": True},
                {"code": "switch_2", "value": False},
            ]
        },
    }
    assert parse_cloud_switch_state(result, switch_code="switch_2") == DeviceState.OFF


@pytest.mark.asyncio
async def test_tuya_cloud_probe_online_cloud_error() -> None:
    class ErrorCloud(FakeCloud):
        def getconnectstatus(self, device_id: str) -> dict:
            return {"Error": "permission denied", "success": False}

    dev = TuyaCloudDevice("p1", "dev123", ErrorCloud())
    status, detail = await dev.probe_online()
    assert status == "cloud_error"
    assert "permission denied" in detail


@pytest.mark.asyncio
async def test_tuya_cloud_probe_online_reachable() -> None:
    class ReachableCloud(FakeCloud):
        def getstatus(self, device_id: str) -> dict:
            return {"success": False, "msg": "status unavailable"}

    dev = TuyaCloudDevice("p1", "dev123", ReachableCloud())
    status, detail = await dev.probe_online()
    assert status == "online"
    assert "reachable" in detail


@pytest.mark.asyncio
async def test_tuya_cloud_switch_2_only() -> None:
    class MultiCloud(FakeCloud):
        def __init__(self) -> None:
            super().__init__()
            self.states = {"switch_1": False, "switch_2": True, "switch_3": False}

        def sendcommand(self, device_id: str, body: dict) -> dict:
            code = body["commands"][0]["code"]
            if code in self.states:
                self.states[code] = bool(body["commands"][0]["value"])
                return {"success": True}
            return {"success": False, "msg": "bad code"}

        def getstatus(self, device_id: str) -> dict:
            return {
                "success": True,
                "result": [{"code": code, "value": val} for code, val in self.states.items()],
            }

    cloud = MultiCloud()
    dev = TuyaCloudDevice("p2", "dev456", cloud, switch_code="switch_2")
    assert await dev.get_state() == DeviceState.ON
    result = await dev.turn_off()
    assert result.success
    assert cloud.states["switch_2"] is False
    assert cloud.states["switch_1"] is False
    assert cloud.states["switch_3"] is False
