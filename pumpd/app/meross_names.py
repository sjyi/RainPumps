"""Meross cloud display name extraction and local config merge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import AppConfig, DeviceLabelOverride, PumpConfig
from app.devices.meross_cloud import iter_meross_outlets
from app.device_keys import device_label_key
from app.display_names import device_labels_map, pump_display_label


@dataclass(frozen=True)
class MerossCloudNames:
    device_labels: dict[str, str]
    pump_labels: dict[str, str]


def _device_info_fields(info: Any) -> tuple[str, str, list[Any], str]:
    if isinstance(info, dict):
        dev_name = str(info.get("devName") or "").strip()
        channels = info.get("channels") if isinstance(info.get("channels"), list) else []
        device_type = str(info.get("deviceType") or info.get("device_type") or "")
        uuid = str(info.get("uuid") or "")
        return uuid, dev_name, channels, device_type
    dev_name = str(getattr(info, "dev_name", "") or "").strip()
    channels = getattr(info, "channels", None) or []
    if not isinstance(channels, list):
        channels = []
    device_type = str(getattr(info, "device_type", "") or "")
    uuid = str(getattr(info, "uuid", "") or "")
    return uuid, dev_name, channels, device_type


def meross_switch_label(
    dev_name: str,
    channel: int,
    channels: list[Any] | None,
    *,
    device_type: str = "",
) -> str:
    """Build a pump switch label from Meross cloud device + channel metadata."""
    dev_name = dev_name.strip()
    if not dev_name and not channels:
        return ""
    outlets = iter_meross_outlets(channels, device_type=device_type) if channels else [(0, "")]
    if len(outlets) <= 1:
        return dev_name
    for ch, suffix in outlets:
        if ch == channel:
            suffix = (suffix or "").strip()
            return suffix or dev_name
    return dev_name


def collect_meross_cloud_names(
    cloud_devices: list[Any],
    pumps: list[PumpConfig],
) -> MerossCloudNames:
    by_uuid: dict[str, tuple[str, list[Any], str]] = {}
    for info in cloud_devices:
        uuid, dev_name, channels, device_type = _device_info_fields(info)
        if uuid:
            by_uuid[uuid] = (dev_name, channels, device_type)

    device_labels: dict[str, str] = {}
    pump_labels: dict[str, str] = {}

    for pump in pumps:
        uuid = pump.meross.device_uuid
        if not uuid or uuid not in by_uuid:
            continue
        dev_name, channels, device_type = by_uuid[uuid]
        if dev_name:
            device_labels[device_label_key("meross", uuid)] = dev_name
        switch_label = meross_switch_label(
            dev_name,
            pump.meross.channel,
            channels,
            device_type=device_type,
        )
        if switch_label:
            pump_labels[pump.name] = switch_label

    return MerossCloudNames(device_labels=device_labels, pump_labels=pump_labels)


def diff_meross_cloud_names(
    config: AppConfig,
    cloud: MerossCloudNames,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return pump/device label updates needed to match Meross cloud."""
    current_devices = device_labels_map(config)
    pump_updates: dict[str, str] = {}
    device_updates: dict[str, str] = {}

    for name, label in cloud.pump_labels.items():
        pump = next((p for p in config.pumps if p.name == name), None)
        if pump is None:
            continue
        current = pump_display_label(pump)
        if current != label:
            pump_updates[name] = label

    for key, label in cloud.device_labels.items():
        current = current_devices.get(key, "")
        if current != label:
            device_updates[key] = label

    return pump_updates, device_updates


def merge_device_label_rows(
    existing: list[DeviceLabelOverride],
    updates: dict[str, str],
) -> list[DeviceLabelOverride]:
    rows = {
        device_label_key(row.device_backend, row.device_id): row for row in existing
    }
    for key, label in updates.items():
        if ":" not in key:
            continue
        backend, device_id = key.split(":", 1)
        if not backend or not device_id:
            continue
        cleaned = (label or "").strip()
        if cleaned:
            rows[key] = DeviceLabelOverride(
                device_backend=backend,
                device_id=device_id,
                label=cleaned,
            )
    return list(rows.values())
