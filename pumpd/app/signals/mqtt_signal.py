"""MQTT rain signal — Phase 2 stub."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.config import MqttConfig
from app.engine import RainState
from app.signals.base import RainSignal

logger = logging.getLogger(__name__)


class MqttRainSignal(RainSignal):
    """Subscribes to MQTT when enabled; otherwise returns inactive stub."""

    def __init__(self, config: MqttConfig) -> None:
        self.config = config
        self._last: RainState | None = None
        self._client: Any = None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        try:
            import paho.mqtt.client as mqtt

            def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
                try:
                    payload = json.loads(msg.payload.decode())
                    self._last = RainState(
                        is_raining=bool(payload.get("raining", payload.get("is_raining", False))),
                        rate_mm_h=float(payload.get("rate_mm_h", 0)),
                        confidence=float(payload.get("confidence", 1.0)),
                        source="mqtt",
                        ts=datetime.now(UTC),
                        water_present=payload.get("water_present"),
                    )
                except Exception as exc:
                    logger.warning("mqtt message parse error: %s", exc)

            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # type: ignore[attr-defined]
            self._client.on_message = on_message
            self._client.connect(self.config.host, self.config.port, 60)
            self._client.subscribe(self.config.topic)
            self._client.loop_start()
        except Exception as exc:
            logger.error("mqtt start failed: %s", exc)

    async def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    async def get_state(self) -> RainState:
        now = datetime.now(UTC)
        if self.config.enabled and self._last:
            return self._last
        return RainState(
            is_raining=False,
            rate_mm_h=0.0,
            confidence=0.0,
            source="mqtt",
            ts=now,
        )
