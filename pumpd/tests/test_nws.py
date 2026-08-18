"""NWS weather provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.weather.nws import NwsProvider


@pytest.mark.asyncio
async def test_nws_rounds_coordinates_before_request() -> None:
    provider = NwsProvider()
    mock_client = AsyncMock()
    points_resp = MagicMock()
    points_resp.json.return_value = {"properties": {"forecastHourly": "https://example/hourly"}}
    points_resp.raise_for_status = MagicMock()
    hourly_resp = MagicMock()
    hourly_resp.json.return_value = {"properties": {"periods": []}}
    hourly_resp.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(side_effect=[points_resp, hourly_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.weather.nws.httpx.AsyncClient", return_value=mock_client):
        await provider.fetch(40.853374237141914, -74.00032281875612)

    first_url = mock_client.get.await_args_list[0].args[0]
    assert "/points/40.8534,-74.0003" in first_url
