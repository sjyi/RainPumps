"""System / integration tests — HTTP API and UI routes."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import load_config


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    db_path = tmp_path / "test.db"
    cfg = load_config("config.example.yaml")
    cfg.database_url = f"sqlite:///{db_path}"

    monkeypatch.setattr("app.main.load_config", lambda _path="config.yaml": cfg)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))

    from app.main import create_app

    app = create_app("config.example.yaml")
    with TestClient(app) as test_client:
        yield test_client


def test_root_redirects_to_user(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/user"


def test_user_ui_returns_html(client: TestClient) -> None:
    response = client.get("/user")
    assert response.status_code == 200
    assert "Rain pump status" in response.text
    assert "Rain forecast" in response.text
    assert "Location" in response.text


def test_admin_ui_has_location_map(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert "location-map" in response.text
    assert "leaflet" in response.text.lower()


def test_admin_ui_returns_html(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Rain Roof Pumps" in response.text
    assert "Hardware health" in response.text


def test_user_partial_returns_html(client: TestClient) -> None:
    response = client.get("/partials/user/status")
    assert response.status_code == 200
    assert "forecast" in response.text.lower()


def test_health_always_unauthenticated(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data
    assert "checks" in data
    assert "scheduler" in data["checks"]


def test_api_status_json(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "rain" in data
    assert "pumps" in data
    assert "forecast_12h" in data
    assert "hardware_health" in data


def test_api_events_json(client: TestClient) -> None:
    response = client.get("/api/events?limit=10")
    assert response.status_code == 200
    assert "events" in response.json()


def test_api_hardware_health(client: TestClient) -> None:
    response = client.get("/api/hardware-health")
    assert response.status_code == 200
    assert "components" in response.json()


def test_set_pump_mode_auto(client: TestClient) -> None:
    response = client.post(
        "/api/pumps/north_pump/mode",
        json={"mode": "auto"},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "auto"


def test_set_pump_mode_invalid_returns_422(client: TestClient) -> None:
    response = client.post(
        "/api/pumps/north_pump/mode",
        json={"mode": "invalid"},
    )
    assert response.status_code == 422


def test_api_status_includes_location_and_forecasts(client: TestClient) -> None:
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "location" in data
    assert "forecast_7d" in data
    assert "forecast_12h_local" in data


def test_get_location_config(client: TestClient) -> None:
    response = client.get("/api/config/location")
    assert response.status_code == 200
    data = response.json()
    assert "latitude" in data
    assert "longitude" in data


def test_set_location_config(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import LocationConfig

    async def fake_update(
        _self: object,
        latitude: float,
        longitude: float,
        *,
        name: str = "",
        address: str = "",
    ) -> LocationConfig:
        return LocationConfig(latitude=latitude, longitude=longitude, name=name, address=address)

    monkeypatch.setattr("app.service.PumpService.update_location", fake_update)
    response = client.post(
        "/api/config/location",
        json={"latitude": 41.0, "longitude": -73.0, "name": "Test", "address": "Test, NY"},
    )
    assert response.status_code == 200
    assert response.json()["latitude"] == 41.0


def test_geocode_search(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.geocoding import GeocodeResult

    async def fake_search(query: str, *, limit: int = 5) -> list[GeocodeResult]:
        return [GeocodeResult(name="Brooklyn, NY, US", latitude=40.65, longitude=-73.95)]

    monkeypatch.setattr("app.web.routes.search_locations", fake_search)
    response = client.get("/api/geocode/search?q=Brooklyn")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


def test_unknown_pump_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/pumps/nonexistent/mode",
        json={"mode": "auto"},
    )
    assert response.status_code == 404


def test_api_list_pumps(client: TestClient) -> None:
    response = client.get("/api/pumps")
    assert response.status_code == 200
    data = response.json()
    assert "pumps" in data
    assert any(p["name"] == "north_pump" for p in data["pumps"])


def test_remove_pump(client: TestClient, tmp_path: Path) -> None:
    from app.config import PumpConfig, SmartThingsPumpConfig, TuyaConfig, save_pumps

    cfg_path = tmp_path / "config.yaml"
    pumps = [
        PumpConfig(name="north_pump"),
        PumpConfig(name="test_device", tuya=TuyaConfig(device_id="abc")),
    ]
    save_pumps(cfg_path, pumps, mode="replace")

    service = client.app.state.service
    service.config_path = str(cfg_path)
    service.config.pumps = pumps

    response = client.delete("/api/pumps/test_device")
    assert response.status_code == 200
    data = response.json()
    assert data["removed"] == "test_device"
    assert [p["name"] for p in data["pumps"]] == ["north_pump"]
    assert [p.name for p in service.config.pumps] == ["north_pump"]


def test_remove_unknown_pump_returns_404(client: TestClient) -> None:
    response = client.delete("/api/pumps/does_not_exist")
    assert response.status_code == 404
