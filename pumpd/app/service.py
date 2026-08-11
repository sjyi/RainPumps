"""Core orchestration — forecast ingest, rules evaluation, device control."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from pathlib import Path
from typing import Any

from app.config import AppConfig, LocationConfig, PumpConfig, remove_pump, save_location, save_pumps
from app.device_import import load_tuya_cloud_credentials, resolve_credential_paths
from app.devices.base import CommandResult, DeviceState
from app.devices.composite import CompositePumpDevice
from app.devices.meross_cloud import MerossCloudDevice, MerossCloudSession
from app.devices.smartthings import SmartThingsDevice
from app.devices.tuya_cloud import TuyaCloudDevice
from app.devices.tuya_local import TuyaLocalDevice
from app.engine import (
    EvaluateResult,
    HourlyForecast,
    PumpCommand,
    PumpPhase,
    RainState,
    _as_utc,
    evaluate,
)
from app.hardware_health import CommandLockError, HardwareMonitor
from app.models import (
    EventRow,
    ForecastRow,
    HardwareHealthRow,
    ProviderHealthRow,
    PumpStateRow,
    WeatherCurrentRow,
    WeatherDailyRow,
)
from app.notify import Notifier
from app.rain_simulation import (
    DRAIN_WAIT_SECONDS,
    RAIN_PHASE_SECONDS,
    SIM_POST_RAIN_DRAIN_MINUTES,
    RainSimulationState,
    inject_simulation_forecast,
)
from app.safety import compute_safety_flags, set_engine_meta
from app.switch_stagger import group_turn_on_commands, sort_commands_by_switch
from app.signals.forecast_signal import ForecastSignal
from app.signals.mqtt_signal import MqttRainSignal
from app.weather.display import CurrentConditions, DailyForecast
from app.weather.nws import NwsProvider
from app.weather.open_meteo import OpenMeteoProvider

logger = logging.getLogger(__name__)


class DeviceCommandError(Exception):
    """Manual device command failed after optional timeout retry."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(result.message)

PROVIDERS = {
    "open_meteo": OpenMeteoProvider(),
    "nws": NwsProvider(),
}


