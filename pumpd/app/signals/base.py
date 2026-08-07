"""Rain signal abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.engine import RainState


class RainSignal(ABC):
    @abstractmethod
    async def get_state(self) -> RainState:
        raise NotImplementedError
