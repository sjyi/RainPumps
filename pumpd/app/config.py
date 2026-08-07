"""Application configuration loaded from config.yaml and environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocationConfig(BaseModel):
    latitude: float = 0.0
    longitude: float = 0.0
    name: str = ""
    address: str = ""


class WeatherConfig(BaseModel):
    poll_minutes: int = 30
    providers: list[str] = Field(default_factory=lambda: ["open_meteo", "nws"])


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
    manual_revert_hours: int = 4
    duty_cycle: DutyCycleConfig = Field(default_factory=DutyCycleConfig)


class SafetyConfig(BaseModel):
    max_continuous_runtime_minutes: int = 60
    min_cooldown_minutes: int = 15
    watchdog_stale_forecast_hours: int = 3


class TuyaConfig(BaseModel):
    device_id: str = ""
    ip: str = ""
    local_key: str = ""
    version: float = 3.4
    switch_code: str = ""  # e.g. switch_1, switch_2 — one outlet on multi-gang plugs


class SmartThingsPumpConfig(BaseModel):
    device_id: str = ""


class PumpConfig(BaseModel):
    name: str
    enabled: bool = True
    tuya: TuyaConfig = Field(default_factory=TuyaConfig)
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
    device_command_timeout_seconds: float = 10.0


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

    local — Tuya LAN first, SmartThings fallback (default when remote LAN unavailable).
    cloud — Tuya IoT Cloud + SmartThings; skips LAN even if configured.
    auto  — LAN, then cloud, then SmartThings.
    """

    control_mode: ControlMode = "auto"
    switch_stagger_seconds: float = 30.0


class AppConfig(BaseModel):
    timezone: str = "America/New_York"
    location: LocationConfig = Field(default_factory=LocationConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
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
    tuya_api_key: str = ""
    tuya_api_secret: str = ""
    tuya_api_region: str = "us"
    tuya_api_device_id: str = ""


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


PumpMode = Literal["auto", "manual_on", "manual_off"]
PumpPhaseName = Literal["idle", "pre_rain", "running", "post_rain_drain"]
CommandAction = Literal["turn_on", "turn_off", "no_op"]
