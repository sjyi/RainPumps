"""System / integration tests — HTTP API and UI routes."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import load_config


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    import shutil

    from app.config import load_config as real_load_config

    db_path = tmp_path / "test.db"
    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    cfg = real_load_config(config_path)
    cfg.database_url = f"sqlite:///{db_path}"

    def _load_config(_path: str = "config.yaml"):
        loaded = real_load_config(config_path)
        loaded.database_url = f"sqlite:///{db_path}"
        return loaded

    monkeypatch.setattr("app.main.load_config", _load_config)
    monkeypatch.setattr("app.config.load_config", _load_config)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))

    from app.main import create_app

    app = create_app(str(config_path))
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
    assert "Check device connections" in response.text


def test_admin_ui_has_auto_import(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Sync all devices" in response.text
    assert "auto-import-devices-btn" in response.text


def test_auto_import_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import PumpConfig

    async def fake_auto_import(**kwargs: object) -> dict:
        pump = PumpConfig(name="meross_pump")
        return {
            "pumps": [pump],
            "stats": {"added": 1, "updated": 0, "skipped": 0, "discovered": 1},
            "discovery": {
                "devices": [{"label": "Test", "meross_device_uuid": "abc"}],
                "sources": {},
                "errors": {},
                "setup": {"ready": True},
            },
        }

    monkeypatch.setattr("app.web.routes.auto_import_devices", fake_auto_import)
    monkeypatch.setattr(
        "app.service.PumpService.import_pumps",
        AsyncMock(return_value=[PumpConfig(name="meross_pump")]),
    )

    response = client.post("/api/devices/auto-import?lan_scan=false")
    assert response.status_code == 200
    data = response.json()
    assert data["stats"]["added"] == 1
    assert data["message"].startswith("Synced 1 pump")


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


def test_api_runtime_config(client: TestClient) -> None:
    response = client.get("/api/config/runtime")
    assert response.status_code == 200
    data = response.json()
    assert data["system_max_runtime_minutes"] == 180
    assert "switches" in data

    response = client.post(
        "/api/config/runtime",
        json={
            "system_max_runtime_minutes": 240,
            "devices": {},
            "pumps": {"north_pump": 150},
        },
    )
    assert response.status_code == 200
    updated = response.json()
    assert updated["system_max_runtime_minutes"] == 240
    switch = next(s for s in updated["switches"] if s["name"] == "north_pump")
    assert switch["max_runtime_minutes"] == 150
    assert switch["effective_minutes"] == 150
    assert switch["source"] == "switch"


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


def test_get_and_set_display_units(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    response = client.get("/api/config/display")
    assert response.status_code == 200
    assert response.json()["units"] in ("metric", "imperial")

    monkeypatch.setattr(
        "app.service.PumpService.update_display_units",
        lambda self, units: units,
    )
    response = client.post("/api/config/display", json={"units": "metric"})
    assert response.status_code == 200
    assert response.json()["units"] == "metric"


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


def test_clear_all_local_devices(client: TestClient, tmp_path: Path) -> None:
    from app.config import PumpConfig, TuyaConfig, clear_local_devices, save_pumps
    from app.db import init_db
    from app.models import HardwareHealthRow, PumpStateRow

    cfg_path = tmp_path / "config.yaml"
    pumps = [
        PumpConfig(name="pump_a", tuya=TuyaConfig(device_id="dev_a")),
        PumpConfig(name="pump_b", tuya=TuyaConfig(device_id="dev_b")),
    ]
    save_pumps(cfg_path, pumps, mode="replace")

    service = client.app.state.service
    service.config_path = str(cfg_path)
    service.config.pumps = pumps
    service.session_factory = init_db(f"sqlite:///{tmp_path / 'clear.db'}")
    service._ensure_pump_rows()
    with service.session_factory() as session:
        session.add(
            HardwareHealthRow(component_id="pump_a", component_type="pump", status="ok")
        )
        session.commit()

    response = client.post("/api/devices/clear-local")
    assert response.status_code == 200
    data = response.json()
    assert data["removed_count"] == 2
    assert set(data["removed"]) == {"pump_a", "pump_b"}
    assert "cloud devices were not changed" in data["message"].lower()
    assert service.config.pumps == []

    with service.session_factory() as session:
        assert session.scalars(select(PumpStateRow)).all() == []
        assert session.scalars(select(HardwareHealthRow)).all() == []

    clear_local_devices(cfg_path)
    loaded = cfg_path.read_text(encoding="utf-8")
    assert "pumps: []" in loaded.replace("\n", " ") or "pumps: []\n" in loaded


def test_get_and_set_display_names(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    response = client.get("/api/config/names")
    assert response.status_code == 200
    assert "groups" in response.json()

    async def fake_update(
        _self: object,
        *,
        device_labels: dict[str, str],
        switch_labels: dict[str, str],
        propagate_cloud: bool = True,
    ) -> dict[str, object]:
        return {
            "saved": True,
            "cloud": [
                {
                    "kind": "switch",
                    "pump_name": "north_pump",
                    "success": True,
                    "message": "updated",
                }
            ],
        }

    monkeypatch.setattr("app.service.PumpService.update_display_names", fake_update)
    response = client.post(
        "/api/config/names",
        json={
            "devices": {"tuya:abc": "Roof Plug"},
            "switches": {"north_pump": "North Outlet"},
            "propagate_cloud": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["saved"] is True
    assert len(data["cloud"]) == 1