class PumpService:
    def __init__(
        self,
        config: AppConfig,
        session_factory: sessionmaker[Session],
        smartthings_pat: str = "",
        tuya_api_key: str = "",
        tuya_api_secret: str = "",
        tuya_api_region: str = "us",
        tuya_api_device_id: str = "",
        meross_email: str = "",
        meross_password: str = "",
        meross_api_base: str = "https://iotx-us.meross.com",
        meross_mfa_code: str = "",
        config_path: str = "config.yaml",
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.session_factory = session_factory
        self.smartthings_pat = smartthings_pat
        self.tuya_api_key = tuya_api_key
        self.tuya_api_secret = tuya_api_secret
        self.tuya_api_region = tuya_api_region
        self.tuya_api_device_id = tuya_api_device_id
        self.tuya_cloud_client = self._create_tuya_cloud_client()
        self.meross_session = MerossCloudSession(
            email=meross_email,
            password=meross_password,
            api_base_url=meross_api_base,
            mfa_code=meross_mfa_code,
        )
        self.notifier = Notifier(config)
        self.hardware = HardwareMonitor(config, session_factory)
        self.devices: dict[str, CompositePumpDevice] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.forecast_signal = ForecastSignal(session_factory, config.rules)
        self.mqtt_signal = MqttRainSignal(config.mqtt)
        self.scheduler_running = False
        self.rain_simulation = RainSimulationState()
        self._simulation_task: asyncio.Task[None] | None = None
        self._simulation_drain_backup: int | None = None
        self._online_probe_cache: dict[str, dict[str, str]] | None = None
        self._online_probe_cache_at: float = 0.0
        self._online_probe_cache_ttl = 30.0
        self._build_devices()

    def _create_tuya_cloud_client(self) -> Any | None:
        base = Path(self.config_path).parent
        paths = resolve_credential_paths(base)
        key, secret, region, device_id, cfg_path = load_tuya_cloud_credentials(
            api_key=self.tuya_api_key,
            api_secret=self.tuya_api_secret,
            api_region=self.tuya_api_region,
            api_device_id=self.tuya_api_device_id,
            config_file=paths.get("tinytuya_json"),
        )
        if not key or not secret:
            return None
        try:
            import tinytuya

            return tinytuya.Cloud(
                apiRegion=region or None,
                apiKey=key,
                apiSecret=secret,
                apiDeviceID=device_id or None,
                configFile=str(cfg_path) if cfg_path else tinytuya.CONFIGFILE,
            )
        except Exception:
            logger.exception("failed to initialize Tuya cloud client")
            return None

    def _build_devices(self) -> None:
        self.devices.clear()
        self.locks.clear()
        mode = self.config.devices.control_mode
        use_local = mode in ("local", "auto")
        use_cloud = mode in ("cloud", "auto") and self.tuya_cloud_client is not None
        use_meross = self.meross_session.configured and mode in ("local", "cloud", "auto")
        for pump in self.config.pumps:
            tuya_local = None
            if use_local and pump.tuya.device_id and pump.tuya.ip and pump.tuya.local_key:
                tuya_local = TuyaLocalDevice(
                    pump.name,
                    pump.tuya.device_id,
                    pump.tuya.ip,
                    pump.tuya.local_key,
                    pump.tuya.version,
                    switch_code=pump.tuya.switch_code,
                )
            tuya_cloud = None
            if use_cloud and pump.tuya.device_id:
                tuya_cloud = TuyaCloudDevice(
                    pump.name,
                    pump.tuya.device_id,
                    self.tuya_cloud_client,
                    switch_code=pump.tuya.switch_code,
                )
            meross_cloud = None
            if use_meross and pump.meross.device_uuid:
                meross_cloud = MerossCloudDevice(
                    pump.name,
                    pump.meross.device_uuid,
                    pump.meross.channel,
                    self.meross_session,
                )
            st = None
            if pump.smartthings.device_id and self.smartthings_pat:
                st = SmartThingsDevice(pump.name, pump.smartthings.device_id, self.smartthings_pat)
            self.devices[pump.name] = CompositePumpDevice(
                pump.name,
                tuya_local,
                st,
                tuya_cloud=tuya_cloud,
                meross_cloud=meross_cloud,
                control_mode=mode,
            )
            self.locks[pump.name] = asyncio.Lock()

    @asynccontextmanager
    async def _pump_lock(self, name: str) -> AsyncIterator[None]:
        timeout = self.config.api.lock_timeout_seconds
        try:
            await asyncio.wait_for(self.locks[name].acquire(), timeout=timeout)
        except TimeoutError as exc:
            raise CommandLockError(f"command lock timeout for {name} after {timeout}s") from exc
        try:
            yield
        finally:
            self.locks[name].release()

    async def startup(self) -> None:
        await self.mqtt_signal.start()
        if self.meross_session.configured and any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            try:
                await self.meross_session.startup()
            except Exception:
                logger.exception("failed to initialize Meross cloud session")
        self._ensure_pump_rows()
        await self.reconcile_devices()
        await self.poll_forecasts()

    async def shutdown(self) -> None:
        for pump_cfg in self.config.pumps:
            async with self._pump_lock(pump_cfg.name):
                with self.session_factory() as session:
                    row = session.get(PumpStateRow, pump_cfg.name)
                    if not row:
                        continue
                    if row.phase == "post_rain_drain" and row.mode != "manual_on":
                        self._log_event(
                            pump_cfg.name,
                            "shutdown",
                            "post-rain drain aborted on shutdown",
                            session=session,
                        )
                    if row.mode != "manual_on" and row.device_on:
                        await self.devices[pump_cfg.name].turn_off()
                        row.device_on = False
                        self._log_event(
                            pump_cfg.name,
                            "shutdown",
                            "pump commanded off on shutdown",
                            session=session,
                        )
                    session.commit()
        await self.meross_session.shutdown()
        await self.mqtt_signal.stop()

    def _ensure_pump_rows(self) -> None:
        with self.session_factory() as session:
            existing = {r.name for r in session.scalars(select(PumpStateRow)).all()}
            now = datetime.now(UTC)
            for pump in self.config.pumps:
                if pump.name not in existing:
                    session.add(
                        PumpStateRow(
                            name=pump.name,
                            phase="idle",
                            mode="auto",
                            device_on=False,
                            duty_on=True,
                            runtime_continuous_min=0,
                            safety_override_approved=False,
                            updated_at=now,
                        )
                    )
            session.commit()

    def _get_pump_row(self, name: str) -> PumpStateRow | None:
        with self.session_factory() as session:
            return session.get(PumpStateRow, name)

    async def reconcile_devices(self) -> None:
        """Startup reconciliation: device wins for device_on; sync hardware to DB intent."""
        for pump_cfg in self.config.pumps:
            device = self.devices.get(pump_cfg.name)
            if not device:
                continue
            async with self._pump_lock(pump_cfg.name):
                physical = await device.get_state()
                physical_on = physical == DeviceState.ON
                db_device_on_before = False
                pump_mode = "auto"
                with self.session_factory() as session:
                    row = session.get(PumpStateRow, pump_cfg.name)
                    if not row:
                        continue
                    db_device_on_before = row.device_on
                    pump_mode = row.mode
                    if physical_on != row.device_on:
                        row.device_on = physical_on
                        session.add(
                            EventRow(
                                ts=datetime.now(UTC),
                                pump_name=pump_cfg.name,
                                event_type="reconcile",
                                reason=f"device state wins: device_on={physical_on}",
                                details_json=json.dumps({"db_was": db_device_on_before}),
                            )
                        )
                    session.commit()

                if pump_mode != "manual_on" and not db_device_on_before and physical_on:
                    await device.turn_off()
                    with self.session_factory() as session:
                        row = session.get(PumpStateRow, pump_cfg.name)
                        if row:
                            row.device_on = False
                            session.add(
                                EventRow(
                                    ts=datetime.now(UTC),
                                    pump_name=pump_cfg.name,
                                    event_type="reconcile",
                                    reason="commanded off: DB intent off, device was on",
                                    details_json=None,
                                )
                            )
                            session.commit()

    async def poll_forecasts(self) -> None:
        if self.rain_simulation.active:
            logger.debug("skipping poll_forecasts during rain simulation")
            return
        lat = self.config.location.latitude
        lon = self.config.location.longitude
        all_forecasts: dict[str, list[HourlyForecast]] = {}
        now = datetime.now(UTC)

        for name in self.config.weather.providers:
            provider = PROVIDERS.get(name)
            if not provider:
                continue
            try:
                forecasts = await provider.fetch(lat, lon)
                all_forecasts[name] = forecasts
                with self.session_factory() as session:
                    self._update_provider_health(session, name, ok=True)
                    session.execute(delete(ForecastRow).where(ForecastRow.provider == name))
                    for f in forecasts:
                        session.add(
                            ForecastRow(
                                provider=name,
                                hour_ts=f.hour_ts,
                                pop_pct=f.pop_pct,
                                rain_mm=f.rain_mm,
                                fetched_at=now,
                            )
                        )
                    session.commit()
            except Exception as exc:
                logger.exception("forecast fetch failed for %s", name)
                with self.session_factory() as session:
                    self._update_provider_health(session, name, ok=False, error=str(exc))
                    session.commit()

        if len(all_forecasts) >= 2:
            self._log_provider_disagreement(all_forecasts)

        if all_forecasts:
            logger.info("stored forecasts from %d providers", len(all_forecasts))
            await self.run_evaluation()

        await self._poll_display_weather(lat, lon)

    async def _poll_display_weather(self, lat: float, lon: float) -> None:
        provider = OpenMeteoProvider()
        try:
            current, daily = await provider.fetch_display(
                lat, lon, timezone=self.config.timezone
            )
        except Exception:
            logger.exception("display weather fetch failed")
            return

        now = datetime.now(UTC)
        with self.session_factory() as session:
            if current:
                row = session.get(WeatherCurrentRow, 1)
                if not row:
                    row = WeatherCurrentRow(id=1)
                    session.add(row)
                row.temp_c = current.temp_c
                row.humidity_pct = current.humidity_pct
                row.weather_code = current.weather_code
                row.precipitation_mm = current.precipitation_mm
                row.rain_mm = current.rain_mm
                row.is_day = current.is_day
                row.fetched_at = now

            session.execute(delete(WeatherDailyRow))
            for day in daily:
                session.add(
                    WeatherDailyRow(
                        day=day.day.isoformat(),
                        weather_code=day.weather_code,
                        temp_max_c=day.temp_max_c,
                        temp_min_c=day.temp_min_c,
                        precip_sum_mm=day.precip_sum_mm,
                        pop_max_pct=day.pop_max_pct,
                        fetched_at=now,
                    )
                )
            session.commit()

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        *,
        name: str = "",
        address: str = "",
    ) -> LocationConfig:
        loc = LocationConfig(
            latitude=latitude,
            longitude=longitude,
            name=name or self.config.location.name,
            address=address or self.config.location.address,
        )
        self.config.location = loc
        save_location(self.config_path, loc)
        await self.poll_forecasts()
        return loc

    async def import_pumps(
        self,
        pumps: list[PumpConfig],
        *,
        mode: str = "merge",
    ) -> list[PumpConfig]:
        saved = save_pumps(self.config_path, pumps, mode=mode)  # type: ignore[arg-type]
        self.config.pumps = saved
        self._build_devices()
        if self.meross_session.configured and any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            if not self.meross_session.started:
                try:
                    await self.meross_session.startup()
                except Exception:
                    logger.exception("failed to initialize Meross cloud session after import")
        self._ensure_pump_rows()
        self._log_event(
            None,
            "device_import",
            f"imported {len(saved)} pump(s)",
            details={"pumps": [p.name for p in saved], "mode": mode},
        )
        await self.reconcile_devices()
        return saved

    async def remove_pump(self, name: str) -> list[PumpConfig]:
        if not any(p.name == name for p in self.config.pumps):
            raise KeyError(name)
        saved = remove_pump(self.config_path, name)
        self.config.pumps = saved
        with self.session_factory() as session:
            session.execute(delete(PumpStateRow).where(PumpStateRow.name == name))
            session.execute(
                delete(HardwareHealthRow).where(HardwareHealthRow.component_id == name)
            )
            session.commit()
        self._build_devices()
        self._log_event(
            name,
            "device_remove",
            f"removed pump {name} from local config (Tuya/SmartThings unchanged)",
            details={"remaining": [p.name for p in saved]},
        )
        return saved

    async def probe_pumps_online(
        self, *, force: bool = False, cache_ttl: float | None = None
    ) -> dict[str, dict[str, str]]:
        """Probe each configured pump by reading live switch state."""
        now = time.monotonic()
        ttl = self._online_probe_cache_ttl if cache_ttl is None else cache_ttl
        if (
            not force
            and self._online_probe_cache is not None
            and now - self._online_probe_cache_at < ttl
        ):
            return self._online_probe_cache

        if self.meross_session.configured and any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            if not self.meross_session.started:
                try:
                    await asyncio.wait_for(self.meross_session.startup(), timeout=15.0)
                except Exception:
                    logger.debug("meross startup before online probe failed", exc_info=True)

        probe_timeout = 10.0
        sem = asyncio.Semaphore(4)

        async def probe_one(name: str) -> dict[str, str]:
            device = self.devices.get(name)
            if device is None or not device.has_control_path():
                return {"status": "unconfigured", "detail": "missing credentials"}
            async with sem:
                try:
                    state = await asyncio.wait_for(device.get_state(), timeout=probe_timeout)
                except TimeoutError:
                    return {"status": "offline", "detail": "timeout"}
                except Exception as exc:
                    return {"status": "offline", "detail": str(exc)}
            if state == DeviceState.UNKNOWN:
                return {"status": "offline", "detail": "unreachable"}
            adapter = device._last_adapter or "device"
            return {"status": "online", "detail": f"{adapter}:{state.value}"}

        names = [p.name for p in self.config.pumps]
        results = await asyncio.gather(*(probe_one(name) for name in names))
        cached = dict(zip(names, results, strict=True))
        self._online_probe_cache = cached
        self._online_probe_cache_at = now
        return cached

    async def get_pump_cards(self) -> list[dict[str, Any]]:
        """Pump rows enriched with live online status for dashboard cards."""
        with self.session_factory() as session:
            rows = session.scalars(select(PumpStateRow)).all()
            state_by_name = {row.name: row for row in rows}
        online_map = await self.probe_pumps_online()
        hw_map = {h["component_id"]: h for h in self.hardware.status_summary()}
        cards: list[dict[str, Any]] = []
        for cfg in self.config.pumps:
            row = state_by_name.get(cfg.name)
            online = online_map.get(cfg.name, {"status": "unknown", "detail": ""})
            hw = hw_map.get(cfg.name)
            switch_code = (
                cfg.meross.switch_code or f"switch_{cfg.meross.channel + 1}"
                if cfg.meross.device_uuid
                else cfg.tuya.switch_code or "switch_1"
            )
            device_id = cfg.meross.device_uuid or cfg.tuya.device_id
            cards.append(
                {
                    "name": cfg.name,
                    "enabled": cfg.enabled,
                    "phase": row.phase if row else "idle",
                    "mode": row.mode if row else "auto",
                    "device_on": row.device_on if row else False,
                    "runtime_today_min": row.runtime_today_min if row else 0,
                    "runtime_continuous_min": row.runtime_continuous_min if row else 0,
                    "safety_override_approved": row.safety_override_approved if row else False,
                    "online_status": online["status"],
                    "online_detail": online.get("detail", ""),
                    "hardware_status": hw["status"] if hw else None,
                    "switch_code": switch_code,
                    "device_id": device_id,
                    "device_backend": (
                        "meross"
                        if cfg.meross.device_uuid
                        else "tuya"
                        if cfg.tuya.device_id
                        else ""
                    ),
                }
            )
        return cards

    def _log_provider_disagreement(self, all_forecasts: dict[str, list[HourlyForecast]]) -> None:
        names = list(all_forecasts.keys())
        a_name, b_name = names[0], names[1]
        disagreements: list[dict[str, Any]] = []
        by_hour_a = {_as_utc(f.hour_ts): f for f in all_forecasts[a_name]}
        by_hour_b = {_as_utc(f.hour_ts): f for f in all_forecasts[b_name]}
        for hour in set(by_hour_a) | set(by_hour_b):
            fa = by_hour_a.get(hour)
            fb = by_hour_b.get(hour)
            if fa and fb and (fa.pop_pct != fb.pop_pct or fa.rain_mm != fb.rain_mm):
                disagreements.append(
                    {
                        "hour": hour.isoformat(),
                        a_name: {"pop": fa.pop_pct, "rain_mm": fa.rain_mm},
                        b_name: {"pop": fb.pop_pct, "rain_mm": fb.rain_mm},
                    }
                )
        if disagreements:
            self._log_event(
                None,
                "provider_disagreement",
                f"{a_name} vs {b_name}: {len(disagreements)} hours differ",
                details={"hours": disagreements[:12]},
            )

    def _update_provider_health(
        self, session: Session, provider: str, *, ok: bool, error: str = ""
    ) -> None:
        now = datetime.now(UTC)
        row = session.get(ProviderHealthRow, provider)
        if not row:
            row = ProviderHealthRow(provider=provider)
            session.add(row)
        if ok:
            row.last_ok_at = now
            row.last_error = None
        else:
            row.last_error = error
            row.last_error_at = now

    def _load_forecast_window(self, session: Session, hours: int) -> list[HourlyForecast]:
        now = datetime.now(UTC)
        end = now + timedelta(hours=hours)
        start = now - timedelta(hours=1)
        rows = session.scalars(select(ForecastRow).order_by(ForecastRow.hour_ts)).all()
        by_hour: dict[datetime, HourlyForecast] = {}
        for r in rows:
            hour = _as_utc(r.hour_ts)
            if hour < start or hour > end:
                continue
            hf = HourlyForecast(hour_ts=hour, pop_pct=r.pop_pct, rain_mm=r.rain_mm)
            existing = by_hour.get(hour)
            if existing:
                by_hour[hour] = HourlyForecast(
                    hour_ts=hour,
                    pop_pct=max(existing.pop_pct, hf.pop_pct),
                    rain_mm=max(existing.rain_mm, hf.rain_mm),
                )
            else:
                by_hour[hour] = hf
        return sorted(by_hour.values(), key=lambda x: x.hour_ts)

    def _row_to_phase(self, row: PumpStateRow, pump_cfg: PumpConfig) -> PumpPhase:
        return PumpPhase(
            name=row.name,
            enabled=pump_cfg.enabled,
            phase=row.phase,  # type: ignore[arg-type]
            mode=row.mode,  # type: ignore[arg-type]
            device_on=row.device_on,
            duty_on=row.duty_on,
            runtime_continuous_min=row.runtime_continuous_min,
            cooldown_until=_as_utc(row.cooldown_until) if row.cooldown_until else None,
            manual_revert_at=_as_utc(row.manual_revert_at) if row.manual_revert_at else None,
            post_rain_drain_started_at=(
                _as_utc(row.post_rain_drain_started_at) if row.post_rain_drain_started_at else None
            ),
            sensor_dry_since=_as_utc(row.sensor_dry_since) if row.sensor_dry_since else None,
            duty_cycle_started_at=(
                _as_utc(row.duty_cycle_started_at) if row.duty_cycle_started_at else None
            ),
            safety_override_approved=row.safety_override_approved,
        )

    def _apply_phase_to_row(self, row: PumpStateRow, phase: PumpPhase) -> None:
        row.phase = phase.phase
        row.mode = phase.mode
        row.device_on = phase.device_on
        row.duty_on = phase.duty_on
        row.runtime_continuous_min = phase.runtime_continuous_min
        row.cooldown_until = phase.cooldown_until
        row.manual_revert_at = phase.manual_revert_at
        row.post_rain_drain_started_at = phase.post_rain_drain_started_at
        row.sensor_dry_since = phase.sensor_dry_since
        row.duty_cycle_started_at = phase.duty_cycle_started_at
        row.safety_override_approved = phase.safety_override_approved
        row.updated_at = datetime.now(UTC)

    async def get_rain_state(self) -> RainState:
        if self.config.mqtt.enabled:
            mqtt = await self.mqtt_signal.get_state()
            if mqtt.confidence > 0:
                self.hardware.record_sensor_message(mqtt)
                return mqtt
        return await self.forecast_signal.get_state()

    async def run_evaluation(self) -> EvaluateResult | None:
        now = datetime.now(UTC)
        rain = await self.get_rain_state()
        self.hardware.check_sensor_stale(rain)

        with self.session_factory() as session:
            set_engine_meta(session, "engine_last_eval_at", now.isoformat())
            safety = compute_safety_flags(session, self.config, rain)
            window = self._load_forecast_window(session, self.config.rules.lookahead_hours + 2)
            pump_cfgs = {p.name: p for p in self.config.pumps}
            phases = []
            for row in session.scalars(select(PumpStateRow)).all():
                cfg = pump_cfgs.get(row.name)
                if cfg:
                    phases.append(self._row_to_phase(row, cfg))

            result = evaluate(
                now=now,
                rain=rain,
                forecast_window=window,
                pumps=phases,
                rules=self.config.rules,
                safety=safety,
                max_runtime_minutes=self.config.safety.max_continuous_runtime_minutes,
                min_cooldown_minutes=self.config.safety.min_cooldown_minutes,
                mqtt_min_confidence=self.config.mqtt.min_confidence,
            )

            if safety.stale_forecast and safety.mqtt_stale_override:
                await self.notifier.send(
                    "pumpd", "Forecasts stale but MQTT sensor reports rain — pumps kept running"
                )
            elif safety.stale_forecast:
                await self.notifier.send("pumpd", "Stale forecast watchdog — pumps off", "high")
            if safety.engine_watchdog:
                await self.notifier.send("pumpd", "Engine evaluation watchdog tripped", "high")

            session.commit()

        await self._execute_commands(result.commands)

        with self.session_factory() as session:
            rows = {r.name: r for r in session.scalars(select(PumpStateRow)).all()}
            for phase in result.pumps:
                state_row = rows.get(phase.name)
                if state_row is None:
                    continue
                self._apply_phase_to_row(state_row, phase)
                if phase.device_on:
                    state_row.runtime_continuous_min += self.config.rules.evaluate_minutes
                    state_row.runtime_today_min += self.config.rules.evaluate_minutes
                else:
                    state_row.runtime_continuous_min = 0
                self._update_runtime_today(state_row)
            session.commit()

        return result

    def _update_runtime_today(self, row: PumpStateRow) -> None:
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        if row.updated_at:
            updated_local = row.updated_at.astimezone(tz)
            if updated_local.date() != now.date():
                row.runtime_today_min = 0

    async def _execute_commands(self, commands: list[PumpCommand]) -> None:
        if not commands:
            return

        pumps_by_name = {p.name: p for p in self.config.pumps}
        turn_offs = [cmd for cmd in commands if cmd.action == "turn_off"]
        turn_ons = [cmd for cmd in commands if cmd.action == "turn_on"]
        stagger = self.config.devices.switch_stagger_seconds

        for cmd in turn_offs:
            await self._execute_command(cmd)
            self._log_pump_decision(cmd)

        singles, by_device = group_turn_on_commands(turn_ons, pumps_by_name)
        for cmd in singles:
            await self._execute_command(cmd)
            self._log_pump_decision(cmd)

        async def run_staggered_group(device_id: str, group: list[PumpCommand]) -> None:
            ordered = sort_commands_by_switch(group, pumps_by_name)
            for index, cmd in enumerate(ordered):
                if index > 0 and stagger > 0:
                    logger.info(
                        "switch stagger: waiting %.0fs before %s (%s)",
                        stagger,
                        cmd.pump_name,
                        device_id,
                    )
                    await asyncio.sleep(stagger)
                await self._execute_command(cmd)
                self._log_pump_decision(cmd, stagger_index=index)

        tasks: list[asyncio.Task[None]] = []
        for device_id, group in by_device.items():
            if len(group) <= 1:
                cmd = group[0]
                await self._execute_command(cmd)
                self._log_pump_decision(cmd)
                continue
            tasks.append(asyncio.create_task(run_staggered_group(device_id, group)))
        if tasks:
            await asyncio.gather(*tasks)

    def _log_pump_decision(
        self,
        cmd: PumpCommand,
        *,
        stagger_index: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": "pump_decision",
            "pump": cmd.pump_name,
            "action": cmd.action,
            "reason": cmd.reason,
        }
        if stagger_index is not None:
            payload["stagger_index"] = stagger_index
        if self.config.logging.json_logs:
            logger.info(json.dumps(payload))
        else:
            suffix = f" (stagger #{stagger_index + 1})" if stagger_index is not None else ""
            logger.info("%s %s: %s%s", cmd.pump_name, cmd.action, cmd.reason, suffix)

    async def _execute_command(self, cmd: PumpCommand) -> None:
        device = self.devices.get(cmd.pump_name)
        if not device:
            return
        async with self._pump_lock(cmd.pump_name):
            if cmd.action == "turn_on":
                res = await device.turn_on()
            elif cmd.action == "turn_off":
                res = await device.turn_off()
            else:
                return
            verify_mismatch = "verify" in res.message.lower() if res.message else False
            self._log_event(
                cmd.pump_name,
                cmd.action,
                cmd.reason,
                details={"success": res.success, "adapter": res.adapter, "message": res.message},
            )
            if res.success:
                self.hardware.record_pump_success(cmd.pump_name)
            else:
                self.hardware.record_pump_failure(
                    cmd.pump_name, res.message, verify_mismatch=verify_mismatch
                )
                await self.notifier.send(
                    f"pumpd {cmd.pump_name}",
                    f"Control failed: {res.message}",
                    "high",
                )
            if verify_mismatch:
                await self.notifier.send(
                    f"pumpd {cmd.pump_name}",
                    f"Verify mismatch: {res.message}",
                    "high",
                )
            elif cmd.notify:
                await self.notifier.send(f"pumpd {cmd.pump_name}", cmd.reason)

    def _log_event(
        self,
        pump_name: str | None,
        event_type: str,
        reason: str,
        details: dict[str, Any] | None = None,
        session: Session | None = None,
    ) -> None:
        def _add(sess: Session) -> None:
            sess.add(
                EventRow(
                    ts=datetime.now(UTC),
                    pump_name=pump_name,
                    event_type=event_type,
                    reason=reason,
                    details_json=json.dumps(details) if details else None,
                )
            )

        if session:
            _add(session)
        else:
            with self.session_factory() as session:
                _add(session)
                session.commit()

    def _safety_active(self) -> bool:
        with self.session_factory() as session:
            rain = RainState(False, 0, 0, "forecast", datetime.now(UTC))
            safety = compute_safety_flags(session, self.config, rain)
            return safety.stale_forecast or safety.engine_watchdog

    async def _manual_device_command(self, name: str, *, turn_on: bool) -> CommandResult:
        device = self.devices.get(name)
        if device is None:
            return CommandResult(success=False, adapter="composite", message="device not configured")

        timeout = self.config.api.device_command_timeout_seconds
        action = device.turn_on if turn_on else device.turn_off
        action_label = "turn_on" if turn_on else "turn_off"

        try:
            result = await asyncio.wait_for(action(), timeout=timeout)
            if result.success:
                return result
            return result
        except TimeoutError:
            pass

        status_before = "unknown"
        try:
            state = await asyncio.wait_for(device.get_state(), timeout=timeout)
            status_before = state.value
        except TimeoutError:
            status_before = "unknown"

        self._log_event(
            name,
            f"{action_label}_retry",
            "initial command timed out; queried status before retry",
            details={"status_before_retry": status_before},
        )

        try:
            result = await asyncio.wait_for(action(), timeout=timeout)
        except TimeoutError:
            return CommandResult(
                success=False,
                adapter="composite",
                message="Initial request timed out; retry also timed out",
                timed_out=True,
                retried=True,
                status_before_retry=status_before,
            )

        message_parts = ["Initial request timed out"]
        if status_before != "unknown":
            message_parts.append(f"status before retry: {status_before}")
        if result.success:
            message_parts.append("retry succeeded")
        else:
            message_parts.append(f"retry failed: {result.message or 'unknown error'}")
        return CommandResult(
            success=result.success,
            adapter=result.adapter,
            message="; ".join(message_parts),
            timed_out=True,
            retried=True,
            status_before_retry=status_before,
        )

    async def set_pump_mode(
        self,
        name: str,
        mode: str,
        *,
        approve_safety_override: bool = False,
    ) -> tuple[PumpStateRow | None, CommandResult | None]:
        pump_cfg = next((p for p in self.config.pumps if p.name == name), None)
        if not pump_cfg:
            return None, None

        if (
            mode in ("manual_on", "manual_off")
            and self._safety_active()
            and not approve_safety_override
        ):
            raise ValueError(
                "safety hard-stop active; set approve_safety_override=true to override"
            )

        now = datetime.now(UTC)
        revert = now + timedelta(hours=self.config.rules.manual_revert_hours)
        async with self._pump_lock(name):
            with self.session_factory() as session:
                row = session.get(PumpStateRow, name)
                if not row:
                    return None, None
                row.mode = mode
                row.manual_revert_at = revert if mode != "auto" else None
                row.safety_override_approved = (
                    approve_safety_override if mode in ("manual_on", "manual_off") else False
                )
                session.commit()
                session.refresh(row)
            if approve_safety_override and mode in ("manual_on", "manual_off"):
                self._log_event(
                    name,
                    "safety_override",
                    f"manual {mode} approved over active safety hard-stop",
                )
                await self.notifier.send(
                    f"pumpd {name}",
                    f"Manual {mode} overriding safety (approved)",
                    "high",
                )
            if mode == "manual_on":
                cmd_result = await self._manual_device_command(name, turn_on=True)
                if not cmd_result.success:
                    raise DeviceCommandError(cmd_result)
            elif mode == "manual_off":
                cmd_result = await self._manual_device_command(name, turn_on=False)
                if not cmd_result.success:
                    raise DeviceCommandError(cmd_result)
            else:
                cmd_result = None
            await self.run_evaluation()
            row = self._get_pump_row(name)
            return row, cmd_result

    def get_rain_simulation_status(self) -> dict[str, Any]:
        return self.rain_simulation.to_dict()

    async def start_auto_rain_simulation(self) -> dict[str, Any]:
        if self.rain_simulation.active or (
            self._simulation_task is not None and not self._simulation_task.done()
        ):
            raise ValueError("rain simulation already running")

        enabled = {p.name for p in self.config.pumps if p.enabled}
        with self.session_factory() as session:
            rows = {r.name: r for r in session.scalars(select(PumpStateRow)).all()}
        auto_pumps = sorted(name for name in enabled if rows.get(name) and rows[name].mode == "auto")
        skipped = sorted(name for name in enabled if name not in auto_pumps)
        if not auto_pumps:
            raise ValueError("No enabled pumps are in Auto mode — switch pumps to Auto first")

        self.rain_simulation = RainSimulationState(
            active=True,
            phase="starting",
            message="Starting simulated rain forecast…",
            started_at=datetime.now(UTC),
            auto_pumps=auto_pumps,
            skipped_pumps=skipped,
        )
        self._simulation_task = asyncio.create_task(self._run_auto_rain_simulation())
        self._log_event(
            None,
            "rain_simulation_start",
            f"auto rain simulation started for {len(auto_pumps)} pump(s)",
            details={"auto_pumps": auto_pumps, "skipped_pumps": skipped},
        )
        return self.rain_simulation.to_dict()

    async def stop_auto_rain_simulation(self) -> dict[str, Any]:
        if self._simulation_task and not self._simulation_task.done():
            self._simulation_task.cancel()
            try:
                await self._simulation_task
            except asyncio.CancelledError:
                pass
            self._log_event(None, "rain_simulation_stop", "rain simulation stopped by user")
            return self.rain_simulation.to_dict()
        return self.rain_simulation.to_dict()

    async def _restore_after_simulation(self) -> None:
        if self._simulation_drain_backup is not None:
            self.config.rules.post_rain_drain_minutes = self._simulation_drain_backup
            self._simulation_drain_backup = None
        await self.poll_forecasts()
        await self.run_evaluation()

    async def _run_auto_rain_simulation(self) -> None:
        self._simulation_drain_backup = self.config.rules.post_rain_drain_minutes
        self.config.rules.post_rain_drain_minutes = SIM_POST_RAIN_DRAIN_MINUTES
        try:
            inject_simulation_forecast(
                self.session_factory,
                raining=True,
                lookahead_hours=self.config.rules.lookahead_hours,
            )
            self.rain_simulation.phase = "rain"
            self.rain_simulation.message = (
                "Simulated rain forecast active — running auto evaluation (pumps should start)"
            )
            await self.run_evaluation()

            await asyncio.sleep(RAIN_PHASE_SECONDS)
            if not self.rain_simulation.active:
                return

            inject_simulation_forecast(
                self.session_factory,
                raining=False,
                lookahead_hours=self.config.rules.lookahead_hours,
            )
            self.rain_simulation.phase = "dry"
            self.rain_simulation.message = (
                "Simulated rain ended — post-rain drain phase (pumps stay on briefly)"
            )
            await self.run_evaluation()

            self.rain_simulation.phase = "drain"
            self.rain_simulation.message = (
                f"Draining for {SIM_POST_RAIN_DRAIN_MINUTES} min — pumps should stop soon"
            )
            await asyncio.sleep(DRAIN_WAIT_SECONDS)
            if not self.rain_simulation.active:
                return

            await self.run_evaluation()
            self.rain_simulation.phase = "complete"
            self.rain_simulation.message = "Simulation complete — pumps should be off; live forecasts restored next"
            self._log_event(
                None,
                "rain_simulation_complete",
                "auto rain simulation finished",
                details={"auto_pumps": self.rain_simulation.auto_pumps},
            )
        except asyncio.CancelledError:
            self.rain_simulation.phase = "stopped"
            self.rain_simulation.message = "Simulation cancelled"
            raise
        except Exception as exc:
            logger.exception("rain simulation failed")
            self.rain_simulation.phase = "error"
            self.rain_simulation.message = f"Simulation error: {exc}"
            self._log_event(None, "rain_simulation_error", str(exc))
        finally:
            self.rain_simulation.active = False
            self._simulation_task = None
            await self._restore_after_simulation()
            if self.rain_simulation.phase not in ("stopped", "error"):
                self.rain_simulation.message = (
                    "Simulation complete — live weather forecasts restored"
                )

    def get_events(
        self,
        *,
        limit: int = 50,
        pump: str | None = None,
        since: str | None = None,
    ) -> list[EventRow]:
        with self.session_factory() as session:
            q = select(EventRow).order_by(EventRow.ts.desc())
            if pump:
                q = q.where(EventRow.pump_name == pump)
            if since:
                since_dt = datetime.fromisoformat(since)
                q = q.where(EventRow.ts >= since_dt)
            q = q.limit(limit)
            return list(session.scalars(q).all())

    def _load_daily_forecast(self, session: Session) -> list[DailyForecast]:
        rows = session.scalars(
            select(WeatherDailyRow).order_by(WeatherDailyRow.day)
        ).all()
        return [
            DailyForecast(
                day=date.fromisoformat(r.day),
                weather_code=r.weather_code,
                temp_max_c=r.temp_max_c,
                temp_min_c=r.temp_min_c,
                precip_sum_mm=r.precip_sum_mm,
                pop_max_pct=r.pop_max_pct,
            )
            for r in rows
        ]

    def _load_current_conditions(self, session: Session) -> CurrentConditions | None:
        row = session.get(WeatherCurrentRow, 1)
        if not row:
            return None
        return CurrentConditions(
            temp_c=row.temp_c,
            humidity_pct=row.humidity_pct,
            weather_code=row.weather_code,
            precipitation_mm=row.precipitation_mm,
            rain_mm=row.rain_mm,
            is_day=row.is_day,
            fetched_at=_as_utc(row.fetched_at),
        )

    def get_status(self) -> dict[str, Any]:
        with self.session_factory() as session:
            pumps = session.scalars(select(PumpStateRow)).all()
            events = session.scalars(
                select(EventRow).order_by(EventRow.ts.desc()).limit(20)
            ).all()
            health = session.scalars(select(ProviderHealthRow)).all()
            forecast = self._load_forecast_window(session, 12)
            forecast_7d = self._load_daily_forecast(session)
            current = self._load_current_conditions(session)
            last_forecast = session.scalar(select(func.max(ForecastRow.fetched_at)))
            from app.models import EngineMetaRow

            row = session.get(EngineMetaRow, "engine_last_eval_at")
            last_eval = row.value if row else None
        tz = ZoneInfo(self.config.timezone)
        loc = self.config.location
        location_label = loc.name or loc.address or f"{loc.latitude:.4f}, {loc.longitude:.4f}"
        return {
            "pumps": pumps,
            "events": events,
            "provider_health": health,
            "forecast_12h": forecast,
            "forecast_7d": forecast_7d,
            "current": current,
            "location": {
                "latitude": loc.latitude,
                "longitude": loc.longitude,
                "name": loc.name,
                "address": loc.address,
                "label": location_label,
                "timezone": self.config.timezone,
            },
            "forecast_12h_local": [
                {
                    "hour": f.hour_ts.astimezone(tz).strftime("%H:%M"),
                    "pop_pct": f.pop_pct,
                    "rain_mm": f.rain_mm,
                }
                for f in forecast
            ],
            "forecast_7d_local": [
                {
                    "date": d.day.isoformat(),
                    "day_name": d.day.strftime("%a"),
                    "weather_code": d.weather_code,
                    "description": d.description,
                    "temp_max_c": d.temp_max_c,
                    "temp_min_c": d.temp_min_c,
                    "precip_sum_mm": d.precip_sum_mm,
                    "pop_max_pct": d.pop_max_pct,
                }
                for d in forecast_7d
            ],
            "last_forecast_at": last_forecast,
            "last_eval_at": last_eval,
            "hardware_health": self.hardware.status_summary(),
        }
