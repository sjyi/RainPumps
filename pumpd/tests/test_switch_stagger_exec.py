"""Staggered command execution tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig, DevicesConfig, PumpConfig, TuyaConfig
from app.db import init_db
from app.devices.base import CommandResult
from app.engine import PumpCommand
from app.service import PumpService


@pytest.mark.asyncio
async def test_execute_commands_staggers_multi_switch_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = init_db("sqlite:///:memory:")
    config = AppConfig(
        devices=DevicesConfig(switch_stagger_seconds=30),
        pumps=[
            PumpConfig(
                name="p1",
                tuya=TuyaConfig(device_id="dev1", switch_code="switch_1"),
            ),
            PumpConfig(
                name="p2",
                tuya=TuyaConfig(device_id="dev1", switch_code="switch_2"),
            ),
            PumpConfig(
                name="p3",
                tuya=TuyaConfig(device_id="dev1", switch_code="switch_3"),
            ),
        ],
    )
    service = PumpService(config, session_factory, config_path="config.example.yaml")
    service._build_devices()

    sleep_mock = AsyncMock()
    monkeypatch.setattr("app.service.asyncio.sleep", sleep_mock)

    execute_mock = AsyncMock()
    monkeypatch.setattr(service, "_execute_command", execute_mock)
    monkeypatch.setattr(service, "_log_pump_decision", lambda *args, **kwargs: None)

    commands = [
        PumpCommand("p3", "turn_on", "rain"),
        PumpCommand("p1", "turn_on", "rain"),
        PumpCommand("p2", "turn_on", "rain"),
    ]
    await service._execute_commands(commands)

    assert execute_mock.await_count == 3
    names = [call.args[0].pump_name for call in execute_mock.await_args_list]
    assert names == ["p1", "p2", "p3"]
    assert sleep_mock.await_count == 2
    sleep_mock.assert_any_await(30)
    sleep_mock.assert_any_await(30)
