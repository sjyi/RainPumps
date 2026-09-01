"""Tests for user/admin manual ON/OFF controls and HTMX request wiring."""

from __future__ import annotations

from collections.abc import Generator
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import MerossConfig, PumpConfig, save_pumps
from app.devices.base import CommandResult


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


def _meross_group_pumps() -> list[PumpConfig]:
    device_uuid = "24052182553790570902c4e7ae044143"
    return [
        PumpConfig(
            name="outlet_one",
            label="Roof East Switch 1",
            meross=MerossConfig(
                device_uuid=device_uuid,
                channel=1,
                switch_code="switch_1",
            ),
        ),
        PumpConfig(
            name="outlet_two",
            label="Roof East Switch 2",
            meross=MerossConfig(
                device_uuid=device_uuid,
                channel=2,
                switch_code="switch_2",
            ),
        ),
    ]


@pytest.fixture
def group_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    import shutil

    from app.config import load_config as real_load_config

    db_path = tmp_path / "test.db"
    config_path = tmp_path / "config.yaml"
    shutil.copy("config.example.yaml", config_path)
    save_pumps(config_path, _meross_group_pumps(), mode="replace")

    def _load_config(_path: str = "config.yaml"):
        loaded = real_load_config(config_path)
        loaded.database_url = f"sqlite:///{db_path}"
        return loaded

    monkeypatch.setattr("app.main.load_config", _load_config)
    monkeypatch.setattr("app.config.load_config", _load_config)
    monkeypatch.setattr("app.service.PumpService.poll_forecasts", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.reconcile_devices", AsyncMock())
    monkeypatch.setattr("app.service.PumpService.run_evaluation", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.service.PumpService.probe_pumps_online",
        AsyncMock(
            return_value={
                "outlet_one": {"status": "online", "detail": "meross_cloud:on"},
                "outlet_two": {"status": "online", "detail": "meross_cloud:off"},
            }
        ),
    )

    from app.main import create_app

    app = create_app(str(config_path))
    with TestClient(app) as test_client:
        yield test_client


class _ButtonCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[dict[str, str | None]] = []
        self._current: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "button":
            return
        self._current = {key: value for key, value in attrs}
        self.buttons.append(self._current)

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self._current = None


def _collect_buttons(html: str) -> list[dict[str, str | None]]:
    parser = _ButtonCollector()
    parser.feed(html)
    return parser.buttons


def _manual_mode_buttons(html: str) -> list[dict[str, str | None]]:
    return [
        btn
        for btn in _collect_buttons(html)
        if btn.get("data-manual-mode") in ("manual_on", "manual_off")
    ]


def _assert_manual_button_wiring(btn: dict[str, str | None]) -> None:
    mode = btn.get("data-manual-mode")
    assert mode in ("manual_on", "manual_off")
    assert btn.get("type") == "button"
    assert "mode-btn" in (btn.get("class") or "")
    assert btn.get("hx-post")
    assert btn.get("hx-swap") == "none"
    assert "hx-vals" not in btn, (
        "manual buttons must not use hx-vals js: (HTMX 2 breaks on wrapped this); "
        "payload is injected via htmx:configRequest in base.html"
    )
    if btn.get("class", "").find("manual-apply-btn") >= 0:
        return
    post = btn["hx-post"] or ""
    if "/api/devices/mode" in post:
        assert btn.get("data-mode-action", "").lower().startswith("all manual")
    else:
        assert post.startswith("/api/pumps/")
        assert post.endswith("/mode")


@pytest.mark.parametrize("path", ["/user", "/partials/user/status"])
def test_user_ui_manual_on_off_buttons(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    html = response.text

    manual_buttons = _manual_mode_buttons(html)
    assert manual_buttons, f"expected Manual ON/OFF buttons on {path}"

    modes = {btn["data-manual-mode"] for btn in manual_buttons}
    assert "manual_on" in modes
    assert "manual_off" in modes

    for btn in manual_buttons:
        _assert_manual_button_wiring(btn)

    assert 'hx-post="/api/pumps/north_pump/mode"' in html
    assert "Manual ON" in html
    assert "Manual OFF" in html


def test_user_ui_has_manual_control_panel_markup(client: TestClient) -> None:
    response = client.get("/user")
    assert response.status_code == 200
    html = response.text

    assert "manual-control-scope" in html
    assert "manual-control-panel" in html
    assert "manual-apply-btn" in html
    assert "manual-hours" in html
    assert "manual-minutes" in html

    apply_buttons = [
        btn
        for btn in _collect_buttons(html)
        if "manual-apply-btn" in (btn.get("class") or "")
    ]
    assert apply_buttons
    for btn in apply_buttons:
        assert btn.get("hx-post")
        assert btn.get("hx-swap") == "none"
        assert "hx-vals" not in btn


def test_user_ui_does_not_use_broken_htmx_js_vals(client: TestClient) -> None:
    response = client.get("/user")
    assert response.status_code == 200
    html = response.text

    assert "js:pumpManualPayload" not in html
    assert "js:deviceGroupManualPayload" not in html
    assert "js:pumpManualApplyPayload" not in html
    assert "js:deviceGroupManualApplyPayload" not in html


def test_base_template_wires_htmx_config_request_for_manual_payload(client: TestClient) -> None:
    response = client.get("/user")
    assert response.status_code == 200
    html = response.text

    assert "htmx:configRequest" in html
    assert "mergeManualRequestParams" in html
    assert "window.pumpManualPayload" in html
    assert "window.deviceGroupManualPayload" in html
    assert "window.pumpManualApplyPayload" in html


def test_admin_ui_manual_on_off_buttons(client: TestClient) -> None:
    response = client.get("/admin")
    assert response.status_code == 200
    html = response.text

    manual_buttons = _manual_mode_buttons(html)
    assert manual_buttons

    for btn in manual_buttons:
        _assert_manual_button_wiring(btn)


def test_device_group_manual_buttons(group_client: TestClient) -> None:
    response = group_client.get("/partials/user/status")
    assert response.status_code == 200
    html = response.text

    assert "pump-device-group" in html
    assert "pump-device-quick-actions" in html
    assert 'hx-post="/api/devices/mode"' in html

    group_buttons = [
        btn
        for btn in _manual_mode_buttons(html)
        if (btn.get("hx-post") or "").endswith("/api/devices/mode")
    ]
    assert len(group_buttons) >= 2
    group_modes = {btn["data-manual-mode"] for btn in group_buttons}
    assert group_modes == {"manual_on", "manual_off"}


def test_device_group_collapsed_header_shows_circuit_status(group_client: TestClient) -> None:
    response = group_client.get("/partials/user/status")
    assert response.status_code == 200
    html = response.text

    assert "pump-device-circuit-status" in html
    assert "Roof East Switch 1" in html
    assert "Roof East Switch 2" in html
    assert " outlets</span>" not in html
    assert html.count('class="badge on pump-power-badge"') >= 1
    assert html.count('class="badge off pump-power-badge"') >= 1


def test_admin_device_group_header_shows_circuit_status(group_client: TestClient) -> None:
    response = group_client.get("/partials/admin/status")
    assert response.status_code == 200
    html = response.text

    assert "pump-device-group-admin" in html
    assert "pump-device-circuit-status" in html
    assert "Roof East Switch 1" in html
    assert "Roof East Switch 2" in html
    assert " outlets</span>" not in html
    assert html.count('class="badge on pump-power-badge"') >= 1
    assert html.count('class="badge off pump-power-badge"') >= 1


def test_admin_device_group_has_edit_pencil_on_header_and_switches(group_client: TestClient) -> None:
    response = group_client.get("/partials/admin/status")
    assert response.status_code == 200
    html = response.text

    assert html.count("name-edit-toggle") >= 3
    assert "name-cancel-edit" in html
    assert "save-device-names-btn" in html
    assert "propagate-cloud-toggle" in html


@pytest.mark.parametrize(
    ("mode", "expected_mode"),
    [
        ("manual_on", "manual_on"),
        ("manual_off", "manual_off"),
    ],
)
def test_set_pump_mode_accepts_htmx_form_payload(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_mode: str,
) -> None:
    service = client.app.state.service
    monkeypatch.setattr(service, "_safety_active", lambda: False)
    monkeypatch.setattr(
        service,
        "_manual_device_command",
        AsyncMock(
            return_value=CommandResult(
                success=True,
                adapter="test",
                message="ok",
                timed_out=False,
                retried=False,
            )
        ),
    )

    response = client.post(
        "/api/pumps/north_pump/mode",
        data={
            "mode": mode,
            "manual_hours": "0",
            "manual_minutes": "5",
            "manual_until_auto": "false",
        },
    )
    assert response.status_code == 200
    assert response.json()["mode"] == expected_mode


def test_set_device_group_mode_accepts_htmx_form_payload(
    group_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = group_client.app.state.service
    monkeypatch.setattr(service, "_safety_active", lambda: False)
    monkeypatch.setattr(
        service,
        "_manual_device_command",
        AsyncMock(
            return_value=CommandResult(
                success=True,
                adapter="test",
                message="ok",
                timed_out=False,
                retried=False,
            )
        ),
    )

    response = group_client.post(
        "/api/devices/mode",
        data={
            "mode": "manual_on",
            "manual_hours": "0",
            "manual_minutes": "5",
            "manual_until_auto": "false",
            "device_backend": "meross",
            "device_id": "24052182553790570902c4e7ae044143",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "manual_on"
    assert len(data["pumps"]) == 2


def test_auto_button_still_uses_static_hx_vals(client: TestClient) -> None:
    response = client.get("/user")
    assert response.status_code == 200
    html = response.text

    assert 'hx-vals=\'{"mode": "auto"}\'' in html
    assert 'hx-post="/api/pumps/north_pump/mode"' in html


def test_manual_buttons_trigger_status_panel_refresh(client: TestClient) -> None:
    response = client.get("/user")
    assert response.status_code == 200
    html = response.text

    refresh_value = "if(event.detail.successful) htmx.trigger('#status-panel', 'load')"
    manual_buttons = _manual_mode_buttons(html)
    assert manual_buttons
    for btn in manual_buttons:
        assert btn.get("hx-on::after-request") == refresh_value
