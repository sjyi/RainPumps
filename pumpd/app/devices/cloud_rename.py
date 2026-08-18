"""Push display name changes to cloud device APIs where supported."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.devices.smartthings import ST_BASE

logger = logging.getLogger(__name__)


def _cloud_success(result: Any) -> tuple[bool, str]:
    if isinstance(result, dict):
        if result.get("success") is True:
            return True, ""
        if result.get("Error"):
            return False, str(result["Error"])
        return False, str(result.get("msg") or result)
    return False, "unexpected cloud response"


def tuya_switch_index(switch_code: str) -> int | None:
    match = re.match(r"switch_(\d+)$", (switch_code or "").strip())
    if match:
        return int(match.group(1))
    return None


async def rename_tuya_device(cloud: Any, device_id: str, name: str) -> dict[str, Any]:
    if not cloud or not device_id or not name.strip():
        return {"success": False, "message": "missing Tuya cloud client or name"}
    try:
        result = await asyncio.to_thread(
            cloud.cloudrequest,
            f"/v1.0/devices/{device_id}",
            "PUT",
            {"name": name.strip()},
        )
    except Exception as exc:
        logger.debug("tuya device rename failed for %s: %s", device_id, exc)
        return {"success": False, "message": str(exc)}
    ok, err = _cloud_success(result)
    return {"success": ok, "message": err or "updated in Tuya Cloud"}


async def rename_tuya_switch(
    cloud: Any,
    device_id: str,
    switch_code: str,
    name: str,
) -> dict[str, Any]:
    if not cloud or not device_id or not name.strip():
        return {"success": False, "message": "missing Tuya cloud client or name"}
    idx = tuya_switch_index(switch_code)
    if idx is None:
        return {"success": False, "message": f"unsupported Tuya switch code {switch_code!r}"}
    try:
        result = await asyncio.to_thread(
            cloud.cloudrequest,
            f"/v1.0/devices/{device_id}/multiple-name",
            "PUT",
            {"name": name.strip(), "id": idx},
        )
    except Exception as exc:
        logger.debug("tuya switch rename failed for %s/%s: %s", device_id, switch_code, exc)
        return {"success": False, "message": str(exc)}
    ok, err = _cloud_success(result)
    if ok:
        return {"success": True, "message": "updated outlet name in Tuya Cloud"}
    # Single-outlet devices may not support multiple-name; fall back to device rename.
    fallback = await rename_tuya_device(cloud, device_id, name)
    if fallback.get("success"):
        fallback["message"] = "updated device name in Tuya Cloud (single-outlet fallback)"
    return fallback


async def rename_smartthings_device(pat: str, device_id: str, label: str) -> dict[str, Any]:
    if not pat or not device_id or not label.strip():
        return {"success": False, "message": "missing SmartThings credentials or label"}
    url = f"{ST_BASE}/devices/{device_id}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.put(url, headers=headers, json={"label": label.strip()})
            resp.raise_for_status()
    except Exception as exc:
        logger.debug("smartthings rename failed for %s: %s", device_id, exc)
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "updated label in SmartThings"}


async def rename_meross_device(session: Any, device_uuid: str, name: str) -> dict[str, Any]:
    if not session or not getattr(session, "configured", False):
        return {"success": False, "message": "Meross cloud not configured"}
    if not device_uuid or not name.strip():
        return {"success": False, "message": "missing Meross device or name"}
    try:
        if not session.started:
            await session.startup()
        return await session.update_cloud_device_name(device_uuid, name)
    except Exception as exc:
        logger.debug("meross device rename failed for %s: %s", device_uuid, exc)
        return {"success": False, "message": str(exc)}


async def rename_meross_switch(
    session: Any,
    device_uuid: str,
    channel: int,
    name: str,
) -> dict[str, Any]:
    if not session or not getattr(session, "configured", False):
        return {"success": False, "message": "Meross cloud not configured"}
    if not device_uuid or not name.strip():
        return {"success": False, "message": "missing Meross device or name"}
    try:
        if not session.started:
            await session.startup()
        devices = await session.list_cloud_devices()
        info = next((d for d in devices if getattr(d, "uuid", "") == device_uuid), None)
        if info is None:
            return {"success": False, "message": "device not found in Meross cloud"}
        return await session.update_cloud_switch_name(
            device_uuid,
            channel,
            name,
            device_type=str(getattr(info, "device_type", "") or ""),
            channels=getattr(info, "channels", None),
        )
    except Exception as exc:
        logger.debug(
            "meross switch rename failed for %s ch%s: %s",
            device_uuid,
            channel,
            exc,
        )
        return {"success": False, "message": str(exc)}
