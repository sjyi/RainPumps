"""FastAPI routes — dashboard, REST API, htmx partials."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import AppConfig, EnvSettings
from app.device_import import (
    annotate_discovered_system_status,
    auto_import_devices,
    discover_all,
    discovered_dict_to_pump_config,
    get_import_setup_status,
    import_status_counts,
    resolve_credential_paths,
    slugify_pump_name,
)
from app.engine import _as_utc
from app.geocoding import reverse_geocode, search_locations
from app.hardware_health import CommandLockError
from app.service import DeviceCommandError, PumpService
from app.weather.display import weather_code_icon, weather_code_label

templates = Jinja2Templates(directory="app/web/templates")
templates.env.filters["weather_icon"] = weather_code_icon
templates.env.filters["weather_label"] = weather_code_label


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


class ModeRequest(BaseModel):
    mode: str
    approve_safety_override: bool = False


class LocationRequest(BaseModel):
    latitude: float
    longitude: float
    name: str = ""
    address: str = ""


class ImportPumpItem(BaseModel):
    name: str
    enabled: bool = True
    tuya_device_id: str = ""
    tuya_ip: str = ""
    tuya_local_key: str = ""
    tuya_version: float = 3.4
    tuya_switch_code: str = ""
    meross_device_uuid: str = ""
    meross_channel: int = 0
    meross_switch_code: str = ""
    smartthings_device_id: str = ""


class ImportPumpsRequest(BaseModel):
    pumps: list[ImportPumpItem]
    mode: str = "merge"


def _health_summary(
    config: AppConfig, service: PumpService, scheduler: Any
) -> dict[str, Any]:
    now = datetime.now(UTC)
    status = service.get_status()
    checks: dict[str, str | int | float] = {"db": "ok"}

    checks["scheduler"] = "ok" if scheduler.running else "fail"

    last_forecast = status.get("last_forecast_at")
    if last_forecast:
        checks["last_forecast_age_minutes"] = round(
            (now - _as_utc(last_forecast)).total_seconds() / 60, 1
        )
    else:
        checks["last_forecast_age_minutes"] = -1

    last_eval_str = status.get("last_eval_at")
    eval_age = 9999.0
    if last_eval_str:
        last_eval = _as_utc(datetime.fromisoformat(last_eval_str))
        eval_age = (now - last_eval).total_seconds() / 60
        checks["last_eval_age_minutes"] = round(eval_age, 1)
    else:
        checks["last_eval_age_minutes"] = -1

    eval_limit = 2 * config.rules.evaluate_minutes
    forecast_limit = 2 * config.weather.poll_minutes
    forecast_age = checks["last_forecast_age_minutes"]
    hw_faults = [
        c for c in status.get("hardware_health", []) if c.get("status") == "fault"
    ]
    ok = (
        checks["scheduler"] == "ok"
        and isinstance(forecast_age, (int, float))
        and forecast_age >= 0
        and forecast_age <= forecast_limit
        and eval_age <= eval_limit
        and not hw_faults
    )
    checks["engine_watchdog"] = "ok" if eval_age <= eval_limit else "fail"
    checks["hardware_faults"] = len(hw_faults)

    return {"status": "ok" if ok else "degraded", "checks": checks}


def create_router(config: AppConfig, service: PumpService, scheduler: Any) -> APIRouter:
    router = APIRouter()

    def verify_auth(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if config.api.auth_enabled and x_api_key != config.api.api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    @router.get("/", response_class=RedirectResponse)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/user", status_code=302)

    @router.get("/user", response_class=HTMLResponse)
    async def user_dashboard(request: Request) -> HTMLResponse:
        status = service.get_status()
        rain = await service.get_rain_state()
        pump_cards = await service.get_pump_cards()
        return templates.TemplateResponse(
            request,
            "user.html",
            {
                "config": config,
                "status": status,
                "rain": rain,
                "pump_cards": pump_cards,
                "active_nav": "user",
            },
        )

    @router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(verify_auth)])
    async def admin_dashboard(request: Request) -> HTMLResponse:
        status = service.get_status()
        rain = await service.get_rain_state()
        health = _health_summary(config, service, scheduler)
        events = service.get_events(limit=50)
        pump_cards = await service.get_pump_cards()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "config": config,
                "status": status,
                "rain": rain,
                "health": health,
                "events": events,
                "pump_cards": pump_cards,
                "control_mode": config.devices.control_mode,
                "active_nav": "admin",
            },
        )

    @router.get("/partials/user/status", response_class=HTMLResponse)
    async def partial_user_status(request: Request) -> HTMLResponse:
        status = service.get_status()
        rain = await service.get_rain_state()
        pump_cards = await service.get_pump_cards()
        return templates.TemplateResponse(
            request,
            "partials/user_status.html",
            {"status": status, "rain": rain, "config": config, "pump_cards": pump_cards},
        )

    @router.get(
        "/partials/admin/status",
        response_class=HTMLResponse,
        dependencies=[Depends(verify_auth)],
    )
    async def partial_admin_status(request: Request) -> HTMLResponse:
        status = service.get_status()
        rain = await service.get_rain_state()
        health = _health_summary(config, service, scheduler)
        pump_cards = await service.get_pump_cards()
        return templates.TemplateResponse(
            request,
            "partials/admin_status.html",
            {
                "status": status,
                "rain": rain,
                "health": health,
                "pump_cards": pump_cards,
                "control_mode": config.devices.control_mode,
            },
        )

    @router.get("/partials/status", response_class=RedirectResponse)
    async def partial_status_legacy() -> RedirectResponse:
        return RedirectResponse(url="/partials/user/status", status_code=302)

    @router.get("/api/status")
    async def api_status() -> dict[str, Any]:
        status = service.get_status()
        rain = await service.get_rain_state()
        return {
            "rain": {
                "is_raining": rain.is_raining,
                "rate_mm_h": rain.rate_mm_h,
                "confidence": rain.confidence,
                "source": rain.source,
                "ts": rain.ts.isoformat(),
                "water_present": rain.water_present,
            },
            "pumps": [
                {
                    "name": p["name"],
                    "phase": p["phase"],
                    "mode": p["mode"],
                    "device_on": p["device_on"],
                    "runtime_today_min": p["runtime_today_min"],
                    "runtime_continuous_min": p["runtime_continuous_min"],
                    "safety_override_approved": p["safety_override_approved"],
                    "online_status": p["online_status"],
                    "online_detail": p["online_detail"],
                }
                for p in await service.get_pump_cards()
            ],
            "forecast_12h": [
                {"hour": f.hour_ts.isoformat(), "pop_pct": f.pop_pct, "rain_mm": f.rain_mm}
                for f in status["forecast_12h"]
            ],
            "forecast_12h_local": status["forecast_12h_local"],
            "forecast_7d": status["forecast_7d_local"],
            "current": (
                {
                    "temp_c": status["current"].temp_c,
                    "humidity_pct": status["current"].humidity_pct,
                    "weather_code": status["current"].weather_code,
                    "description": status["current"].description,
                    "precipitation_mm": status["current"].precipitation_mm,
                    "rain_mm": status["current"].rain_mm,
                    "is_day": status["current"].is_day,
                    "fetched_at": status["current"].fetched_at.isoformat(),
                }
                if status.get("current")
                else None
            ),
            "location": status["location"],
            "provider_health": [
                {
                    "provider": h.provider,
                    "last_ok_at": h.last_ok_at.isoformat() if h.last_ok_at else None,
                    "last_error": h.last_error,
                }
                for h in status["provider_health"]
            ],
            "hardware_health": status["hardware_health"],
        }

    @router.post("/api/pumps/{name}/mode")
    async def set_mode(
        name: str,
        request: Request,
        _: None = Depends(verify_auth),
    ) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
            selected = payload.get("mode")
            approved = bool(payload.get("approve_safety_override", False))
        else:
            form = await request.form()
            selected = form.get("mode")
            raw = form.get("approve_safety_override", "false")
            approved = str(raw).lower() in ("true", "1", "yes")
        if selected not in ("auto", "manual_on", "manual_off"):
            raise HTTPException(status_code=422, detail="invalid mode")
        try:
            row, cmd_result = await service.set_pump_mode(
                name, selected, approve_safety_override=approved
            )
        except CommandLockError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DeviceCommandError as exc:
            result = exc.result
            raise HTTPException(
                status_code=504,
                detail={
                    "message": result.message,
                    "timed_out": result.timed_out,
                    "retried": result.retried,
                    "status_before_retry": result.status_before_retry,
                },
            ) from exc
        if not row:
            raise HTTPException(status_code=404, detail="pump not found")
        payload: dict[str, Any] = {
            "name": row.name,
            "mode": row.mode,
            "safety_override_approved": row.safety_override_approved,
        }
        if cmd_result is not None:
            payload["command"] = {
                "success": cmd_result.success,
                "adapter": cmd_result.adapter,
                "message": cmd_result.message,
                "timed_out": cmd_result.timed_out,
                "retried": cmd_result.retried,
                "status_before_retry": cmd_result.status_before_retry,
            }
        return payload

    @router.get("/api/events")
    async def api_events(
        limit: int = Query(default=50, le=500),
        pump: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        events = service.get_events(limit=limit, pump=pump, since=since)
        return {
            "events": [
                {
                    "id": e.id,
                    "ts": e.ts.isoformat(),
                    "pump_name": e.pump_name,
                    "event_type": e.event_type,
                    "reason": e.reason,
                }
                for e in events
            ]
        }

    @router.get("/api/hardware-health")
    async def api_hardware_health() -> dict[str, Any]:
        return {"components": service.hardware.status_summary()}

    @router.get("/api/simulate/status", dependencies=[Depends(verify_auth)])
    async def api_simulate_status() -> dict[str, Any]:
        return service.get_rain_simulation_status()

    @router.post("/api/simulate/auto-rain/start", dependencies=[Depends(verify_auth)])
    async def api_simulate_auto_rain_start() -> dict[str, Any]:
        try:
            return await service.start_auto_rain_simulation()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/simulate/auto-rain/stop", dependencies=[Depends(verify_auth)])
    async def api_simulate_auto_rain_stop() -> dict[str, Any]:
        return await service.stop_auto_rain_simulation()

    @router.get("/api/config/location", dependencies=[Depends(verify_auth)])
    async def api_get_location() -> dict[str, Any]:
        loc = config.location
        return {
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "name": loc.name,
            "address": loc.address,
            "timezone": config.timezone,
        }

    @router.post("/api/config/location", dependencies=[Depends(verify_auth)])
    async def api_set_location(body: LocationRequest) -> dict[str, Any]:
        if not (-90 <= body.latitude <= 90 and -180 <= body.longitude <= 180):
            raise HTTPException(status_code=422, detail="invalid coordinates")
        address = body.address
        if not address and not body.name:
            address = await reverse_geocode(body.latitude, body.longitude)
        loc = await service.update_location(
            body.latitude,
            body.longitude,
            name=body.name,
            address=address,
        )
        config.location = loc
        return {
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "name": loc.name,
            "address": loc.address,
        }

    @router.get("/api/geocode/search", dependencies=[Depends(verify_auth)])
    async def api_geocode_search(q: str = Query(min_length=1)) -> dict[str, Any]:
        results = await search_locations(q)
        return {
            "results": [
                {
                    "name": r.name,
                    "latitude": r.latitude,
                    "longitude": r.longitude,
                }
                for r in results
            ]
        }

    @router.get("/api/geocode/reverse", dependencies=[Depends(verify_auth)])
    async def api_geocode_reverse(
        lat: float = Query(ge=-90, le=90),
        lon: float = Query(ge=-180, le=180),
    ) -> dict[str, str]:
        address = await reverse_geocode(lat, lon)
        return {"address": address}

    @router.get("/api/devices/setup", dependencies=[Depends(verify_auth)])
    async def api_devices_setup() -> dict[str, Any]:
        env = EnvSettings()
        base = Path(service.config_path).parent
        paths = resolve_credential_paths(base)
        return get_import_setup_status(
            smartthings_pat=env.smartthings_pat or service.smartthings_pat,
            tuya_api_key=env.tuya_api_key,
            tuya_api_secret=env.tuya_api_secret,
            tuya_api_region=env.tuya_api_region,
            tuya_api_device_id=env.tuya_api_device_id,
            meross_email=env.meross_email,
            meross_password=env.meross_password,
            paths={
                "tinytuya_json": paths.get("tinytuya_json"),
                "devices_json": paths.get("devices_json"),
                "env_file": paths.get("env_file"),
            },
        )

    @router.get("/api/devices/discover", dependencies=[Depends(verify_auth)])
    async def api_discover_devices(
        lan_scan: bool = Query(default=True),
    ) -> dict[str, Any]:
        env = EnvSettings()
        base = Path(service.config_path).parent
        paths = resolve_credential_paths(base)
        result = await discover_all(
            smartthings_pat=env.smartthings_pat or service.smartthings_pat,
            tuya_api_key=env.tuya_api_key,
            tuya_api_secret=env.tuya_api_secret,
            tuya_api_region=env.tuya_api_region,
            tuya_api_device_id=env.tuya_api_device_id,
            meross_email=env.meross_email,
            meross_password=env.meross_password,
            meross_api_base=env.meross_api_base,
            meross_mfa_code=env.meross_mfa_code,
            tuya_config_file=paths.get("tinytuya_json"),
            tuya_devices_file=paths.get("devices_json"),
            lan_scan=lan_scan,
        )
        result["devices"] = annotate_discovered_system_status(result["devices"], config.pumps)
        result["import_status"] = import_status_counts(result["devices"])
        return result

    @router.post("/api/devices/upload-tuya-json", dependencies=[Depends(verify_auth)])
    async def api_upload_tuya_json(request: Request) -> dict[str, Any]:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=422, detail="file required")
        base = Path(service.config_path).parent
        cred_dir = base / "credentials"
        cred_dir.mkdir(exist_ok=True)
        filename = getattr(upload, "filename", "") or "devices.json"
        dest = cred_dir / ("tinytuya.json" if "tinytuya" in filename else "devices.json")
        content = await upload.read()  # type: ignore[union-attr]
        dest.write_bytes(content)
        return {"saved": str(dest), "size": len(content), "filename": dest.name}

    @router.post("/api/devices/import", dependencies=[Depends(verify_auth)])
    async def api_import_devices(body: ImportPumpsRequest) -> dict[str, Any]:
        if body.mode not in ("merge", "replace"):
            raise HTTPException(status_code=422, detail="mode must be merge or replace")

        pumps = []
        for item in body.pumps:
            name = item.name.strip()
            if not name:
                name = slugify_pump_name(
                    item.tuya_device_id or item.meross_device_uuid or item.smartthings_device_id
                )
            pumps.append(
                discovered_dict_to_pump_config(
                    item.model_dump(),
                    name,
                    enabled=item.enabled,
                )
            )
        try:
            saved = await service.import_pumps(pumps, mode=body.mode)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Could not write config.yaml: {exc}. "
                    "If running in Docker, ensure config.yaml is not mounted read-only."
                ),
            ) from exc
        return {"pumps": [{"name": p.name, "enabled": p.enabled} for p in saved]}

    @router.post("/api/devices/auto-import", dependencies=[Depends(verify_auth)])
    async def api_auto_import_devices(
        lan_scan: bool = Query(default=True),
        mode: str = Query(default="merge"),
    ) -> dict[str, Any]:
        if mode not in ("merge", "replace"):
            raise HTTPException(status_code=422, detail="mode must be merge or replace")
        env = EnvSettings()
        base = Path(service.config_path).parent
        paths = resolve_credential_paths(base)
        result = await auto_import_devices(
            existing=config.pumps,
            smartthings_pat=env.smartthings_pat or service.smartthings_pat,
            tuya_api_key=env.tuya_api_key,
            tuya_api_secret=env.tuya_api_secret,
            tuya_api_region=env.tuya_api_region,
            tuya_api_device_id=env.tuya_api_device_id,
            meross_email=env.meross_email,
            meross_password=env.meross_password,
            meross_api_base=env.meross_api_base,
            meross_mfa_code=env.meross_mfa_code,
            tuya_config_file=paths.get("tinytuya_json"),
            tuya_devices_file=paths.get("devices_json"),
            lan_scan=lan_scan,
        )
        pumps = result["pumps"]
        stats = result["stats"]
        discovery = result["discovery"]
        raw_devices = discovery.get("devices", [])

        def _annotated(existing: list) -> tuple[list, dict[str, int]]:
            rows = annotate_discovered_system_status(raw_devices, existing)
            return rows, import_status_counts(rows)

        if not pumps:
            devices, import_counts = _annotated(config.pumps)
            return {
                "pumps": [],
                "stats": stats,
                "sources": discovery.get("sources", {}),
                "errors": discovery.get("errors", {}),
                "devices": devices,
                "setup": discovery.get("setup"),
                "import_status": import_counts,
                "message": "No devices found to import",
            }
        try:
            saved = await service.import_pumps(pumps, mode=mode)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Could not write config.yaml: {exc}. "
                    "If running in Docker, ensure config.yaml is not mounted read-only."
                ),
            ) from exc
        devices, import_counts = _annotated(saved)
        return {
            "pumps": [{"name": p.name, "enabled": p.enabled} for p in saved],
            "stats": stats,
            "sources": discovery.get("sources", {}),
            "errors": discovery.get("errors", {}),
            "devices": devices,
            "setup": discovery.get("setup"),
            "import_status": import_counts,
            "message": (
                f"Synced {len(pumps)} pump(s): {stats['added']} added, "
                f"{stats['updated']} updated"
            ),
        }

    @router.get("/api/pumps", dependencies=[Depends(verify_auth)])
    async def api_list_pumps() -> dict[str, Any]:
        return {
            "pumps": [
                {
                    "name": p.name,
                    "enabled": p.enabled,
                    "tuya_device_id": p.tuya.device_id,
                    "tuya_ip": p.tuya.ip,
                    "tuya_switch_code": p.tuya.switch_code,
                    "meross_device_uuid": p.meross.device_uuid,
                    "meross_channel": p.meross.channel,
                    "meross_switch_code": p.meross.switch_code,
                    "smartthings_device_id": p.smartthings.device_id,
                }
                for p in config.pumps
            ]
        }

    @router.delete("/api/pumps/{name}", dependencies=[Depends(verify_auth)])
    async def api_remove_pump(name: str) -> dict[str, Any]:
        try:
            saved = await service.remove_pump(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown pump: {name}") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Could not write config.yaml: {exc}. "
                    "If running in Docker, ensure config.yaml is not mounted read-only."
                ),
            ) from exc
        return {
            "removed": name,
            "pumps": [{"name": p.name, "enabled": p.enabled} for p in saved],
        }

    @router.get("/health")
    async def health() -> JSONResponse:
        """Health check — always unauthenticated so monitors work even if API keys fail."""
        summary = _health_summary(config, service, scheduler)
        code = 200 if summary["status"] == "ok" else 503
        return JSONResponse(status_code=code, content=summary)

    return router
