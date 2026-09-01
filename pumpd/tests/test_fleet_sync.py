"""Fleet sync for newly discovered / out-of-sync auto pumps."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import AppConfig, PumpConfig
from app.db import init_db
from app.models import PumpStateRow
from app.service import PumpService


@pytest.fixture
def service(tmp_path: Path) -> PumpService:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "\n".join(
            [
                "pumps:",
                "  - name: existing_pump",
                "    meross:",
                "      device_uuid: uuid-existing",
                "      channel: 1",
                "  - name: new_pump",
                "    meross:",
                "      device_uuid: uuid-new",
                "      channel: 1",
            ]
        ),
        encoding="utf-8",
    )
    session_factory = init_db(f"sqlite:///{tmp_path / 'pumpd.db'}")
    cfg = AppConfig()
    cfg.pumps = [
        PumpConfig(name="existing_pump", meross={"device_uuid": "uuid-existing", "channel": 1}),
        PumpConfig(name="new_pump", meross={"device_uuid": "uuid-new", "channel": 1}),
    ]
    svc = PumpService(cfg, session_factory, config_path=str(cfg_path))
    svc._build_devices()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            PumpStateRow(
                name="existing_pump",
                phase="running",
                mode="auto",
                device_on=True,
                duty_on=True,
                runtime_continuous_min=15,
                updated_at=now,
            )
        )
        session.add(
            PumpStateRow(
                name="new_pump",
                phase="idle",
                mode="auto",
                device_on=False,
                duty_on=True,
                runtime_continuous_min=0,
                updated_at=now,
            )
        )
        session.commit()
    return svc


def test_sync_new_pumps_to_fleet_copies_active_phase(service: PumpService) -> None:
    result = service.sync_new_pumps_to_fleet(["new_pump"])

    assert result["synced"] == ["new_pump"]
    assert result["reference_pump"] == "existing_pump"
    assert result["reference_phase"] == "running"

    row = service._get_pump_row("new_pump")
    assert row is not None
    assert row.phase == "running"
    assert row.device_on is False


def test_sync_new_pumps_skips_when_fleet_idle(service: PumpService) -> None:
    with service.session_factory() as session:
        row = session.get(PumpStateRow, "existing_pump")
        assert row is not None
        row.phase = "idle"
        row.device_on = False
        session.commit()

    result = service.sync_new_pumps_to_fleet(["new_pump"])
    assert result["synced"] == []
    assert "idle" in result["reason"].lower()

    row = service._get_pump_row("new_pump")
    assert row is not None
    assert row.phase == "idle"


def test_find_out_of_sync_pumps(service: PumpService) -> None:
    assert service.find_out_of_sync_pumps() == ["new_pump"]


@pytest.mark.asyncio
async def test_import_pumps_syncs_and_evaluates(
    service: PumpService, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "run_evaluation", eval_mock)
    monkeypatch.setattr(service, "reconcile_devices", AsyncMock())
    monkeypatch.setattr(service, "refresh_meross_ui_state", AsyncMock())
    monkeypatch.setattr(service, "_wait_for_new_meross_devices", AsyncMock())
    monkeypatch.setattr(
        "app.service.save_pumps",
        lambda _path, pumps, mode="merge": pumps,
    )

    pumps = [
        PumpConfig(name="existing_pump", meross={"device_uuid": "uuid-existing", "channel": 1}),
        PumpConfig(name="brand_new", meross={"device_uuid": "uuid-brand", "channel": 1}),
    ]
    result = await service.import_pumps(pumps, mode="merge")

    assert "brand_new" in result["new_pumps"]
    assert result["fleet_sync"]["synced"] == ["brand_new"]
    eval_mock.assert_awaited_once()

    row = service._get_pump_row("brand_new")
    assert row is not None
    assert row.phase == "running"


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


def test_sync_fleet_api(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.service.PumpService.sync_pumps_to_fleet",
        AsyncMock(
            return_value={
                "synced": ["p2"],
                "reference_pump": "p1",
                "reference_phase": "running",
            }
        ),
    )
    response = client.post("/api/pumps/sync-fleet", json={})
    assert response.status_code == 200
    assert response.json()["synced"] == ["p2"]
