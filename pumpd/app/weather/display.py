"""Display weather models and WMO weather code labels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class CurrentConditions:
    temp_c: float
    humidity_pct: float
    weather_code: int
    precipitation_mm: float
    rain_mm: float
    is_day: bool
    fetched_at: datetime
    weather_text: str = ""
    has_precipitation: bool = False
    provider: str = ""

    @property
    def description(self) -> str:
        if self.weather_text:
            return self.weather_text
        return weather_code_label(self.weather_code)


@dataclass(frozen=True)
class DailyForecast:
    day: date
    weather_code: int
    temp_max_c: float
    temp_min_c: float
    precip_sum_mm: float
    pop_max_pct: float

    @property
    def description(self) -> str:
        return weather_code_label(self.weather_code)


def weather_code_label(code: int) -> str:
    """WMO weather interpretation codes (Open-Meteo)."""
    labels = {
        0: "Clear",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Dense drizzle",
        56: "Freezing drizzle",
        57: "Dense freezing drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Freezing rain",
        67: "Heavy freezing rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Light showers",
        81: "Showers",
        82: "Heavy showers",
        85: "Light snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with hail",
        99: "Thunderstorm with heavy hail",
    }
    return labels.get(code, "Unknown")


def weather_code_icon(code: int, is_day: bool = True) -> str:
    """Simple emoji icon for weather codes."""
    if code in (0, 1):
        return "☀️" if is_day else "🌙"
    if code == 2:
        return "⛅" if is_day else "☁️"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫️"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "🌧️"
    if code in (71, 73, 75, 77, 85, 86):
        return "❄️"
    if code in (95, 96, 99):
        return "⛈️"
    return "🌡️"
