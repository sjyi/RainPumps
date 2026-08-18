"""Core orchestration — forecast ingest, rules evaluation, device control."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import (
    AppConfig,
    DeviceLabelOverride,
    DeviceDisplayOrderEntry,
    DeviceRuntimeOverride,
    DisplayConfig,
    LocationConfig,
    PumpConfig,
    SafetyConfig,
    load_config,
    remove_pump,
    clear_local_devices,
    save_command_verify_settings,
    save_display,
    save_display_names,
    save_device_display_order,
    save_location,
    save_notifications_settings,
    save_pumps,
    save_runtime_settings,
)
from app.device_import import load_tuya_cloud_credentials, resolve_credential_paths
from app.devices.base import CommandResult, DeviceState
from app.devices.cloud_rename import (
    rename_meross_device,
    rename_meross_switch,
    rename_smartthings_device,
    rename_tuya_device,
    rename_tuya_switch,
)
from app.devices.composite import CompositePumpDevice
from app.device_keys import device_label_key
from app.display_names import (
    device_labels_map,
    display_name_settings_view,
    device_order_settings_view,
    pump_display_label,
)
from app.meross_names import (
    collect_meross_cloud_names,
    diff_meross_cloud_names,
    merge_device_label_rows,
)
from app.devices.meross_cloud import MerossCloudDevice, MerossCloudSession
from app.devices.smartthings import SmartThingsDevice
from app.devices.tuya_cloud import TuyaCloudDevice
from app.devices.tuya_local import TuyaLocalDevice
from app.manual_control import (
    ManualContext,
    ManualEnvSnapshot,
    ManualRevertKind,
    compute_manual_revert_at,
    dump_manual_context,
    parse_manual_context,
    resolve_manual_duration_minutes,
)
from app.engine import (
    EvaluateResult,
    HourlyForecast,
    PumpCommand,
    PumpPhase,
    RainState,
    _as_utc,
    evaluate,
    should_preemptive_start,
)
from app.google_gmail import GoogleGmailClient, default_redirect_uri, default_token_path
from app.hardware_health import CommandLockError, HardwareMonitor
from app.history_timeline import build_history_timeline
from app.models import (
    EventRow,
    ForecastHistoryRow,
    ForecastRow,
    HardwareHealthRow,
    ProviderHealthRow,
    PumpStateRow,
    WeatherCurrentRow,
    WeatherDailyRow,
)
from app.notify import Notifier
from app.pump_card_groups import group_pump_cards, parse_probe_switch_state
from app.rain_simulation import (
    DRAIN_WAIT_SECONDS,
    RAIN_PHASE_SECONDS,
    SIM_POST_RAIN_DRAIN_MINUTES,
    RainSimulationState,
    inject_simulation_forecast,
)
from app.runtime_config import (
    max_runtime_by_pump,
    pump_cards_from_config,
    runtime_settings_view,
)
from app.safety import compute_safety_flags, set_engine_meta
from app.signals.forecast_signal import ForecastSignal
from app.signals.mqtt_signal import MqttRainSignal
from app.switch_stagger import group_turn_on_commands, sort_commands_by_switch, sort_pumps_by_switch
from app.time_format import format_local
from app.weather.display import CurrentConditions, DailyForecast
from app.weather.nws import NwsProvider
from app.weather.open_meteo import OpenMeteoProvider

logger = logging.getLogger(__name__)

CONTROL_EVENT_TYPES = frozenset({"turn_on", "turn_off", "mode_change", "reconcile"})


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
        meross_lan_first: bool = False,
        google_oauth_client_id: str = "",
        google_oauth_client_secret: str = "",
        config_path: str = "config.yaml",
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.google_oauth_client_id = google_oauth_client_id
        self.google_oauth_client_secret = google_oauth_client_secret
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
            lan_first=meross_lan_first,
        )
        self.notifier = Notifier(config)
        self.gmail_client = self._build_gmail_client()
        self.hardware = HardwareMonitor(config, session_factory)
        self.devices: dict[str, CompositePumpDevice] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.forecast_signal = ForecastSignal(session_factory, config.rules)
        self.mqtt_signal = MqttRainSignal(config.mqtt)
        self.scheduler_running = False
        self.rain_simulation = RainSimulationState()
        self._simulation_task: asyncio.Task[None] | None = None
        self._manual_revert_tasks: dict[str, asyncio.Task[None]] = {}
        self._simulation_drain_backup: int | None = None
        self._online_probe_cache: dict[str, dict[str, str]] | None = None
        self._online_probe_cache_at: float = 0.0
        self._online_probe_cache_ttl = 60.0
        self._meross_ui_cache_at: float = 0.0
        self._meross_ui_cache_ttl = 60.0
        self._tuya_ip_cache: dict[str, str] = {}
        self._build_devices()

    def _build_gmail_client(self) -> GoogleGmailClient:
        return GoogleGmailClient(
            client_id=self.google_oauth_client_id,
            client_secret=self.google_oauth_client_secret,
            token_path=default_token_path(self.config_path),
            redirect_uri=default_redirect_uri(self.config.notifications.public_base_url),
        )

    def _reload_config(self) -> None:
        from app.config import load_config

        self.config = load_config(self.config_path)
        self.notifier.config = self.config
        self.gmail_client = self._build_gmail_client()

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

    def _resolve_tuya_ip(self, device_id: str, configured_ip: str) -> str:
        ip = (configured_ip or "").strip()
        if ip:
            return ip
        return self._tuya_ip_cache.get(device_id, "")

    async def _refresh_tuya_ip_cache(self) -> None:
        needs_scan = any(
            pump.tuya.device_id
            and pump.tuya.local_key
            and not self._resolve_tuya_ip(pump.tuya.device_id, pump.tuya.ip)
            for pump in self.config.pumps
        )
        if not needs_scan:
            return
        try:
            import tinytuya

            found = await asyncio.to_thread(
                tinytuya.deviceScan,
                False,
                6,
                False,
                False,
                True,
            )
        except Exception:
            logger.debug("tuya lan scan for online probe failed", exc_info=True)
            return
        for ip, item in (found or {}).items():
            if not isinstance(item, dict):
                continue
            dev_id = str(item.get("gwId") or item.get("id") or "")
            if dev_id:
                self._tuya_ip_cache[dev_id] = str(ip)

    def _pump_physical_device_key(self, pump: PumpConfig) -> str:
        if pump.tuya.device_id:
            return f"tuya:{pump.tuya.device_id}"
        if pump.meross.device_uuid:
            return f"meross:{pump.meross.device_uuid}"
        if pump.smartthings.device_id:
            return f"st:{pump.smartthings.device_id}"
        return f"pump:{pump.name}"

    def _tuya_local_probe_device(self, pump: PumpConfig) -> TuyaLocalDevice | None:
        device_id = pump.tuya.device_id
        local_key = pump.tuya.local_key
        ip = self._resolve_tuya_ip(device_id, pump.tuya.ip)
        if not device_id or not local_key or not ip:
            return None
        return TuyaLocalDevice(
            pump.name,
            device_id,
            ip,
            local_key,
            pump.tuya.version,
            switch_code=pump.tuya.switch_code,
        )

    def _build_devices(self) -> None:
        self.devices.clear()
        self.locks.clear()
        mode = self.config.devices.control_mode
        use_local = mode in ("local", "auto")
        use_cloud = mode in ("cloud", "auto") and self.tuya_cloud_client is not None
        use_meross = self.meross_session.configured and mode in ("local", "cloud", "auto")
        for pump in self.config.pumps:
            tuya_ip = self._resolve_tuya_ip(pump.tuya.device_id, pump.tuya.ip)
            tuya_local = None
            if pump.tuya.device_id and pump.tuya.local_key and tuya_ip and (
                use_local or use_cloud
            ):
                tuya_local = TuyaLocalDevice(
                    pump.name,
                    pump.tuya.device_id,
                    tuya_ip,
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
                meross_uuids = {
                    p.meross.device_uuid.strip()
                    for p in self.config.pumps
                    if p.meross.device_uuid.strip()
                }
                if meross_uuids:
                    enrolled = await self.meross_session.wait_for_devices(meross_uuids)
                    missing = sorted(uuid for uuid, ok in enrolled.items() if not ok)
                    if missing:
                        logger.warning(
                            "meross devices not enrolled after startup: %s",
                            ", ".join(f"{uuid[:8]}…" for uuid in missing),
                        )
            except Exception:
                logger.exception("failed to initialize Meross cloud session")
        self._ensure_pump_rows()
        self._restore_manual_revert_schedules()
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
        for name in list(self._manual_revert_tasks):
            self._cancel_manual_revert_task(name)
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
                        session.add(
                            ForecastHistoryRow(
                                fetched_at=now,
                                provider=name,
                                hour_ts=f.hour_ts,
                                pop_pct=f.pop_pct,
                                rain_mm=f.rain_mm,
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

    def update_display_units(self, units: str) -> str:
        if units not in ("metric", "imperial"):
            raise ValueError("units must be metric or imperial")
        self.config.display = DisplayConfig(units=units)  # type: ignore[arg-type]
        save_display(self.config_path, self.config.display)
        return units

    def get_command_verify_settings(self) -> dict[str, Any]:
        return {
            "command_verify_delay_seconds": self.config.devices.command_verify_delay_seconds,
            "command_verify_max_attempts": self.config.devices.command_verify_max_attempts,
        }

    def get_notification_settings(self) -> dict[str, Any]:
        smtp = self.config.notifications.smtp
        conn = self.gmail_client.connection()
        return {
            "admin_email": self.config.notifications.admin_email,
            "public_base_url": self.config.notifications.public_base_url,
            "google_gmail": {
                "oauth_configured": self.gmail_client.configured,
                "connected": conn is not None,
                "sender_email": conn.email if conn else "",
                "connected_at": conn.connected_at if conn else "",
            },
            "smtp": {
                "enabled": smtp.enabled,
                "host": smtp.host,
                "port": smtp.port,
                "username": smtp.username,
                "from_addr": smtp.from_addr,
                "to_addrs": list(smtp.to_addrs),
                "password_configured": bool(smtp.password),
            },
        }

    def update_notification_settings(
        self,
        *,
        admin_email: str,
        public_base_url: str,
        smtp_enabled: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_from_addr: str,
        smtp_to_addrs: list[str],
    ) -> dict[str, Any]:
        if smtp_port < 1 or smtp_port > 65535:
            raise ValueError("smtp port must be between 1 and 65535")
        base = public_base_url.strip().rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            raise ValueError("public base URL must start with http:// or https://")
        smtp = self.config.notifications.smtp.model_copy(
            update={
                "enabled": smtp_enabled,
                "host": smtp_host.strip(),
                "port": smtp_port,
                "username": smtp_username.strip(),
                "from_addr": smtp_from_addr.strip(),
                "to_addrs": [addr.strip() for addr in smtp_to_addrs if addr.strip()],
            }
        )
        notifications = self.config.notifications.model_copy(
            update={
                "admin_email": admin_email.strip(),
                "public_base_url": base,
                "smtp": smtp,
            }
        )
        save_notifications_settings(self.config_path, notifications=notifications)
        self._reload_config()
        return self.get_notification_settings()

    def disconnect_google_gmail(self) -> dict[str, Any]:
        self.gmail_client.disconnect()
        return self.get_notification_settings()

    async def send_test_notification_email(self) -> dict[str, Any]:
        ok, detail = await self.notifier.send_admin_email(
            "pumpd test notification",
            "This is a test email from pumpd.\n\nIf you received this, email notifications are configured correctly.",
            gmail_client=self.gmail_client,
        )
        return {"success": ok, "message": detail}

    def update_command_verify_settings(
        self,
        *,
        command_verify_delay_seconds: float,
    ) -> dict[str, Any]:
        if command_verify_delay_seconds < 1 or command_verify_delay_seconds > 300:
            raise ValueError("verify delay must be between 1 and 300 seconds")
        devices = self.config.devices.model_copy(
            update={"command_verify_delay_seconds": command_verify_delay_seconds}
        )
        save_command_verify_settings(
            self.config_path,
            devices=devices,
        )
        self._reload_config()
        return self.get_command_verify_settings()

    def get_runtime_settings(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        return runtime_settings_view(self.config, groups)

    def update_runtime_settings(
        self,
        *,
        system_max_runtime_minutes: int,
        device_overrides: dict[str, int | None],
        pump_overrides: dict[str, int | None],
    ) -> dict[str, Any]:
        if system_max_runtime_minutes < 5 or system_max_runtime_minutes > 24 * 60:
            raise ValueError("system max runtime must be between 5 and 1440 minutes")

        safety = self.config.safety.model_copy(
            update={"max_continuous_runtime_minutes": system_max_runtime_minutes}
        )

        device_runtime: list[DeviceRuntimeOverride] = []
        for key, minutes in device_overrides.items():
            if ":" not in key:
                continue
            if minutes is None:
                continue
            backend, device_id = key.split(":", 1)
            if minutes < 5 or minutes > 24 * 60:
                raise ValueError(f"device max runtime for {key} must be between 5 and 1440 minutes")
            device_runtime.append(
                DeviceRuntimeOverride(
                    device_backend=backend,
                    device_id=device_id,
                    max_runtime_minutes=minutes,
                )
            )

        pump_runtime: dict[str, int | None] = {}
        for name, minutes in pump_overrides.items():
            if minutes is None:
                pump_runtime[name] = None
            else:
                if minutes < 5 or minutes > 24 * 60:
                    raise ValueError(
                        f"switch max runtime for {name} must be between 5 and 1440 minutes"
                    )
                pump_runtime[name] = minutes

        save_runtime_settings(
            self.config_path,
            safety=safety,
            device_runtime=device_runtime,
            pump_runtime=pump_runtime,
        )
        self._reload_config()
        groups = group_pump_cards(pump_cards_from_config(self.config), config=self.config)
        return runtime_settings_view(self.config, groups)

    def get_display_name_settings(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        return display_name_settings_view(self.config, groups)

    def get_device_display_order_settings(
        self,
        groups: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        return device_order_settings_view(self.config, groups)

    def update_device_display_order(self, order_keys: list[str]) -> list[dict[str, str]]:
        groups = group_pump_cards(
            pump_cards_from_config(self.config),
            config=self.config,
        )
        known_keys: set[str] = set()
        for group in groups:
            pumps = group.get("pumps") or []
            if not pumps:
                continue
            known_keys.add(
                device_label_key(
                    pumps[0].get("device_backend") or "",
                    pumps[0].get("device_id") or "",
                )
            )

        rows: list[DeviceDisplayOrderEntry] = []
        seen: set[str] = set()
        for key in order_keys:
            if ":" not in key or key not in known_keys or key in seen:
                continue
            backend, device_id = key.split(":", 1)
            if not backend or not device_id:
                continue
            rows.append(
                DeviceDisplayOrderEntry(
                    device_backend=backend,
                    device_id=device_id,
                )
            )
            seen.add(key)

        for key in sorted(known_keys - seen):
            backend, device_id = key.split(":", 1)
            rows.append(
                DeviceDisplayOrderEntry(
                    device_backend=backend,
                    device_id=device_id,
                )
            )

        save_device_display_order(self.config_path, rows)
        self._reload_config()
        groups = group_pump_cards(
            pump_cards_from_config(self.config),
            config=self.config,
        )
        return device_order_settings_view(self.config, groups)

    async def update_display_names(
        self,
        *,
        device_labels: dict[str, str],
        switch_labels: dict[str, str],
        propagate_cloud: bool = True,
    ) -> dict[str, Any]:
        pump_labels: dict[str, str] = {}
        for name, label in switch_labels.items():
            if not any(p.name == name for p in self.config.pumps):
                raise ValueError(f"unknown pump: {name}")
            pump_labels[name] = (label or "").strip()

        label_rows: list[DeviceLabelOverride] = []
        seen_devices: set[str] = set()
        for key, label in device_labels.items():
            if ":" not in key:
                continue
            backend, device_id = key.split(":", 1)
            if not backend or not device_id:
                continue
            cleaned = (label or "").strip()
            seen_devices.add(key)
            if cleaned:
                label_rows.append(
                    DeviceLabelOverride(
                        device_backend=backend,
                        device_id=device_id,
                        label=cleaned,
                    )
                )

        save_display_names(
            self.config_path,
            pump_labels=pump_labels,
            device_labels=label_rows,
        )
        self._reload_config()

        cloud_results: list[dict[str, Any]] = []
        if not propagate_cloud:
            return {"saved": True, "cloud": cloud_results}

        pumps_by_device: dict[str, list[PumpConfig]] = {}
        for pump in self.config.pumps:
            if pump.meross.device_uuid:
                key = device_label_key("meross", pump.meross.device_uuid)
            elif pump.tuya.device_id:
                key = device_label_key("tuya", pump.tuya.device_id)
            elif pump.smartthings.device_id:
                key = device_label_key("smartthings", pump.smartthings.device_id)
            else:
                continue
            pumps_by_device.setdefault(key, []).append(pump)

        for key, label in device_labels.items():
            cleaned = (label or "").strip()
            if not cleaned or key not in seen_devices:
                continue
            backend, device_id = key.split(":", 1)
            result = await self._propagate_device_label(backend, device_id, cleaned)
            cloud_results.append(
                {
                    "kind": "device",
                    "device_backend": backend,
                    "device_id": device_id,
                    **result,
                }
            )

        for name, label in pump_labels.items():
            cleaned = (label or "").strip()
            if not cleaned:
                continue
            pump = next(p for p in self.config.pumps if p.name == name)
            result = await self._propagate_switch_label(pump, cleaned)
            device_key = None
            if pump.meross.device_uuid:
                device_key = device_label_key("meross", pump.meross.device_uuid)
            elif pump.tuya.device_id:
                device_key = device_label_key("tuya", pump.tuya.device_id)
            elif pump.smartthings.device_id:
                device_key = device_label_key("smartthings", pump.smartthings.device_id)
            skip_switch = (
                device_key is not None
                and device_key in device_labels
                and len(pumps_by_device.get(device_key, [])) == 1
                and device_labels.get(device_key, "").strip() == cleaned
            )
            if skip_switch:
                continue
            cloud_results.append(
                {
                    "kind": "switch",
                    "pump_name": name,
                    **result,
                }
            )

        return {"saved": True, "cloud": cloud_results}

    async def _propagate_device_label(
        self, backend: str, device_id: str, label: str
    ) -> dict[str, Any]:
        if backend == "tuya" and self.tuya_cloud_client is not None:
            return await rename_tuya_device(self.tuya_cloud_client, device_id, label)
        if backend == "smartthings" and self.smartthings_pat:
            return await rename_smartthings_device(self.smartthings_pat, device_id, label)
        if backend == "meross" and self.meross_session.configured:
            return await rename_meross_device(self.meross_session, device_id, label)
        return {
            "success": False,
            "message": f"cloud rename not available for {backend} (missing credentials or unsupported)",
        }

    async def _propagate_switch_label(self, pump: PumpConfig, label: str) -> dict[str, Any]:
        if pump.tuya.device_id and self.tuya_cloud_client is not None:
            switch_code = pump.tuya.switch_code or "switch_1"
            return await rename_tuya_switch(
                self.tuya_cloud_client,
                pump.tuya.device_id,
                switch_code,
                label,
            )
        if pump.smartthings.device_id and self.smartthings_pat:
            return await rename_smartthings_device(
                self.smartthings_pat,
                pump.smartthings.device_id,
                label,
            )
        if pump.meross.device_uuid and self.meross_session.configured:
            return await rename_meross_switch(
                self.meross_session,
                pump.meross.device_uuid,
                pump.meross.channel,
                label,
            )
        return {
            "success": False,
            "message": "cloud rename not available (missing credentials or unsupported)",
        }

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
        await self.refresh_meross_ui_state(force=True)
        await self.reconcile_devices()
        return saved

    async def clear_all_local_devices(self) -> dict[str, Any]:
        """Remove every pump from local config and SQLite — cloud accounts unchanged."""
        removed_names = [p.name for p in self.config.pumps]
        clear_local_devices(self.config_path)
        self._reload_config()

        with self.session_factory() as session:
            session.execute(delete(PumpStateRow))
            session.execute(delete(HardwareHealthRow))
            session.commit()

        for name in list(self._manual_revert_tasks):
            self._cancel_manual_revert_task(name)

        self._online_probe_cache = None
        self._online_probe_cache_at = 0.0
        self._tuya_ip_cache.clear()
        self._build_devices()

        self._log_event(
            None,
            "devices_clear",
            f"cleared {len(removed_names)} pump(s) from local config and database",
            details={"removed": removed_names},
        )
        return {"removed_count": len(removed_names), "removed": removed_names}

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
        self,
        *,
        force: bool = False,
        cache_ttl: float | None = None,
        names: list[str] | None = None,
        use_cache_only: bool = False,
    ) -> dict[str, dict[str, str]]:
        """Probe configured pumps by reading live switch state."""
        now = time.monotonic()
        ttl = self._online_probe_cache_ttl if cache_ttl is None else cache_ttl
        all_names = [p.name for p in self.config.pumps]

        if use_cache_only:
            if self._online_probe_cache is not None:
                return self._online_probe_cache
            return {
                name: {"status": "unknown", "detail": "not probed yet"}
                for name in all_names
            }

        if names is None:
            if (
                not force
                and self._online_probe_cache is not None
                and now - self._online_probe_cache_at < ttl
            ):
                return self._online_probe_cache
            probe_names = all_names
        else:
            probe_names = [name for name in names if name in all_names]
            if not probe_names:
                return self._online_probe_cache or {}
            if (
                not force
                and self._online_probe_cache is not None
                and now - self._online_probe_cache_at < ttl
            ):
                return self._online_probe_cache

        if force:
            self.meross_session.clear_togglex_cache()
            if self.meross_session.configured:
                try:
                    await self.meross_session.online_status_map(refresh=True)
                except Exception:
                    logger.debug("meross online map refresh before probe failed", exc_info=True)

        if self.meross_session.configured and any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            if not self.meross_session.started:
                try:
                    await asyncio.wait_for(self.meross_session.startup(), timeout=15.0)
                except Exception:
                    logger.debug("meross startup before online probe failed", exc_info=True)

        await self._refresh_tuya_ip_cache()

        pump_by_name = {p.name: p for p in self.config.pumps}
        sem = asyncio.Semaphore(4)

        async def probe_pump(name: str) -> tuple[str, dict[str, str]]:
            pump = pump_by_name[name]
            device = self.devices.get(name)
            if pump is None or device is None or not device.has_control_path():
                return name, {
                    "status": "unconfigured",
                    "detail": "missing credentials",
                }

            local_probe = device.tuya or self._tuya_local_probe_device(pump)
            async with sem:
                result = await device.probe_connectivity(local_device=local_probe, force=force)
            return name, result

        keyed = await asyncio.gather(*(probe_pump(name) for name in probe_names))
        probed = dict(keyed)
        if names is None:
            cached = probed
        else:
            cached = dict(self._online_probe_cache or {})
            cached.update(probed)
        self._online_probe_cache = cached
        self._online_probe_cache_at = now
        return cached

    async def refresh_meross_ui_state(self, *, force: bool = False) -> dict[str, Any]:
        """Pull Meross names + live switch state for the UI (60s cache unless forced)."""
        if not self.meross_session.configured or not any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            return {"refreshed": False, "cached": False}

        now = time.monotonic()
        if (
            not force
            and self._meross_ui_cache_at > 0
            and now - self._meross_ui_cache_at < self._meross_ui_cache_ttl
        ):
            return {"refreshed": False, "cached": True}

        name_result = await self.sync_meross_display_names_from_cloud()
        online_map = await self.probe_pumps_online(force=True)
        self._meross_ui_cache_at = now
        return {
            "refreshed": True,
            "cached": False,
            "names": name_result,
            "online_map": online_map,
        }

    async def sync_meross_display_names_from_cloud(self) -> dict[str, Any]:
        """Pull Meross cloud device/outlet names into local config when they differ."""
        if not self.meross_session.configured or not any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            return {"updated": False, "pump_labels": {}, "device_labels": {}}

        try:
            if not self.meross_session.started:
                await self.meross_session.startup()
            cloud_devices = await self.meross_session.list_cloud_devices()
        except Exception as exc:
            logger.warning("meross display name sync failed: %s", exc)
            return {"updated": False, "error": str(exc)}

        cloud = collect_meross_cloud_names(cloud_devices, self.config.pumps)
        pump_updates, device_updates = diff_meross_cloud_names(self.config, cloud)
        if not pump_updates and not device_updates:
            return {"updated": False, "pump_labels": {}, "device_labels": {}}

        save_display_names(
            self.config_path,
            pump_labels=pump_updates,
            device_labels=merge_device_label_rows(self.config.device_labels, device_updates),
        )
        self._reload_config()
        logger.info(
            "synced Meross display names from cloud (%d switch, %d device)",
            len(pump_updates),
            len(device_updates),
        )
        return {
            "updated": True,
            "pump_labels": pump_updates,
            "device_labels": device_updates,
        }

    async def refresh_all_pump_online_status(self) -> dict[str, dict[str, str]]:
        """Force a live connectivity check for every configured pump."""
        online_map: dict[str, dict[str, str]] | None = None
        if self.meross_session.configured and any(
            p.meross.device_uuid for p in self.config.pumps
        ):
            result = await self.refresh_meross_ui_state(force=True)
            online_map = result.get("online_map")
        if not online_map:
            online_map = await self.probe_pumps_online(force=True)
        self._sync_probe_switch_states(online_map)
        return online_map

    def _sync_probe_switch_states(self, online_map: dict[str, dict[str, str]]) -> None:
        """Persist live switch ON/OFF from a forced cloud probe into pump state."""
        for name, entry in online_map.items():
            live_on = parse_probe_switch_state(entry.get("detail", ""))
            if live_on is None:
                continue
            with self.session_factory() as session:
                row = session.get(PumpStateRow, name)
                if row is None or row.device_on == live_on:
                    continue
                row.device_on = live_on
                session.commit()

    async def recover_offline_pump_status(self) -> dict[str, dict[str, str]]:
        """Re-probe pumps currently marked offline or unknown."""
        all_names = [p.name for p in self.config.pumps]
        if not all_names:
            return {}

        current = self._online_probe_cache
        if current is None:
            return await self.probe_pumps_online(force=True)

        offline_names = [
            name
            for name in all_names
            if current.get(name, {}).get("status") in ("offline", "unknown")
        ]
        if not offline_names:
            return current
        return await self.probe_pumps_online(force=True, names=offline_names)

    def _sync_live_device_on(self, name: str, live_on: bool) -> None:
        """Update DB device_on when live probe differs (auto mode only)."""
        with self.session_factory() as session:
            row = session.get(PumpStateRow, name)
            if not row or row.mode != "auto" or row.device_on == live_on:
                return
            row.device_on = live_on
            session.commit()

    async def get_pump_cards(self, *, refresh_cloud: bool = False) -> list[dict[str, Any]]:
        """Pump rows enriched with live online status for dashboard cards."""
        if refresh_cloud:
            await self.refresh_meross_ui_state(force=False)
        with self.session_factory() as session:
            rows = session.scalars(select(PumpStateRow)).all()
            state_by_name = {row.name: row for row in rows}
        online_map = await self.probe_pumps_online(use_cache_only=True)
        hw_map = {h["component_id"]: h for h in self.hardware.status_summary()}
        device_labels = device_labels_map(self.config)
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
            device_backend = (
                "meross"
                if cfg.meross.device_uuid
                else "tuya"
                if cfg.tuya.device_id
                else "smartthings"
                if cfg.smartthings.device_id
                else ""
            )
            device_key = (
                device_label_key(device_backend, device_id)
                if device_id and device_backend
                else ""
            )
            manual_ctx = parse_manual_context(row.manual_context_json) if row else None
            manual_revert_at = _as_utc(row.manual_revert_at) if row and row.manual_revert_at else None
            mode = row.mode if row else "auto"
            device_on = row.device_on if row else False
            live_on = parse_probe_switch_state(online.get("detail", ""))
            if live_on is not None:
                if mode == "auto" and live_on != device_on:
                    self._sync_live_device_on(cfg.name, live_on)
                device_on = live_on
            cards.append(
                {
                    "name": cfg.name,
                    "display_label": pump_display_label(cfg),
                    "device_label": device_labels.get(device_key, ""),
                    "enabled": cfg.enabled,
                    "phase": row.phase if row else "idle",
                    "mode": mode,
                    "device_on": device_on,
                    "runtime_today_min": row.runtime_today_min if row else 0,
                    "runtime_continuous_min": row.runtime_continuous_min if row else 0,
                    "safety_override_approved": row.safety_override_approved if row else False,
                    "manual_revert_at_local": (
                        format_local(manual_revert_at, self.config.timezone)
                        if manual_revert_at
                        else None
                    ),
                    "manual_revert_kind": manual_ctx.revert_kind if manual_ctx else None,
                    "manual_default_minutes": self.config.rules.manual_revert_minutes,
                    "online_status": online["status"],
                    "online_detail": online.get("detail", ""),
                    "hardware_status": hw["status"] if hw else None,
                    "switch_code": switch_code,
                    "device_id": device_id,
                    "device_backend": device_backend,
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
            manual_context=parse_manual_context(row.manual_context_json),
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
        row.manual_context_json = (
            dump_manual_context(phase.manual_context) if phase.manual_context else None
        )
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
                max_runtime_by_pump=max_runtime_by_pump(self.config),
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

        device_on_changes: list[tuple[str, bool, bool]] = []
        with self.session_factory() as session:
            rows = {r.name: r for r in session.scalars(select(PumpStateRow)).all()}
            for phase in result.pumps:
                state_row = rows.get(phase.name)
                if state_row is None:
                    continue
                previous_on = state_row.device_on
                self._apply_phase_to_row(state_row, phase)
                if previous_on != phase.device_on:
                    device_on_changes.append((phase.name, previous_on, phase.device_on))
                if phase.device_on:
                    state_row.runtime_continuous_min += self.config.rules.evaluate_minutes
                    state_row.runtime_today_min += self.config.rules.evaluate_minutes
                else:
                    state_row.runtime_continuous_min = 0
                self._update_runtime_today(state_row)
            session.commit()

        self._extend_until_auto_manual_reverts(now)

        for name, previous_on, new_on in device_on_changes:
            self._log_event(
                name,
                "device_on_change",
                f"device_on={new_on}",
                details={"previous": previous_on},
            )

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
        if cmd.action not in ("turn_on", "turn_off"):
            return
        res = await self._command_with_cloud_verify(
            cmd.pump_name,
            turn_on=cmd.action == "turn_on",
            reason=cmd.reason,
            event_type=cmd.action,
            notify_on_success=cmd.notify,
        )
        if not res.success:
            verify_mismatch = "verify" in res.message.lower() if res.message else False
            self.hardware.record_pump_failure(
                cmd.pump_name, res.message, verify_mismatch=verify_mismatch
            )

    def _command_verify_delay(self, adapter: str | None) -> float:
        base = self.config.devices.command_verify_delay_seconds
        if adapter == "meross_cloud":
            return min(base, 3.0)
        return base

    async def _command_with_cloud_verify(
        self,
        pump_name: str,
        *,
        turn_on: bool,
        reason: str,
        event_type: str,
        notify_on_success: bool = False,
        lock_held: bool = False,
    ) -> CommandResult:
        device = self.devices.get(pump_name)
        if device is None:
            return CommandResult(success=False, adapter="composite", message="device not configured")

        async def _run() -> CommandResult:
            want = DeviceState.ON if turn_on else DeviceState.OFF
            action_label = "turn_on" if turn_on else "turn_off"
            max_attempts = self.config.devices.command_verify_max_attempts
            timeout = self.config.api.device_command_timeout_seconds
            command = device.turn_on if turn_on else device.turn_off
            last_message = ""
            last_adapter = "composite"
            timed_out = False

            for attempt in range(1, max_attempts + 1):
                send_timed_out = False
                try:
                    send_result = await asyncio.wait_for(command(verify=False), timeout=timeout)
                except TimeoutError:
                    send_timed_out = True
                    timed_out = True
                    send_result = CommandResult(
                        success=False,
                        adapter="composite",
                        message="command timed out",
                        timed_out=True,
                    )

                if send_result.adapter:
                    last_adapter = send_result.adapter

                if send_result.success or send_timed_out:
                    delay = self._command_verify_delay(send_result.adapter or last_adapter)
                    await asyncio.sleep(delay)
                    state, cloud_adapter = await device.read_cloud_state()
                    if state == want:
                        success_message = send_result.message
                        if send_timed_out:
                            success_message = (
                                "command confirmed via cloud status (send timed out)"
                            )
                        self._log_event(
                            pump_name,
                            event_type,
                            reason,
                            details={
                                "success": True,
                                "attempt": attempt,
                                "adapter": send_result.adapter,
                                "verified_via": cloud_adapter,
                                "message": success_message,
                                "send_timed_out": send_timed_out,
                            },
                        )
                        self.hardware.record_pump_success(pump_name)
                        if notify_on_success:
                            await self.notifier.send(f"pumpd {pump_name}", reason)
                        return CommandResult(
                            success=True,
                            adapter=send_result.adapter,
                            message=success_message,
                            verify_attempts=attempt,
                            retried=attempt > 1,
                        )

                    last_message = (
                        f"verify failed after {delay:.0f}s: wanted {want.value}, "
                        f"cloud read {state.value} via {cloud_adapter or 'unknown'}"
                    )
                    if send_timed_out:
                        last_message = (
                            f"send timed out; verify failed after {delay:.0f}s: "
                            f"wanted {want.value}, cloud read {state.value} "
                            f"via {cloud_adapter or 'unknown'}"
                        )
                    self._log_event(
                        pump_name,
                        "command_verify_retry",
                        last_message,
                        details={
                            "attempt": attempt,
                            "wanted": want.value,
                            "actual": state.value,
                            "cloud_adapter": cloud_adapter,
                            "action": action_label,
                            "send_timed_out": send_timed_out,
                        },
                    )
                    if attempt >= max_attempts:
                        break
                    continue

                last_message = send_result.message or "command failed"
                self._log_event(
                    pump_name,
                    event_type,
                    reason,
                    details={
                        "success": False,
                        "attempt": attempt,
                        "phase": "send",
                        "adapter": send_result.adapter,
                        "message": last_message,
                    },
                )
                if attempt >= max_attempts:
                    break

            self._log_event(
                pump_name,
                event_type,
                reason,
                details={
                    "success": False,
                    "attempts": max_attempts,
                    "adapter": last_adapter,
                    "message": last_message,
                },
            )
            body = (
                f"Pump: {pump_name}\n"
                f"Action: {action_label}\n"
                f"Reason: {reason}\n"
                f"Attempts: {max_attempts}\n"
                f"Last error: {last_message}\n"
            )
            await self.notifier.send_admin_email(
                f"pumpd alert: {pump_name} {action_label} failed",
                body,
                gmail_client=self.gmail_client,
            )
            return CommandResult(
                success=False,
                adapter=last_adapter,
                message=last_message,
                timed_out=timed_out,
                retried=max_attempts > 1,
                verify_attempts=max_attempts,
            )

        if lock_held:
            return await _run()
        async with self._pump_lock(pump_name):
            return await _run()

    async def _manual_device_command(
        self, name: str, *, turn_on: bool, lock_held: bool = False
    ) -> CommandResult:
        action_label = "turn_on" if turn_on else "turn_off"
        return await self._command_with_cloud_verify(
            name,
            turn_on=turn_on,
            reason=f"manual {action_label}",
            event_type=action_label,
            lock_held=lock_held,
        )

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

    def _cancel_manual_revert_task(self, name: str) -> None:
        task = self._manual_revert_tasks.pop(name, None)
        if task and not task.done():
            task.cancel()

    def _schedule_manual_revert(self, name: str, revert_at: datetime) -> None:
        self._cancel_manual_revert_task(name)

        async def _fire() -> None:
            try:
                delay = (revert_at - datetime.now(UTC)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                async with self._pump_lock(name):
                    row = self._get_pump_row(name)
                    if row is None:
                        return
                    if row.mode not in ("manual_on", "manual_off") or row.manual_revert_at is None:
                        return
                    if _as_utc(row.manual_revert_at) > datetime.now(UTC):
                        return
                    previous_mode = row.mode
                    await self.run_evaluation()
                    row = self._get_pump_row(name)
                    if row and row.mode == "auto" and previous_mode in ("manual_on", "manual_off"):
                        self._log_event(
                            name,
                            "mode_change",
                            "manual mode expired; reverted to auto",
                            details={"mode": "auto", "previous_mode": previous_mode},
                        )
            except asyncio.CancelledError:
                pass
            finally:
                current = self._manual_revert_tasks.get(name)
                if current is asyncio.current_task():
                    self._manual_revert_tasks.pop(name, None)

        self._manual_revert_tasks[name] = asyncio.create_task(_fire())

    def _manual_mode_set_at(self, session: Session, pump_name: str) -> datetime | None:
        row = session.scalar(
            select(EventRow)
            .where(
                EventRow.pump_name == pump_name,
                EventRow.event_type == "mode_change",
            )
            .order_by(EventRow.ts.desc())
            .limit(1)
        )
        if row is None or not row.details_json:
            return None
        try:
            details = json.loads(row.details_json)
        except json.JSONDecodeError:
            return None
        if details.get("mode") not in ("manual_on", "manual_off"):
            return None
        return _as_utc(row.ts)

    def _extend_until_auto_manual_reverts(self, now: datetime) -> None:
        evaluate_minutes = self.config.rules.evaluate_minutes
        with self.session_factory() as session:
            rows = session.scalars(select(PumpStateRow)).all()
            for row in rows:
                if row.mode not in ("manual_on", "manual_off"):
                    continue
                ctx = parse_manual_context(row.manual_context_json)
                if ctx is None or ctx.revert_kind != "until_auto":
                    continue
                revert_at = now + timedelta(minutes=max(1, evaluate_minutes))
                row.manual_revert_at = revert_at
                self._schedule_manual_revert(row.name, revert_at)
            session.commit()

    def _restore_manual_revert_schedules(self) -> None:
        now = datetime.now(UTC)
        max_revert = timedelta(minutes=self.config.rules.manual_revert_minutes)
        legacy_window = timedelta(hours=4)
        schedules: list[tuple[str, datetime]] = []
        with self.session_factory() as session:
            rows = session.scalars(select(PumpStateRow)).all()
            for row in rows:
                if row.mode not in ("manual_on", "manual_off") or not row.manual_revert_at:
                    continue
                ctx = parse_manual_context(row.manual_context_json)
                revert_at = _as_utc(row.manual_revert_at)
                if ctx and ctx.revert_kind == "until_auto":
                    if now >= revert_at:
                        revert_at = now + timedelta(minutes=max(1, self.config.rules.evaluate_minutes))
                    row.manual_revert_at = revert_at
                    schedules.append((row.name, revert_at))
                    continue
                if ctx and ctx.revert_kind == "duration":
                    if now >= revert_at:
                        revert_at = now
                    row.manual_revert_at = revert_at
                    schedules.append((row.name, revert_at))
                    continue
                manual_set_at = self._manual_mode_set_at(session, row.name)
                if manual_set_at is None and revert_at - now > max_revert:
                    manual_set_at = revert_at - legacy_window
                if manual_set_at is not None:
                    due_at = manual_set_at + max_revert
                    revert_at = now if now >= due_at else due_at
                elif now >= revert_at:
                    revert_at = now
                row.manual_revert_at = revert_at
                schedules.append((row.name, revert_at))
            session.commit()
        for name, revert_at in schedules:
            self._schedule_manual_revert(name, revert_at)

    def _set_pump_device_on(self, name: str, *, turn_on: bool) -> None:
        with self.session_factory() as session:
            row = session.get(PumpStateRow, name)
            if row is None:
                return
            row.device_on = turn_on
            row.updated_at = datetime.now(UTC)
            session.commit()

    async def _manual_env_snapshot(self) -> ManualEnvSnapshot:
        rain = await self.get_rain_state()
        now = datetime.now(UTC)
        with self.session_factory() as session:
            window = self._load_forecast_window(session, self.config.rules.lookahead_hours + 2)
        preempt, _ = should_preemptive_start(
            window,
            now,
            self.config.rules.lookahead_hours,
            self.config.rules.precip_probability_threshold,
            self.config.rules.precip_amount_threshold_mm,
        )
        is_raining = rain.is_raining or rain.water_present is True
        return ManualEnvSnapshot(
            is_raining=is_raining,
            preempt=preempt,
            water_present=rain.water_present,
        )

    async def set_pump_mode(
        self,
        name: str,
        mode: str,
        *,
        approve_safety_override: bool = False,
        manual_hours: int = 0,
        manual_minutes: int = 0,
        manual_duration_minutes: int | None = None,
        manual_until_auto: bool = False,
        refresh_cloud_after: bool = True,
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
        revert_kind: ManualRevertKind = "until_auto" if manual_until_auto else "duration"
        duration_minutes = resolve_manual_duration_minutes(
            hours=manual_hours,
            minutes=manual_minutes,
            total_minutes=manual_duration_minutes,
            default_minutes=self.config.rules.manual_revert_minutes,
        )
        revert = (
            compute_manual_revert_at(
                now=now,
                revert_kind=revert_kind,
                duration_minutes=duration_minutes,
                evaluate_minutes=self.config.rules.evaluate_minutes,
            )
            if mode != "auto"
            else None
        )
        manual_context: ManualContext | None = None
        device_on_before: bool | None = None
        if mode in ("manual_on", "manual_off"):
            with self.session_factory() as session:
                row = session.get(PumpStateRow, name)
                if not row:
                    return None, None
                device_on_before = row.device_on
            env = await self._manual_env_snapshot()
            manual_context = ManualContext(
                device_on_before=device_on_before,
                revert_kind=revert_kind,
                env=env,
            )
        async with self._pump_lock(name):
            with self.session_factory() as session:
                row = session.get(PumpStateRow, name)
                if not row:
                    return None, None
                previous_mode = row.mode
                row.mode = mode
                row.manual_revert_at = revert
                row.manual_context_json = (
                    dump_manual_context(manual_context) if manual_context else None
                )
                row.safety_override_approved = (
                    approve_safety_override if mode in ("manual_on", "manual_off") else False
                )
                session.commit()
                session.refresh(row)
            if mode != "auto":
                assert revert is not None
                self._schedule_manual_revert(name, revert)
            else:
                self._cancel_manual_revert_task(name)
            event_details: dict[str, Any] = {
                "mode": mode,
                "previous_mode": previous_mode,
            }
            if manual_context is not None:
                event_details.update(
                    {
                        "manual_revert_kind": revert_kind,
                        "manual_duration_minutes": duration_minutes,
                        "device_on_before": device_on_before,
                        "manual_revert_at": revert.isoformat() if revert else None,
                    }
                )
            self._log_event(
                name,
                "mode_change",
                f"mode set to {mode}",
                details=event_details,
            )
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
            cmd_result: CommandResult | None = None
            if mode == "manual_on":
                cmd_result = await self._manual_device_command(name, turn_on=True, lock_held=True)
                if not cmd_result.success:
                    raise DeviceCommandError(cmd_result)
            elif mode == "manual_off":
                cmd_result = await self._manual_device_command(name, turn_on=False, lock_held=True)
                if not cmd_result.success:
                    raise DeviceCommandError(cmd_result)
            if cmd_result is not None and cmd_result.success:
                self._set_pump_device_on(name, turn_on=(mode == "manual_on"))
            else:
                cmd_result = None
            if refresh_cloud_after and cmd_result is not None and cmd_result.success:
                await self.refresh_meross_ui_state(force=True)
            await self.run_evaluation()
            row = self._get_pump_row(name)
            return row, cmd_result

    def pumps_for_device(self, device_backend: str, device_id: str) -> list[PumpConfig]:
        backend = device_backend.strip().lower()
        device_id = device_id.strip()
        pumps: list[PumpConfig] = []
        for pump in self.config.pumps:
            if not pump.enabled:
                continue
            pump_backend = (
                "meross"
                if pump.meross.device_uuid.strip()
                else "tuya"
                if pump.tuya.device_id.strip()
                else ""
            )
            pump_device_id = pump.meross.device_uuid.strip() or pump.tuya.device_id.strip()
            if pump_backend == backend and pump_device_id == device_id:
                pumps.append(pump)
        return sort_pumps_by_switch(pumps)

    async def set_device_group_mode(
        self,
        device_backend: str,
        device_id: str,
        mode: str,
        *,
        approve_safety_override: bool = False,
        manual_hours: int = 0,
        manual_minutes: int = 0,
        manual_duration_minutes: int | None = None,
        manual_until_auto: bool = False,
    ) -> dict[str, Any]:
        if mode not in ("manual_on", "manual_off"):
            raise ValueError("mode must be manual_on or manual_off")
        pumps = self.pumps_for_device(device_backend, device_id)
        if len(pumps) < 2:
            raise ValueError("device group not found or has fewer than two switches")

        if (
            self._safety_active()
            and not approve_safety_override
        ):
            raise ValueError(
                "safety hard-stop active; set approve_safety_override=true to override"
            )

        stagger = self.config.devices.switch_stagger_seconds
        results: list[dict[str, Any]] = []
        failures: list[str] = []

        for index, pump in enumerate(pumps):
            if mode == "manual_on" and index > 0 and stagger > 0:
                await asyncio.sleep(stagger)
            try:
                row, cmd_result = await self.set_pump_mode(
                    pump.name,
                    mode,
                    approve_safety_override=approve_safety_override,
                    manual_hours=manual_hours,
                    manual_minutes=manual_minutes,
                    manual_duration_minutes=manual_duration_minutes,
                    manual_until_auto=manual_until_auto,
                    refresh_cloud_after=False,
                )
            except DeviceCommandError as exc:
                failures.append(f"{pump.name}: {exc.result.message}")
                results.append(
                    {
                        "name": pump.name,
                        "success": False,
                        "message": exc.result.message,
                    }
                )
                continue
            if row is None:
                failures.append(f"{pump.name}: pump not found")
                continue
            entry: dict[str, Any] = {"name": row.name, "mode": row.mode, "success": True}
            if cmd_result is not None:
                entry["command"] = {
                    "success": cmd_result.success,
                    "adapter": cmd_result.adapter,
                    "message": cmd_result.message,
                    "timed_out": cmd_result.timed_out,
                    "retried": cmd_result.retried,
                }
            results.append(entry)

        if failures:
            raise DeviceCommandError(
                CommandResult(
                    success=False,
                    adapter="composite",
                    message="; ".join(failures),
                )
            )
        await self.refresh_meross_ui_state(force=True)
        return {
            "device_backend": device_backend,
            "device_id": device_id,
            "mode": mode,
            "pumps": results,
        }

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

    def format_events_for_ui(self, events: list[EventRow]) -> list[dict[str, Any]]:
        """Format event rows with ts_local in configured timezone (not UTC)."""
        tz = ZoneInfo(self.config.timezone)
        formatted: list[dict[str, Any]] = []
        for row in events:
            ts_utc = _as_utc(row.ts)
            formatted.append(
                {
                    "id": row.id,
                    "ts": ts_utc.isoformat(),
                    "ts_local": ts_utc.astimezone(tz).strftime("%m-%d %H:%M:%S"),
                    "pump_name": row.pump_name,
                    "event_type": row.event_type,
                    "reason": row.reason,
                }
            )
        return formatted

    def get_control_history(
        self,
        *,
        limit: int = 100,
        pump: str | None = None,
        since: str | None = None,
        hours: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            q = (
                select(EventRow)
                .where(EventRow.event_type.in_(CONTROL_EVENT_TYPES))
                .order_by(EventRow.ts.desc())
            )
            if pump:
                q = q.where(EventRow.pump_name == pump)
            if since:
                since_dt = datetime.fromisoformat(since)
                q = q.where(EventRow.ts >= since_dt)
            elif hours is not None:
                q = q.where(EventRow.ts >= datetime.now(UTC) - timedelta(hours=hours))
            q = q.limit(limit)
            rows = list(session.scalars(q).all())
        tz = ZoneInfo(self.config.timezone)
        return [
            {
                **self._format_control_event(row),
                "ts_local": _as_utc(row.ts).astimezone(tz).strftime("%m-%d %H:%M:%S"),
            }
            for row in rows
        ]

    def get_forecast_history(
        self,
        *,
        limit: int = 200,
        provider: str | None = None,
        hours: int = 48,
    ) -> list[dict[str, Any]]:
        since = datetime.now(UTC) - timedelta(hours=hours)
        tz = ZoneInfo(self.config.timezone)
        with self.session_factory() as session:
            q = (
                select(ForecastHistoryRow)
                .where(ForecastHistoryRow.fetched_at >= since)
                .order_by(
                    ForecastHistoryRow.fetched_at.desc(),
                    ForecastHistoryRow.hour_ts,
                )
            )
            if provider:
                q = q.where(ForecastHistoryRow.provider == provider)
            q = q.limit(limit)
            rows = list(session.scalars(q).all())
        return [
            {
                "fetched_at": _as_utc(row.fetched_at).isoformat(),
                "fetched_local": _as_utc(row.fetched_at).astimezone(tz).strftime("%m-%d %H:%M"),
                "provider": row.provider,
                "hour_ts": _as_utc(row.hour_ts).isoformat(),
                "hour_local": _as_utc(row.hour_ts).astimezone(tz).strftime("%m-%d %H:%M"),
                "pop_pct": row.pop_pct,
                "rain_mm": row.rain_mm,
            }
            for row in rows
        ]

    async def get_history_timeline(
        self,
        *,
        hours: int = 168,
        idle_gap_minutes: int = 60,
    ) -> dict[str, Any]:
        range_end = datetime.now(UTC)
        range_start = range_end - timedelta(hours=hours)
        event_lookback = range_start - timedelta(days=7)
        with self.session_factory() as session:
            events = list(
                session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.event_type.in_(
                            ("turn_on", "turn_off", "reconcile", "device_on_change")
                        )
                    )
                    .where(EventRow.ts >= event_lookback)
                    .where(EventRow.ts <= range_end)
                    .order_by(EventRow.ts.asc())
                ).all()
            )
            state_rows = {
                row.name: row for row in session.scalars(select(PumpStateRow)).all()
            }
        pump_cards = await self.get_pump_cards()
        current_state = {card["name"]: bool(card.get("device_on")) for card in pump_cards}
        updated_at_by_pump = {
            name: _as_utc(row.updated_at) if row.updated_at else None
            for name, row in state_rows.items()
        }
        timeline = build_history_timeline(
            events,
            pump_cards,
            range_start=range_start,
            range_end=range_end,
            idle_gap_minutes=idle_gap_minutes,
            current_state=current_state,
            updated_at_by_pump=updated_at_by_pump,
            config=self.config,
        )
        tz = ZoneInfo(self.config.timezone)
        timeline["timezone"] = self.config.timezone
        timeline["range_start_local"] = range_start.astimezone(tz).strftime("%m-%d %H:%M")
        timeline["range_end_local"] = range_end.astimezone(tz).strftime("%m-%d %H:%M")
        for marker in timeline.get("markers", []):
            marker_ts = _as_utc(datetime.fromisoformat(marker["ts"]))
            marker["ts_local"] = marker_ts.astimezone(tz).strftime("%m-%d %H:%M:%S")
        return timeline

    @staticmethod
    def _format_control_event(row: EventRow) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if row.details_json:
            try:
                details = json.loads(row.details_json)
            except json.JSONDecodeError:
                details = {}
        action = row.event_type
        if action == "mode_change":
            label = str(details.get("mode", row.reason))
        elif action == "turn_on":
            label = "ON"
        elif action == "turn_off":
            label = "OFF"
        else:
            label = action
        return {
            "id": row.id,
            "ts": _as_utc(row.ts).isoformat(),
            "pump_name": row.pump_name,
            "event_type": row.event_type,
            "action": label,
            "reason": row.reason,
            "success": details.get("success"),
            "adapter": details.get("adapter"),
            "details": details,
        }

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
            "hardware_health": self.hardware.status_summary(
                timezone=self.config.timezone
            ),
        }
