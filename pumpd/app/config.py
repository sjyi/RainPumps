"""Application configuration loaded from config.yaml and environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocationConfig(BaseModel):
    latitude: float = 0.0
    longitude: float = 0.0
    name: str = ""
    address: str = ""


class WeatherConfig(BaseModel):
    poll_minutes: int = 10
    current_poll_minutes: int = 10
    providers: list[str] = Field(default_factory=lambda: ["accuweather", "open_meteo", "nws"])
    display_provider: str = "accuweather"

    @model_validator(mode="after")
    def _validate_poll_intervals(self) -> WeatherConfig:
        if self.poll_minutes < 5:
            raise ValueError("weather.poll_minutes must be at least 5")
        if self.current_poll_minutes < 5:
            raise ValueError("weather.current_poll_minutes must be at least 5")
        return self


class DutyCycleConfig(BaseModel):
    enabled: bool = False
    on_minutes: int = 10
    off_minutes: int = 20


class RulesConfig(BaseModel):
    evaluate_minutes: int = 10
    precip_probability_threshold: int = 70
    precip_amount_threshold_mm: float = 2.0
    lookahead_hours: int = 2
    post_rain_drain_minutes: int = 30
    sensor_dry_minutes: int = 10
    manual_revert_minutes: int = 5
    duty_cycle: DutyCycleConfig = Field(default_factory=DutyCycleConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_manual_revert_hours(cls, data: Any) -> Any:
        if isinstance(data, dict) and "manual_revert_hours" in data and "manual_revert_minutes" not in data:
            migrated = dict(data)
            migrated["manual_revert_minutes"] = int(migrated.pop("manual_revert_hours")) * 60
            return migrated
        return data


class SafetyConfig(BaseModel):
    max_continuous_runtime_minutes: int = 180
    min_cooldown_minutes: int = 15
    watchdog_stale_forecast_hours: int = 3


class DeviceRuntimeOverride(BaseModel):
    device_backend: str
    device_id: str
    max_runtime_minutes: int | None = None


class DeviceLabelOverride(BaseModel):
    device_backend: str
    device_id: str
    label: str = ""


class DeviceDisplayOrderEntry(BaseModel):
    device_backend: str
    device_id: str


class TuyaConfig(BaseModel):
    device_id: str = ""
    ip: str = ""
    local_key: str = ""
    version: float = 3.4
    switch_code: str = ""  # e.g. switch_1, switch_2 — one outlet on multi-gang plugs


class SmartThingsPumpConfig(BaseModel):
    device_id: str = ""


class MerossConfig(BaseModel):
    device_uuid: str = ""
    channel: int = 0  # 0-based outlet on multi-gang plugs
    switch_code: str = ""  # e.g. switch_1 — display / stagger ordering


class PumpConfig(BaseModel):
    name: str
    label: str = ""  # optional display name for this switch/outlet
    enabled: bool = True
    max_runtime_minutes: int | None = None
    tuya: TuyaConfig = Field(default_factory=TuyaConfig)
    meross: MerossConfig = Field(default_factory=MerossConfig)
    smartthings: SmartThingsPumpConfig = Field(default_factory=SmartThingsPumpConfig)


class NtfyConfig(BaseModel):
    enabled: bool = True
    url: str = "https://ntfy.sh"
    topic: str = ""


class SmtpConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    password: str = ""


class NotificationsConfig(BaseModel):
    admin_email: str = ""
    public_base_url: str = "http://localhost:8080"
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)


class MqttConfig(BaseModel):
    enabled: bool = False
    host: str = "mosquitto"
    port: int = 1883
    topic: str = "sensors/rain"
    min_confidence: float = 0.8


class ApiConfig(BaseModel):
    auth_enabled: bool = False
    api_key: str = ""
    lock_timeout_seconds: float = 30.0
    device_command_timeout_seconds: float = 20.0


class HardwareMonitorConfig(BaseModel):
    enabled: bool = True
    sensor_stale_minutes: int = 15
    pump_failure_threshold: int = 3
    verify_mismatch_threshold: int = 2


class LoggingConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    json_logs: bool = Field(default=False, alias="json")


ControlMode = Literal["local", "cloud", "auto"]


class DevicesConfig(BaseModel):
    """Device control path selection.

    local — Tuya LAN first, Meross cloud, SmartThings fallback.
    cloud — Tuya IoT Cloud, Meross cloud, SmartThings; skips Tuya LAN.
    auto  — Tuya LAN, Tuya cloud, Meross cloud, SmartThings.
    """

    control_mode: ControlMode = "auto"
    switch_stagger_seconds: float = 30.0
    command_verify_delay_seconds: float = 15.0
    command_verify_max_attempts: int = 3
    online_probe_recovery_minutes: int = 5
    # When false (default), Meross uses cloud MQTT only — no local LAN HTTP.
    meross_lan_first: bool = False


class DisplayConfig(BaseModel):
    units: Literal["metric", "imperial"] = "imperial"


class AppConfig(BaseModel):
    timezone: str = "America/New_York"
    location: LocationConfig = Field(default_factory=LocationConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    device_runtime: list[DeviceRuntimeOverride] = Field(default_factory=list)
    device_labels: list[DeviceLabelOverride] = Field(default_factory=list)
    device_display_order: list[DeviceDisplayOrderEntry] = Field(default_factory=list)
    pumps: list[PumpConfig] = Field(default_factory=list)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    hardware_monitor: HardwareMonitorConfig = Field(default_factory=HardwareMonitorConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    devices: DevicesConfig = Field(default_factory=DevicesConfig)
    database_url: str = "sqlite:///./data/pumpd.db"


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    smartthings_pat: str = ""
    api_key: str = ""
    smtp_password: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    tuya_api_key: str = ""
    tuya_api_secret: str = ""
    tuya_api_region: str = "us"
    tuya_api_device_id: str = ""
    meross_email: str = ""
    meross_password: str = ""
    meross_api_base: str = "https://iotx-us.meross.com"
    meross_mfa_code: str = ""
    accuweather_api_key: str = ""


def load_config(path: Path | str = "config.yaml") -> AppConfig:
    """Load YAML config and merge environment overrides."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    env = EnvSettings()
    cfg = AppConfig.model_validate(data)

    if env.api_key:
        cfg.api.api_key = env.api_key
    if env.smtp_password:
        cfg.notifications.smtp.password = env.smtp_password

    for pump in cfg.pumps:
        env_key = f"TUYA_LOCAL_KEY_{pump.name.upper()}"
        import os

        if os.getenv(env_key):
            pump.tuya.local_key = os.environ[env_key]

    return cfg


