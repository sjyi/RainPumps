"""Geocoding via Open-Meteo (search) and Nominatim (reverse)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "pumpd/1.1.0 (rain pump controller)"


@dataclass(frozen=True)
class GeocodeResult:
    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""


def _display_name(name: str, admin1: str, country: str) -> str:
    parts = [name]
    if admin1:
        parts.append(admin1)
    if country:
        parts.append(country)
    return ", ".join(parts)


async def search_locations(query: str, *, limit: int = 5) -> list[GeocodeResult]:
    if not query.strip():
        return []
    params: dict[str, str | int] = {"name": query.strip(), "count": limit, "language": "en", "format": "json"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(OPEN_METEO_GEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    results: list[GeocodeResult] = []
    for row in data.get("results") or []:
        name = row.get("name", "")
        admin1 = row.get("admin1", "") or ""
        country = row.get("country", "") or ""
        results.append(
            GeocodeResult(
                name=_display_name(name, admin1, country),
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                country=country,
                admin1=admin1,
            )
        )
    return results


async def reverse_geocode(latitude: float, longitude: float) -> str:
    params: dict[str, str | int | float] = {
        "lat": latitude,
        "lon": longitude,
        "format": "json",
        "zoom": 14,
        "addressdetails": 1,
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            resp = await client.get(NOMINATIM_REVERSE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("reverse geocode failed")
        return f"{latitude:.4f}, {longitude:.4f}"

    address = data.get("address") or {}
    for key in ("city", "town", "village", "hamlet", "suburb", "county"):
        if address.get(key):
            place = address[key]
            state = address.get("state", "")
            country = address.get("country", "")
            parts = [place]
            if state:
                parts.append(state)
            if country:
                parts.append(country)
            return ", ".join(parts)
    return str(data.get("display_name", f"{latitude:.4f}, {longitude:.4f}"))
