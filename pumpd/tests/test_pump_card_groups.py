"""Pump card grouping and live probe state parsing."""

from __future__ import annotations

from app.pump_card_groups import parse_probe_switch_state


def test_parse_probe_switch_state_meross() -> None:
    assert parse_probe_switch_state("meross_cloud:on") is True
    assert parse_probe_switch_state("meross_cloud:off") is False


def test_parse_probe_switch_state_tuya() -> None:
    assert parse_probe_switch_state("tuya_cloud:on") is True
    assert parse_probe_switch_state("tuya_local:off") is False


def test_parse_probe_switch_state_unknown() -> None:
    assert parse_probe_switch_state("meross_cloud:reachable") is None
    assert parse_probe_switch_state("") is None
