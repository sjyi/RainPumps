"""Tests for geocoding and weather display helpers."""

from __future__ import annotations

from app.weather.display import weather_code_icon, weather_code_label


def test_weather_code_label_clear() -> None:
    assert weather_code_label(0) == "Clear"
    assert weather_code_label(63) == "Rain"


def test_weather_code_icon_rain() -> None:
    assert weather_code_icon(63) == "🌧️"
    assert weather_code_icon(0, is_day=True) == "☀️"
    assert weather_code_icon(0, is_day=False) == "🌙"
