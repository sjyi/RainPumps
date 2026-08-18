"""Pump device abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class DeviceState(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandResult:
    success: bool
    adapter: str
    message: str = ""
    timed_out: bool = False
    retried: bool = False
    status_before_retry: str = ""
    verify_attempts: int = 0


class PumpDevice(ABC):
    name: str

    @abstractmethod
    async def turn_on(self) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    async def turn_off(self) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    async def get_state(self) -> DeviceState:
        raise NotImplementedError
