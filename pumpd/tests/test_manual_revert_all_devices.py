"""Manual revert verification for every configured pump and device group."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import load_config
from app.db import init_db
from app.manual_revert_verify import (
    _device_groups,
    _prepare_service_mocks,
    verify_all_manual_reverts,
    verify_device_group_manual_revert,
    verify_pump_manual_revert,
)
from app.service import PumpService


@pytest.fixture
def production_config_path() -> Path | None:
    path = Path("config.yaml")
    return path if path.is_file() else None


@pytest.fixture
def live_service(
    tmp_path: Path,
    production_config_path: Path | None,
    monkeypatch: pytest.MonkeyPatch,
) -> PumpService | None:
    if production_config_path is None:
        return None

    cfg = load_config(production_config_path)
    cfg.database_url = f"sqlite:///{tmp_path / 'verify.db'}"
    session_factory = init_db(cfg.database_url)
    svc = PumpService(cfg, session_factory, config_path=str(production_config_path))
    svc._ensure_pump_rows()
    monkeypatch.setattr(svc, "_safety_active", lambda: False)
    _prepare_service_mocks(svc)
    return svc


def _pump_names(service: PumpService) -> list[str]:
    return [p.name for p in service.config.pumps if p.enabled]


@pytest.mark.asyncio
async def test_every_pump_manual_revert(live_service: PumpService | None) -> None:
    if live_service is None:
        pytest.skip("config.yaml not present")
    for name in _pump_names(live_service):
        result = await verify_pump_manual_revert(live_service, name, minutes=5)
        assert result.passed, result.detail


@pytest.mark.asyncio
async def test_every_device_group_manual_revert(live_service: PumpService | None) -> None:
    if live_service is None:
        pytest.skip("config.yaml not present")
    groups = _device_groups(live_service.config)
    if not groups:
        pytest.skip("no multi-switch device groups in config")
    for backend, device_id, names in groups:
        result = await verify_device_group_manual_revert(
            live_service, backend, device_id, names, minutes=5
        )
        assert result.passed, result.detail


@pytest.mark.asyncio
async def test_verify_all_manual_reverts_report(live_service: PumpService | None) -> None:
    if live_service is None:
        pytest.skip("config.yaml not present")
    report = await verify_all_manual_reverts(live_service)
    assert report.passed, report.to_dict()
