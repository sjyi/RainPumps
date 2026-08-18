"""Tests for display unit formatting."""

from app.web.units import format_precip_rate_mm_h, format_precipitation_mm, format_temperature


def test_format_temperature_metric() -> None:
    assert format_temperature(20.0, "metric") == "20°C"


def test_format_temperature_imperial() -> None:
    assert format_temperature(0.0, "imperial") == "32°F"
    assert format_temperature(20.0, "imperial") == "68°F"


def test_format_precipitation_metric() -> None:
    assert format_precipitation_mm(2.5, "metric") == "2.5 mm"


def test_format_precipitation_imperial() -> None:
    assert format_precipitation_mm(25.4, "imperial") == "1.00 in"


def test_format_precip_rate_imperial() -> None:
    assert format_precip_rate_mm_h(25.4, "imperial") == "1.00 in/h"
