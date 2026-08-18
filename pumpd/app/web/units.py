"""Format weather values for metric or imperial display."""

from __future__ import annotations

from typing import Literal

Units = Literal["metric", "imperial"]


def format_temperature(celsius: float, units: Units = "metric") -> str:
    if units == "imperial":
        fahrenheit = celsius * 9.0 / 5.0 + 32.0
        return f"{round(fahrenheit)}°F"
    return f"{round(celsius)}°C"


def format_precipitation_mm(mm: float, units: Units = "metric") -> str:
    if units == "imperial":
        inches = mm / 25.4
        return f"{inches:.2f} in"
    if mm < 10:
        return f"{mm:.1f} mm"
    return f"{mm:.1f} mm"


def format_precip_rate_mm_h(mm_h: float, units: Units = "metric") -> str:
    if units == "imperial":
        inches_h = mm_h / 25.4
        return f"{inches_h:.2f} in/h"
    return f"{mm_h:.1f} mm/h"
