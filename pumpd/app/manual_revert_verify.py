"""Verify manual mode auto-revert for every pump and multi-switch device group."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from app.config import AppConfig, load_config
from app.devices.base import CommandResult
from app.engine import _as_utc
from app.models import PumpStateRow
from app.pump_card_groups import group_pump_cards
from app.runtime_config import pump_cards_from_config
from app.service import PumpService


@dataclass
class RevertCheckResult:
    target: str
    kind: str
    passed: bool
    detail: str


@dataclass
class RevertVerifyReport:
    results: list[RevertCheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total": len(self.results),
            "failed": [r.__dict__ for r in self.results if not r.passed],
            "results": [r.__dict__ for r in self.results],
        }


def _device_groups(config: AppConfig) -> list[tuple[str, str, list[str]]]:
    cards = pump_cards_from_config(config)
    groups = group_pump_cards(cards, config=config)
    out: list[tuple[str, str, list[str]]] = []
    for group in groups:
        if group.get("kind") != "group":
            continue
        backend = group.get("device_backend") or ""
        device_id = group.get("device_id") or ""
        names = [p["name"] for p in group.get("pumps", [])]
        if len(names) >= 2 and backend and device_id:
            out.append((backend, device_id, names))
    return out


def _prepare_service_mocks(service: PumpService) -> None:
    ok = CommandResult(success=True, adapter="verify", message="mocked")
    service._manual_device_command = AsyncMock(return_value=ok)  # type: ignore[method-assign]
    service._execute_command = AsyncMock(return_value=None)  # type: ignore[method-assign]


async def _expire_manual_revert(service: PumpService, pump_name: str) -> None:
    expired = datetime.now(UTC) - timedelta(seconds=1)
    with service.session_factory() as session:
        row = session.get(PumpStateRow, pump_name)
        if row is None:
            raise ValueError(f"pump not found: {pump_name}")
        row.manual_revert_at = expired
        session.commit()
    service._cancel_manual_revert_task(pump_name)
    await service.run_evaluation()


async def _assert_manual_scheduled(
    service: PumpService,
    pump_name: str,
    *,
    minutes: int,
) -> str | None:
    row = service._get_pump_row(pump_name)
    if row is None:
        return f"{pump_name}: missing pump row"
    if row.mode not in ("manual_on", "manual_off"):
        return f"{pump_name}: expected manual mode, got {row.mode}"
    if row.manual_revert_at is None:
        return f"{pump_name}: manual_revert_at not set"
    delta = (_as_utc(row.manual_revert_at) - datetime.now(UTC)).total_seconds()
    if not (minutes * 60 - 30 <= delta <= minutes * 60 + 30):
        return f"{pump_name}: revert in {delta:.0f}s, expected ~{minutes * 60}s"
    if pump_name not in service._manual_revert_tasks:
        return f"{pump_name}: revert timer not scheduled"
    return None


async def _assert_reverted_to_auto(service: PumpService, pump_name: str) -> str | None:
    row = service._get_pump_row(pump_name)
    if row is None:
        return f"{pump_name}: missing pump row after revert"
    if row.mode != "auto":
        return f"{pump_name}: expected auto after revert, got {row.mode}"
    if row.manual_revert_at is not None:
        return f"{pump_name}: manual_revert_at should be cleared"
    if pump_name in service._manual_revert_tasks:
        return f"{pump_name}: revert timer still active"
    return None


async def verify_pump_manual_revert(
    service: PumpService,
    pump_name: str,
    *,
    minutes: int = 5,
) -> RevertCheckResult:
    _prepare_service_mocks(service)
    target = pump_name
    original = service._get_pump_row(pump_name)
    if original is None:
        return RevertCheckResult(target, "pump", False, "pump row missing")

    try:
        await service.set_pump_mode(pump_name, "manual_on")
        err = await _assert_manual_scheduled(service, pump_name, minutes=minutes)
        if err:
            return RevertCheckResult(target, "pump", False, err)

        await _expire_manual_revert(service, pump_name)
        err = await _assert_reverted_to_auto(service, pump_name)
        if err:
            return RevertCheckResult(target, "pump", False, err)

        return RevertCheckResult(target, "pump", True, "manual_on → auto after expiry")
    except Exception as exc:
        return RevertCheckResult(target, "pump", False, str(exc))
    finally:
        if original.mode == "auto":
            await service.set_pump_mode(pump_name, "auto")
        elif original.mode in ("manual_on", "manual_off"):
            await service.set_pump_mode(pump_name, original.mode)


async def verify_device_group_manual_revert(
    service: PumpService,
    device_backend: str,
    device_id: str,
    pump_names: list[str],
    *,
    minutes: int = 5,
) -> RevertCheckResult:
    _prepare_service_mocks(service)
    target = f"{device_backend}:{device_id}"
    originals = {name: service._get_pump_row(name) for name in pump_names}
    if any(row is None for row in originals.values()):
        return RevertCheckResult(target, "device_group", False, "missing pump row")

    try:
        await service.set_device_group_mode(device_backend, device_id, "manual_off")
        for name in pump_names:
            err = await _assert_manual_scheduled(service, name, minutes=minutes)
            if err:
                return RevertCheckResult(target, "device_group", False, err)

        for name in pump_names:
            await _expire_manual_revert(service, name)
            err = await _assert_reverted_to_auto(service, name)
            if err:
                return RevertCheckResult(target, "device_group", False, err)

        return RevertCheckResult(
            target,
            "device_group",
            True,
            f"manual_off on {len(pump_names)} switches → auto after expiry",
        )
    except Exception as exc:
        return RevertCheckResult(target, "device_group", False, str(exc))
    finally:
        for name, row in originals.items():
            if row is None:
                continue
            if row.mode == "auto":
                await service.set_pump_mode(name, "auto")
            elif row.mode in ("manual_on", "manual_off"):
                await service.set_pump_mode(name, row.mode)


async def verify_all_manual_reverts(
    service: PumpService,
    *,
    minutes: int | None = None,
) -> RevertVerifyReport:
    _prepare_service_mocks(service)
    revert_minutes = minutes if minutes is not None else service.config.rules.manual_revert_minutes
    if service._safety_active():
        report = RevertVerifyReport()
        report.results.append(
            RevertCheckResult(
                "safety",
                "preflight",
                False,
                "safety hard-stop active; cannot verify manual revert",
            )
        )
        return report

    report = RevertVerifyReport()
    pump_names = [p.name for p in service.config.pumps if p.enabled]
    for name in pump_names:
        report.results.append(
            await verify_pump_manual_revert(service, name, minutes=revert_minutes)
        )

    for backend, device_id, names in _device_groups(service.config):
        report.results.append(
            await verify_device_group_manual_revert(
                service, backend, device_id, names, minutes=revert_minutes
            )
        )
    return report


def _print_report(report: RevertVerifyReport) -> None:
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.kind:13} {result.target}: {result.detail}")
    print(
        f"\nSummary: {sum(1 for r in report.results if r.passed)}/{len(report.results)} passed"
    )


async def _main(config_path: str = "config.yaml") -> int:
    from app.db import init_db

    config = load_config(config_path)
    session_factory = init_db(config.database_url)
    service = PumpService(config, session_factory, config_path=config_path)
    service._ensure_pump_rows()
    report = await verify_all_manual_reverts(service)
    _print_report(report)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
