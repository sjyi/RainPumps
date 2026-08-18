"""Discover and import Tuya / SmartThings pump devices."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from app.config import PumpConfig

logger = logging.getLogger(__name__)

ST_BASE = "https://api.smartthings.com/v1"
SWITCH_CAPABILITIES = {"switch", "outlet", "relaySwitch"}


@dataclass
class DiscoveredDevice:
    source: Literal["smartthings", "tuya_cloud", "tuya_lan", "tuya_json", "meross_cloud"]
    label: str
    tuya_device_id: str = ""
    tuya_ip: str = ""
    tuya_local_key: str = ""
    tuya_version: float = 3.4
    tuya_switch_code: str = ""
    tuya_switch_dp: str = ""
    meross_device_uuid: str = ""
    meross_channel: int = 0
    meross_switch_code: str = ""
    smartthings_device_id: str = ""
    match_key: str = ""  # normalized name for auto-pairing
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        switch_code = self.tuya_switch_code or self.meross_switch_code or "switch_1"
        return {
            "source": self.source,
            "label": self.label,
            "tuya_device_id": self.tuya_device_id,
            "tuya_ip": self.tuya_ip,
            "tuya_local_key": self.tuya_local_key,
            "tuya_version": self.tuya_version,
            "tuya_switch_code": self.tuya_switch_code,
            "tuya_switch_dp": self.tuya_switch_dp,
            "meross_device_uuid": self.meross_device_uuid,
            "meross_channel": self.meross_channel,
            "meross_switch_code": self.meross_switch_code,
            "smartthings_device_id": self.smartthings_device_id,
            "match_key": self.match_key,
            "switch_code": switch_code,
            "configured": bool(
                (self.tuya_device_id and self.tuya_ip and self.tuya_local_key)
                or self.meross_device_uuid
                or self.smartthings_device_id
            ),
        }


def slugify_pump_name(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or "pump"


def normalize_match_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def tuya_switch_codes_from_mapping(mapping: Any) -> list[tuple[str, str]]:
    """Return [(dp_id, switch_code), ...] for boolean switch DPs in wizard mapping."""
    if not isinstance(mapping, dict):
        return []
    switches: list[tuple[str, str]] = []
    for dp_id, entry in mapping.items():
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", ""))
        if not code.startswith("switch"):
            continue
        entry_type = str(entry.get("type", "Boolean"))
        if entry_type not in ("Boolean", "bool"):
            continue
        switches.append((str(dp_id), code))

    def sort_key(item: tuple[str, str]) -> tuple[int, int | str]:
        _dp, switch_code = item
        if switch_code == "switch":
            return (0, 0)
        match = re.match(r"switch_(\d+)$", switch_code)
        if match:
            return (1, int(match.group(1)))
        return (2, switch_code)

    switches.sort(key=sort_key)
    return switches


def switch_code_to_dp(switch_code: str) -> str:
    match = re.match(r"switch_(\d+)$", switch_code)
    if match:
        return match.group(1)
    return "1"


def _apply_default_switch(dev: DiscoveredDevice) -> DiscoveredDevice:
    mapping = dev.raw.get("mapping") if dev.raw else None
    switches = tuya_switch_codes_from_mapping(mapping)
    if switches:
        dev.tuya_switch_dp, dev.tuya_switch_code = switches[0]
    elif not dev.tuya_switch_code:
        dev.tuya_switch_code = "switch_1"
        dev.tuya_switch_dp = "1"
    elif not dev.tuya_switch_dp:
        dev.tuya_switch_dp = switch_code_to_dp(dev.tuya_switch_code)
    return dev


def dedupe_tuya_devices(devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """Keep one row per Tuya device ID, preferring entries with mapping and local keys."""
    merged: dict[str, DiscoveredDevice] = {}
    extras: list[DiscoveredDevice] = []
    for dev in devices:
        device_id = dev.tuya_device_id
        if not device_id:
            extras.append(dev)
            continue
        if device_id not in merged:
            merged[device_id] = dev
            continue
        existing = merged[device_id]
        existing_mapping = bool(existing.raw.get("mapping")) if existing.raw else False
        new_mapping = bool(dev.raw.get("mapping")) if dev.raw else False
        if new_mapping and not existing_mapping:
            merged[device_id] = dev
            existing = dev
        if dev.tuya_local_key and not existing.tuya_local_key:
            existing.tuya_local_key = dev.tuya_local_key
        if dev.tuya_ip and not existing.tuya_ip:
            existing.tuya_ip = dev.tuya_ip
        if dev.label and len(dev.label) > len(existing.label):
            existing.label = dev.label
        if dev.raw and not existing.raw:
            existing.raw = dev.raw
    return extras + list(merged.values())


def expand_tuya_switch_devices(devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """Split multi-outlet Tuya devices into one import row per switch."""
    expanded: list[DiscoveredDevice] = []
    for dev in devices:
        if not dev.tuya_device_id:
            expanded.append(_apply_default_switch(dev))
            continue
        mapping = dev.raw.get("mapping") if dev.raw else None
        switches = tuya_switch_codes_from_mapping(mapping)
        if len(switches) <= 1:
            expanded.append(_apply_default_switch(dev))
            continue
        for index, (dp_id, switch_code) in enumerate(switches):
            label = f"{dev.label} ({switch_code})"
            expanded.append(
                DiscoveredDevice(
                    source=dev.source,
                    label=label,
                    tuya_device_id=dev.tuya_device_id,
                    tuya_ip=dev.tuya_ip,
                    tuya_local_key=dev.tuya_local_key,
                    tuya_version=dev.tuya_version,
                    tuya_switch_code=switch_code,
                    tuya_switch_dp=dp_id,
                    smartthings_device_id=dev.smartthings_device_id if index == 0 else "",
                    match_key=f"{dev.match_key}_{switch_code}"
                    if dev.match_key
                    else f"{dev.tuya_device_id}_{switch_code}",
                    raw=dev.raw,
                )
            )
    return expanded


def _parse_tuya_version(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 3.4


def resolve_credential_paths(base: Path) -> dict[str, Path]:
    """Standard locations for import credential files (host + Docker)."""
    cred_dir = base / "credentials"
    return {
        "credentials_dir": cred_dir,
        "tinytuya_json": _first_existing_path(
            cred_dir / "tinytuya.json",
            base / "tinytuya.json",
            Path("credentials/tinytuya.json"),
            Path("tinytuya.json"),
        ),
        "devices_json": _first_existing_path(
            cred_dir / "devices.json",
            base / "devices.json",
            Path("credentials/devices.json"),
            Path("devices.json"),
        ),
        "env_file": _first_existing_path(base / ".env", Path(".env")),
    }


def _first_existing_path(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def load_tuya_cloud_credentials(
    *,
    api_key: str = "",
    api_secret: str = "",
    api_region: str = "",
    api_device_id: str = "",
    config_file: Path | None = None,
) -> tuple[str, str, str, str, Path | None]:
    """Merge Tuya cloud credentials from env vars and tinytuya.json."""
    key = api_key.strip()
    secret = api_secret.strip()
    region = (api_region or "us").strip()
    device_id = api_device_id.strip()
    source_file = config_file if config_file and config_file.exists() else None

    if source_file:
        try:
            with source_file.open(encoding="utf-8") as f:
                cfg = json.load(f)
            key = key or str(cfg.get("apiKey") or cfg.get("api_key") or "")
            secret = secret or str(cfg.get("apiSecret") or cfg.get("api_secret") or "")
            region = region or str(cfg.get("apiRegion") or cfg.get("api_region") or "us")
            device_id = device_id or str(cfg.get("apiDeviceID") or cfg.get("api_device_id") or "")
        except Exception as exc:
            logger.warning("failed to read %s: %s", source_file, exc)

    return key, secret, region, device_id, source_file


def get_import_setup_status(
    *,
    smartthings_pat: str = "",
    tuya_api_key: str = "",
    tuya_api_secret: str = "",
    tuya_api_region: str = "",
    tuya_api_device_id: str = "",
    meross_email: str = "",
    meross_password: str = "",
    paths: dict[str, Path | None] | None = None,
) -> dict[str, Any]:
    """Report which import sources are configured (no network calls)."""
    paths = paths or {}
    tuya_cfg_path = paths.get("tinytuya_json")
    devices_path = paths.get("devices_json")
    env_path = paths.get("env_file")

    key, secret, region, device_id, tuya_file = load_tuya_cloud_credentials(
        api_key=tuya_api_key,
        api_secret=tuya_api_secret,
        api_region=tuya_api_region,
        api_device_id=tuya_api_device_id,
        config_file=tuya_cfg_path,
    )
    has_st = bool(smartthings_pat.strip())
    has_tuya_cloud = bool(key and secret)
    has_meross = bool(meross_email.strip() and meross_password)
    has_devices_json = devices_path is not None and devices_path.exists()
    has_tuya_file = tuya_file is not None

    return {
        "smartthings": {
            "configured": has_st,
            "hint": (
                "Set SMARTTHINGS_PAT in pumpd/.env "
                "(Personal Access Token from SmartThings developer portal)."
                if not has_st
                else "SmartThings PAT loaded."
            ),
        },
        "tuya_cloud": {
            "configured": has_tuya_cloud,
            "hint": (
                "Add TUYA_API_KEY and TUYA_API_SECRET to pumpd/.env, or copy tinytuya.json from "
                "'python -m tinytuya wizard' into pumpd/credentials/tinytuya.json."
                if not has_tuya_cloud
                else f"Tuya cloud credentials loaded ({'env + ' if tuya_api_key else ''}"
                f"{'file' if has_tuya_file else 'env'})."
            ),
            "region": region if has_tuya_cloud else "",
            "device_id_set": bool(device_id),
        },
        "meross_cloud": {
            "configured": has_meross,
            "hint": (
                "Set MEROSS_EMAIL and MEROSS_PASSWORD in pumpd/.env."
                if not has_meross
                else "Meross cloud credentials loaded."
            ),
        },
        "tuya_json": {
            "configured": has_devices_json,
            "path": str(devices_path) if devices_path else "",
            "hint": (
                "Upload devices.json from the tinytuya wizard (Admin → Import pumps), "
                "or place it at pumpd/credentials/devices.json."
                if not has_devices_json
                else f"Found {devices_path.name}."
            ),
        },
        "env_file": {
            "configured": env_path is not None,
            "path": str(env_path) if env_path else "",
        },
        "ready": has_st or has_tuya_cloud or has_devices_json or has_meross,
    }


async def discover_smartthings(pat: str) -> list[DiscoveredDevice]:
    if not pat:
        return []
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
    devices: list[DiscoveredDevice] = []
    url: str | None = f"{ST_BASE}/devices"
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        while url:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("items", []):
                if not _smartthings_is_switch(item):
                    continue
                label = item.get("label") or item.get("name") or item["deviceId"]
                devices.append(
                    DiscoveredDevice(
                        source="smartthings",
                        label=label,
                        smartthings_device_id=item["deviceId"],
                        match_key=normalize_match_key(label),
                        raw=item,
                    )
                )
            url = payload.get("_links", {}).get("next", {}).get("href")
            if url and url.startswith("/"):
                url = f"https://api.smartthings.com{url}"
    return devices


def _smartthings_is_switch(item: dict[str, Any]) -> bool:
    for component in item.get("components", []):
        for capability in component.get("capabilities", []):
            cap_id = capability.get("id") or capability.get("capabilityId", "")
            if cap_id in SWITCH_CAPABILITIES:
                return True
    ocf = item.get("ocfDeviceType", "")
    if ocf in ("oic.d.switch", "oic.d.outlet"):
        return True
    name = (item.get("name") or "").lower()
    return "switch" in name or "plug" in name or "outlet" in name


def load_tuya_devices_json(path: Path) -> list[DiscoveredDevice]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = list(data.values())
    devices: list[DiscoveredDevice] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("id") or item.get("device_id") or "")
        if not device_id:
            continue
        label = str(item.get("name") or device_id)
        devices.append(
            DiscoveredDevice(
                source="tuya_json",
                label=label,
                tuya_device_id=device_id,
                tuya_ip=str(item.get("ip") or ""),
                tuya_local_key=str(item.get("key") or item.get("local_key") or ""),
                tuya_version=_parse_tuya_version(item.get("version", 3.4)),
                match_key=normalize_match_key(label),
                raw=item,
            )
        )
    return devices


async def discover_tuya_cloud(
    *,
    api_key: str = "",
    api_secret: str = "",
    api_region: str = "",
    api_device_id: str = "",
    config_file: Path | None = None,
) -> tuple[list[DiscoveredDevice], str | None]:
    key, secret, region, device_id, cfg_path = load_tuya_cloud_credentials(
        api_key=api_key,
        api_secret=api_secret,
        api_region=api_region,
        api_device_id=api_device_id,
        config_file=config_file,
    )
    if not key or not secret:
        return [], None  # skipped — not an error

    try:
        import tinytuya

        cloud = tinytuya.Cloud(
            apiRegion=region or None,
            apiKey=key,
            apiSecret=secret,
            apiDeviceID=device_id or None,
            configFile=str(cfg_path) if cfg_path else tinytuya.CONFIGFILE,
        )
        raw_devices = await asyncio.to_thread(cloud.getdevices)
    except TypeError as exc:
        return [], str(exc)
    except Exception as exc:
        logger.exception("tuya cloud discovery failed")
        return [], str(exc)

    if isinstance(raw_devices, dict) and raw_devices.get("Error"):
        return [], str(raw_devices.get("Error", "Tuya cloud error"))

    devices: list[DiscoveredDevice] = []
    for item in raw_devices or []:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("id") or "")
        if not device_id:
            continue
        label = str(item.get("name") or device_id)
        devices.append(
            DiscoveredDevice(
                source="tuya_cloud",
                label=label,
                tuya_device_id=device_id,
                tuya_ip=str(item.get("ip") or item.get("last_ip") or ""),
                tuya_local_key=str(item.get("key") or ""),
                tuya_version=_parse_tuya_version(item.get("version", 3.4)),
                match_key=normalize_match_key(label),
                raw=item,
            )
        )
    return devices, None


async def discover_tuya_lan(timeout: int = 8) -> tuple[list[DiscoveredDevice], str | None]:
    try:
        import tinytuya

        found = await asyncio.to_thread(
            tinytuya.deviceScan,
            False,
            timeout,
            False,
            False,
            True,
        )
    except Exception as exc:
        logger.exception("tuya lan scan failed")
        return [], str(exc)

    devices: list[DiscoveredDevice] = []
    for ip, item in (found or {}).items():
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("gwId") or item.get("id") or "")
        if not device_id:
            continue
        label = str(item.get("name") or item.get("productName") or device_id)
        devices.append(
            DiscoveredDevice(
                source="tuya_lan",
                label=label,
                tuya_device_id=device_id,
                tuya_ip=str(ip),
                tuya_local_key=str(item.get("key") or item.get("local_key") or ""),
                tuya_version=_parse_tuya_version(item.get("version", 3.4)),
                match_key=normalize_match_key(label),
                raw=item,
            )
        )
    return devices, None


def merge_discovered(
    smartthings: list[DiscoveredDevice],
    tuya: list[DiscoveredDevice],
) -> list[DiscoveredDevice]:
    """Pair SmartThings and Tuya entries with similar names into one row."""
    merged: dict[str, DiscoveredDevice] = {}

    for dev in tuya:
        key = dev.match_key or dev.tuya_device_id
        if key in merged:
            existing = merged[key]
            existing.tuya_device_id = dev.tuya_device_id or existing.tuya_device_id
            existing.tuya_ip = dev.tuya_ip or existing.tuya_ip
            existing.tuya_local_key = dev.tuya_local_key or existing.tuya_local_key
            existing.tuya_version = dev.tuya_version or existing.tuya_version
            if dev.tuya_switch_code:
                existing.tuya_switch_code = dev.tuya_switch_code
            if dev.tuya_switch_dp:
                existing.tuya_switch_dp = dev.tuya_switch_dp
            if dev.label and len(dev.label) > len(existing.label):
                existing.label = dev.label
        else:
            merged[key] = DiscoveredDevice(
                source=dev.source,
                label=dev.label,
                tuya_device_id=dev.tuya_device_id,
                tuya_ip=dev.tuya_ip,
                tuya_local_key=dev.tuya_local_key,
                tuya_version=dev.tuya_version,
                tuya_switch_code=dev.tuya_switch_code,
                tuya_switch_dp=dev.tuya_switch_dp,
                match_key=key,
                raw=dev.raw,
            )

    for st in smartthings:
        key = st.match_key or st.smartthings_device_id
        if key in merged:
            merged[key].smartthings_device_id = st.smartthings_device_id
            if st.label and len(st.label) > len(merged[key].label):
                merged[key].label = st.label
        else:
            merged[key] = DiscoveredDevice(
                source="smartthings",
                label=st.label,
                smartthings_device_id=st.smartthings_device_id,
                match_key=key,
                raw=st.raw,
            )

    # Fuzzy match remaining ST devices to tuya by substring
    for st in smartthings:
        if any(d.smartthings_device_id == st.smartthings_device_id for d in merged.values()):
            continue
        best_key: str | None = None
        for key, tuya_dev in merged.items():
            if tuya_dev.smartthings_device_id:
                continue
            if key and key in st.match_key or st.match_key in key:
                best_key = key
                break
        if best_key:
            merged[best_key].smartthings_device_id = st.smartthings_device_id
        else:
            merged[st.match_key or st.smartthings_device_id] = st

    return sorted(merged.values(), key=lambda d: d.label.lower())


def expand_meross_channel_devices(devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """One import row per Meross outlet (multi-gang plugs)."""
    from app.devices.meross_cloud import channel_to_switch_code, iter_meross_outlets

    expanded: list[DiscoveredDevice] = []
    for dev in devices:
        if not dev.meross_device_uuid:
            expanded.append(dev)
            continue
        raw = dev.raw or {}
        channels = raw.get("channels")
        device_type = str(raw.get("device_type") or "")
        if not isinstance(channels, list) or len(channels) <= 1:
            if not dev.meross_switch_code:
                dev.meross_switch_code = channel_to_switch_code(dev.meross_channel)
            expanded.append(dev)
            continue
        outlets = iter_meross_outlets(channels, device_type=device_type)
        for index, (channel, suffix) in enumerate(outlets):
            label = dev.label if not suffix else f"{dev.label} {suffix}".strip()
            expanded.append(
                DiscoveredDevice(
                    source="meross_cloud",
                    label=label,
                    meross_device_uuid=dev.meross_device_uuid,
                    meross_channel=channel,
                    meross_switch_code=f"switch_{index + 1}",
                    match_key=f"{dev.meross_device_uuid}_{channel}",
                    raw=dev.raw,
                )
            )
    return expanded


async def discover_meross(
    *,
    email: str = "",
    password: str = "",
    api_base: str = "https://iotx-us.meross.com",
    mfa_code: str = "",
) -> tuple[list[DiscoveredDevice], str | None]:
    if not email.strip() or not password:
        return [], None
    from app.devices.meross_cloud import MerossCloudSession

    session = MerossCloudSession(
        email=email,
        password=password,
        api_base_url=api_base,
        mfa_code=mfa_code,
    )
    try:
        cloud_devices = await session.list_cloud_devices()
    except Exception as exc:
        return [], str(exc)
    finally:
        if not session.started:
            await session.shutdown()

    devices: list[DiscoveredDevice] = []
    for info in cloud_devices:
        devices.append(
            DiscoveredDevice(
                source="meross_cloud",
                label=info.dev_name,
                meross_device_uuid=info.uuid,
                meross_channel=0,
                match_key=info.uuid,
                raw={
                    "device_type": info.device_type,
                    "channels": info.channels,
                    "online_status": getattr(info.online_status, "name", str(info.online_status)),
                },
            )
        )
    return expand_meross_channel_devices(devices), None


async def discover_all(
    *,
    smartthings_pat: str = "",
    tuya_api_key: str = "",
    tuya_api_secret: str = "",
    tuya_api_region: str = "",
    tuya_api_device_id: str = "",
    meross_email: str = "",
    meross_password: str = "",
    meross_api_base: str = "https://iotx-us.meross.com",
    meross_mfa_code: str = "",
    tuya_config_file: Path | None = None,
    tuya_devices_file: Path | None = None,
    lan_scan: bool = True,
) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    tuya_devices: list[DiscoveredDevice] = []

    setup = get_import_setup_status(
        smartthings_pat=smartthings_pat,
        tuya_api_key=tuya_api_key,
        tuya_api_secret=tuya_api_secret,
        tuya_api_region=tuya_api_region,
        tuya_api_device_id=tuya_api_device_id,
        meross_email=meross_email,
        meross_password=meross_password,
        paths={
            "tinytuya_json": tuya_config_file,
            "devices_json": tuya_devices_file,
        },
    )

    if tuya_devices_file and tuya_devices_file.exists():
        loaded = load_tuya_devices_json(tuya_devices_file)
        tuya_devices.extend(loaded)
        sources["tuya_json"] = {
            "status": "ok",
            "count": len(loaded),
            "message": f"Loaded {len(loaded)} device(s) from {tuya_devices_file.name}",
        }
    else:
        sources["tuya_json"] = {
            "status": "skipped",
            "count": 0,
            "message": setup["tuya_json"]["hint"],
        }

    key, secret, _, _, _ = load_tuya_cloud_credentials(
        api_key=tuya_api_key,
        api_secret=tuya_api_secret,
        api_region=tuya_api_region,
        api_device_id=tuya_api_device_id,
        config_file=tuya_config_file,
    )
    if key and secret:
        cloud, cloud_err = await discover_tuya_cloud(
            api_key=tuya_api_key,
            api_secret=tuya_api_secret,
            api_region=tuya_api_region,
            api_device_id=tuya_api_device_id,
            config_file=tuya_config_file,
        )
        tuya_devices.extend(cloud)
        if cloud_err:
            sources["tuya_cloud"] = {"status": "error", "count": 0, "message": cloud_err}
        else:
            sources["tuya_cloud"] = {
                "status": "ok",
                "count": len(cloud),
                "message": f"Fetched {len(cloud)} device(s) from Tuya cloud",
            }
    else:
        sources["tuya_cloud"] = {
            "status": "skipped",
            "count": 0,
            "message": setup["tuya_cloud"]["hint"],
        }

    if lan_scan:
        lan, lan_err = await discover_tuya_lan()
        tuya_devices.extend(lan)
        if lan_err:
            sources["tuya_lan"] = {"status": "error", "count": 0, "message": lan_err}
        else:
            sources["tuya_lan"] = {
                "status": "ok",
                "count": len(lan),
                "message": f"Found {len(lan)} device(s) on LAN (keys may be missing)",
            }
    else:
        sources["tuya_lan"] = {"status": "skipped", "count": 0, "message": "LAN scan disabled"}

    if smartthings_pat.strip():
        try:
            smartthings = await discover_smartthings(smartthings_pat)
            sources["smartthings"] = {
                "status": "ok",
                "count": len(smartthings),
                "message": f"Fetched {len(smartthings)} switch device(s) from SmartThings",
            }
        except Exception as exc:
            smartthings = []
            sources["smartthings"] = {"status": "error", "count": 0, "message": str(exc)}
    else:
        smartthings = []
        sources["smartthings"] = {
            "status": "skipped",
            "count": 0,
            "message": setup["smartthings"]["hint"],
        }

    if meross_email.strip() and meross_password:
        meross, meross_err = await discover_meross(
            email=meross_email,
            password=meross_password,
            api_base=meross_api_base,
            mfa_code=meross_mfa_code,
        )
        if meross_err:
            sources["meross_cloud"] = {"status": "error", "count": 0, "message": meross_err}
        else:
            sources["meross_cloud"] = {
                "status": "ok",
                "count": len(meross),
                "message": f"Fetched {len(meross)} outlet(s) from Meross cloud",
            }
    else:
        meross = []
        sources["meross_cloud"] = {
            "status": "skipped",
            "count": 0,
            "message": setup["meross_cloud"]["hint"],
        }

    merged = merge_discovered(smartthings, expand_tuya_switch_devices(dedupe_tuya_devices(tuya_devices)))
    merged.extend(meross)
    merged.sort(key=lambda d: d.label.lower())
    return {
        "devices": [d.to_dict() for d in merged],
        "counts": {
            "smartthings": len(smartthings),
            "tuya": len(tuya_devices),
            "meross": len(meross),
            "merged": len(merged),
        },
        "sources": sources,
        "setup": setup,
        # legacy — only real failures, not skipped sources
        "errors": {
            k: v["message"]
            for k, v in sources.items()
            if v.get("status") == "error"
        },
    }


def discovered_device_key(data: dict[str, Any]) -> str:
    meross_uuid = str(data.get("meross_device_uuid") or "").strip()
    if meross_uuid:
        channel = int(data.get("meross_channel") or 0)
        return f"meross:{meross_uuid}:{channel}"
    tuya_id = str(data.get("tuya_device_id") or "").strip()
    if tuya_id:
        switch = str(data.get("tuya_switch_code") or data.get("switch_code") or "switch_1")
        return f"tuya:{tuya_id}:{switch}"
    st_id = str(data.get("smartthings_device_id") or "").strip()
    if st_id:
        return f"st:{st_id}"
    return ""


def pump_config_key(pump: PumpConfig) -> str:
    if pump.meross.device_uuid.strip():
        return f"meross:{pump.meross.device_uuid.strip()}:{pump.meross.channel}"
    if pump.tuya.device_id.strip():
        switch = pump.tuya.switch_code or "switch_1"
        return f"tuya:{pump.tuya.device_id.strip()}:{switch}"
    if pump.smartthings.device_id.strip():
        return f"st:{pump.smartthings.device_id.strip()}"
    return ""


def annotate_discovered_system_status(
    devices: list[dict[str, Any]],
    existing: list[PumpConfig],
) -> list[dict[str, Any]]:
    """Mark each discovered row as already imported or new (by device identity)."""
    by_key = {pump_config_key(pump): pump.name for pump in existing if pump_config_key(pump)}
    annotated: list[dict[str, Any]] = []
    for data in devices:
        row = dict(data)
        key = discovered_device_key(data)
        if key and key in by_key:
            row["in_system"] = True
            row["existing_pump_name"] = by_key[key]
        else:
            row["in_system"] = False
            row["existing_pump_name"] = ""
        annotated.append(row)
    return annotated


def import_status_counts(devices: list[dict[str, Any]]) -> dict[str, int]:
    existing = sum(1 for d in devices if d.get("in_system"))
    return {
        "total": len(devices),
        "new": len(devices) - existing,
        "existing": existing,
    }


def _unique_pump_name(label: str, used: set[str]) -> str:
    base = slugify_pump_name(label)
    if base not in used:
        used.add(base)
        return base
    index = 2
    while True:
        candidate = f"{base}_{index}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def discovered_dict_to_pump_config(data: dict[str, Any], name: str, *, enabled: bool = True) -> PumpConfig:
    from app.config import MerossConfig, SmartThingsPumpConfig, TuyaConfig

    switch_code = str(data.get("tuya_switch_code") or data.get("switch_code") or "")
    meross_switch = str(data.get("meross_switch_code") or "")
    if not meross_switch and data.get("meross_device_uuid"):
        from app.devices.meross_cloud import channel_to_switch_code

        meross_switch = channel_to_switch_code(int(data.get("meross_channel") or 0))

    return PumpConfig(
        name=name,
        label=str(data.get("label") or "").strip(),
        enabled=enabled,
        tuya=TuyaConfig(
            device_id=str(data.get("tuya_device_id") or ""),
            ip=str(data.get("tuya_ip") or ""),
            local_key=str(data.get("tuya_local_key") or ""),
            version=float(data.get("tuya_version") or 3.4),
            switch_code=switch_code,
        ),
        meross=MerossConfig(
            device_uuid=str(data.get("meross_device_uuid") or ""),
            channel=int(data.get("meross_channel") or 0),
            switch_code=meross_switch,
        ),
        smartthings=SmartThingsPumpConfig(
            device_id=str(data.get("smartthings_device_id") or ""),
        ),
    )


def build_auto_import_pumps(
    discovered: list[dict[str, Any]],
    existing: list[PumpConfig],
) -> tuple[list[PumpConfig], dict[str, int]]:
    """Map discovered devices to pump configs, matching existing pumps by device identity."""
    existing_by_key = {
        key: pump for pump in existing if (key := pump_config_key(pump))
    }
    used_names = {p.name for p in existing}
    to_import: list[PumpConfig] = []
    added = 0
    updated = 0
    skipped = 0

    for data in discovered:
        key = discovered_device_key(data)
        if not key:
            skipped += 1
            continue
        if key in existing_by_key:
            current = existing_by_key[key]
            pump = discovered_dict_to_pump_config(data, current.name, enabled=current.enabled)
            if current.max_runtime_minutes is not None:
                pump.max_runtime_minutes = current.max_runtime_minutes
            to_import.append(pump)
            updated += 1
        else:
            name = _unique_pump_name(str(data.get("label") or "pump"), used_names)
            to_import.append(discovered_dict_to_pump_config(data, name))
            added += 1

    return to_import, {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "discovered": len(discovered),
    }


async def auto_import_devices(
    *,
    existing: list[PumpConfig],
    smartthings_pat: str = "",
    tuya_api_key: str = "",
    tuya_api_secret: str = "",
    tuya_api_region: str = "",
    tuya_api_device_id: str = "",
    meross_email: str = "",
    meross_password: str = "",
    meross_api_base: str = "https://iotx-us.meross.com",
    meross_mfa_code: str = "",
    tuya_config_file: Path | None = None,
    tuya_devices_file: Path | None = None,
    lan_scan: bool = True,
) -> dict[str, Any]:
    """Discover all configured sources and build pump configs for merge import."""
    discovery = await discover_all(
        smartthings_pat=smartthings_pat,
        tuya_api_key=tuya_api_key,
        tuya_api_secret=tuya_api_secret,
        tuya_api_region=tuya_api_region,
        tuya_api_device_id=tuya_api_device_id,
        meross_email=meross_email,
        meross_password=meross_password,
        meross_api_base=meross_api_base,
        meross_mfa_code=meross_mfa_code,
        tuya_config_file=tuya_config_file,
        tuya_devices_file=tuya_devices_file,
        lan_scan=lan_scan,
    )
    pumps, stats = build_auto_import_pumps(discovery.get("devices", []), existing)
    return {
        "pumps": pumps,
        "stats": stats,
        "discovery": discovery,
    }