def save_location(path: Path | str, location: LocationConfig) -> None:
    """Persist location fields to config.yaml."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    loc = data.setdefault("location", {})
    loc["latitude"] = location.latitude
    loc["longitude"] = location.longitude
    loc["name"] = location.name
    loc["address"] = location.address
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_display(path: Path | str, display: DisplayConfig) -> None:
    """Persist display preferences to config.yaml."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    data["display"] = display.model_dump()
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_display_names(
    path: Path | str,
    *,
    pump_labels: dict[str, str],
    device_labels: list[DeviceLabelOverride],
) -> None:
    """Persist pump switch labels and per-device display names."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    pumps = data.get("pumps", [])
    if not isinstance(pumps, list):
        pumps = []
    by_name = {
        p["name"]: p for p in pumps if isinstance(p, dict) and "name" in p
    }
    for name, label in pump_labels.items():
        if name not in by_name:
            continue
        cleaned = (label or "").strip()
        if cleaned:
            by_name[name]["label"] = cleaned
        else:
            by_name[name].pop("label", None)

    ordered_pumps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pump in pumps:
        if not isinstance(pump, dict):
            continue
        name = pump.get("name")
        if not isinstance(name, str) or name not in by_name or name in seen:
            continue
        ordered_pumps.append(by_name[name])
        seen.add(name)
    for name, pump in by_name.items():
        if name not in seen:
            ordered_pumps.append(pump)
    data["pumps"] = ordered_pumps

    existing_device_rows = data.get("device_labels", [])
    if not isinstance(existing_device_rows, list):
        existing_device_rows = []
    device_by_key: dict[str, dict[str, Any]] = {}
    for row in existing_device_rows:
        if not isinstance(row, dict):
            continue
        backend = str(row.get("device_backend") or "").strip()
        device_id = str(row.get("device_id") or "").strip()
        if backend and device_id:
            device_by_key[f"{backend}:{device_id}"] = row

    for row in device_labels:
        key = f"{row.device_backend}:{row.device_id}"
        cleaned = (row.label or "").strip()
        if cleaned:
            device_by_key[key] = row.model_dump(exclude_none=True)
        elif key in device_by_key:
            del device_by_key[key]

    if device_by_key:
        data["device_labels"] = list(device_by_key.values())
    elif "device_labels" in data:
        del data["device_labels"]

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_device_display_order(
    path: Path | str,
    order: list[DeviceDisplayOrderEntry],
) -> None:
    """Persist dashboard device group display order."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    rows = [row.model_dump(exclude_none=True) for row in order]
    if rows:
        data["device_display_order"] = rows
    elif "device_display_order" in data:
        del data["device_display_order"]

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_runtime_settings(
    path: Path | str,
    *,
    safety: SafetyConfig,
    device_runtime: list[DeviceRuntimeOverride],
    pump_runtime: dict[str, int | None],
) -> None:
    """Persist system, device, and per-switch max runtime settings."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    data["safety"] = safety.model_dump()
    rows = [
        row.model_dump(exclude_none=True)
        for row in device_runtime
        if row.max_runtime_minutes is not None
    ]
    if rows:
        data["device_runtime"] = rows
    elif "device_runtime" in data:
        del data["device_runtime"]

    pumps = data.get("pumps", [])
    if not isinstance(pumps, list):
        pumps = []
    by_name = {
        p["name"]: p for p in pumps if isinstance(p, dict) and "name" in p
    }
    for name, minutes in pump_runtime.items():
        if name not in by_name:
            continue
        if minutes is None:
            by_name[name].pop("max_runtime_minutes", None)
        else:
            by_name[name]["max_runtime_minutes"] = minutes
    data["pumps"] = list(by_name.values())

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_command_verify_settings(
    path: Path | str,
    *,
    devices: DevicesConfig,
) -> None:
    """Persist command verification timing settings."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    dev = data.setdefault("devices", {})
    if not isinstance(dev, dict):
        dev = {}
        data["devices"] = dev
    dev["command_verify_delay_seconds"] = devices.command_verify_delay_seconds
    dev["command_verify_max_attempts"] = devices.command_verify_max_attempts

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_notifications_settings(
    path: Path | str,
    *,
    notifications: NotificationsConfig,
) -> None:
    """Persist notification and SMTP settings under notifications: in config.yaml."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    notif = data.setdefault("notifications", {})
    if not isinstance(notif, dict):
        notif = {}
        data["notifications"] = notif

    if notifications.admin_email:
        notif["admin_email"] = notifications.admin_email
    elif "admin_email" in notif:
        del notif["admin_email"]
    if notifications.public_base_url:
        notif["public_base_url"] = notifications.public_base_url

    smtp = notif.setdefault("smtp", {})
    if not isinstance(smtp, dict):
        smtp = {}
        notif["smtp"] = smtp
    smtp_cfg = notifications.smtp
    smtp["enabled"] = smtp_cfg.enabled
    smtp["host"] = smtp_cfg.host
    smtp["port"] = smtp_cfg.port
    smtp["username"] = smtp_cfg.username
    smtp["from_addr"] = smtp_cfg.from_addr
    smtp["to_addrs"] = list(smtp_cfg.to_addrs)

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_pumps(
    path: Path | str,
    pumps: list[PumpConfig],
    *,
    mode: Literal["merge", "replace"] = "merge",
) -> list[PumpConfig]:
    """Persist pump definitions to config.yaml."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    incoming = [p.model_dump() for p in pumps]
    if mode == "replace":
        data["pumps"] = incoming
    else:
        existing = {
            p["name"]: p
            for p in data.get("pumps", [])
            if isinstance(p, dict) and "name" in p
        }
        for pump in incoming:
            existing[pump["name"]] = pump
        data["pumps"] = list(existing.values())

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return [PumpConfig.model_validate(p) for p in data["pumps"]]


def remove_pump(path: Path | str, name: str) -> list[PumpConfig]:
    """Remove a pump from config.yaml by name."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    pumps = data.get("pumps", [])
    if not isinstance(pumps, list):
        pumps = []
    filtered = [p for p in pumps if isinstance(p, dict) and p.get("name") != name]
    if len(filtered) == len(pumps):
        raise KeyError(name)

    data["pumps"] = filtered
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return [PumpConfig.model_validate(p) for p in filtered]


def clear_local_devices(path: Path | str) -> None:
    """Remove all pump/device entries from local config.yaml only."""
    config_path = Path(path)
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    data["pumps"] = []
    data.pop("device_labels", None)
    data.pop("device_display_order", None)
    data.pop("device_runtime", None)

    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


PumpMode = Literal["auto", "manual_on", "manual_off"]
PumpPhaseName = Literal["idle", "pre_rain", "running", "post_rain_drain"]
CommandAction = Literal["turn_on", "turn_off", "no_op"]
