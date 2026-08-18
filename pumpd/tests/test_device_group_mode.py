"""Tests for bulk device group mode control."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import MerossConfig, PumpConfig, TuyaConfig
from app.devices.base import CommandResult
from app.service import DeviceCommandError, PumpService
from app.switch_stagger import sort_pumps_by_switch


def test_sort_pumps_by_switch() -> None:
    pumps = [
        PumpConfig(name="b", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_2")),
        PumpConfig(name="a", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_1")),
    ]
    ordered = sort_pumps_by_switch(pumps)
    assert [p.name for p in ordered] == ["a", "b"]


@pytest.mark.asyncio
async def test_set_device_group_mode_all_on(monkeypatch: pytest.MonkeyPatch) -> None:
    service = PumpService.__new__(PumpService)
    service.config = type("Cfg", (), {"pumps": [], "devices": type("D", (), {"switch_stagger_seconds": 0})()})()
    service.config.pumps = [
        PumpConfig(name="p1", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_1")),
        PumpConfig(name="p2", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_2")),
    ]

    calls: list[tuple[str, str]] = []

    async def fake_set_mode(
        name: str,
        mode: str,
        *,
        approve_safety_override: bool = False,
        manual_hours: int = 0,
        manual_minutes: int = 0,
        manual_duration_minutes: int | None = None,
        manual_until_auto: bool = False,
        **kwargs: object,
    ):
        calls.append((name, mode))
        row = type("Row", (), {"name": name, "mode": mode})()
        return row, CommandResult(success=True, adapter="tuya", message="ok")

    monkeypatch.setattr(service, "_safety_active", lambda: False)
    monkeypatch.setattr(service, "set_pump_mode", fake_set_mode)
    monkeypatch.setattr(service, "pumps_for_device", PumpService.pumps_for_device.__get__(service))
    monkeypatch.setattr(service, "refresh_meross_ui_state", AsyncMock())

    result = await PumpService.set_device_group_mode(
        service, "tuya", "dev-a", "manual_on"
    )
    assert result["mode"] == "manual_on"
    assert calls == [("p1", "manual_on"), ("p2", "manual_on")]


@pytest.mark.asyncio
async def test_set_device_group_mode_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    service = PumpService.__new__(PumpService)
    service.config = type("Cfg", (), {"pumps": [], "devices": type("D", (), {"switch_stagger_seconds": 0})()})()
    service.config.pumps = [
        PumpConfig(name="p1", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_1")),
        PumpConfig(name="p2", tuya=TuyaConfig(device_id="dev-a", switch_code="switch_2")),
    ]

    async def fake_set_mode(
        name: str,
        mode: str,
        *,
        approve_safety_override: bool = False,
        manual_hours: int = 0,
        manual_minutes: int = 0,
        manual_duration_minutes: int | None = None,
        manual_until_auto: bool = False,
        **kwargs: object,
    ):
        if name == "p2":
            raise DeviceCommandError(
                CommandResult(success=False, adapter="tuya", message="timeout")
            )
        return type("Row", (), {"name": name, "mode": mode})(), CommandResult(
            success=True, adapter="tuya", message="ok"
        )

    monkeypatch.setattr(service, "_safety_active", lambda: False)
    monkeypatch.setattr(service, "set_pump_mode", fake_set_mode)
    monkeypatch.setattr(service, "pumps_for_device", PumpService.pumps_for_device.__get__(service))

    with pytest.raises(DeviceCommandError) as exc:
        await PumpService.set_device_group_mode(service, "tuya", "dev-a", "manual_off")
    assert "p2" in exc.value.result.message
