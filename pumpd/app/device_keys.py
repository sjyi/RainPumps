"""Shared helpers for identifying physical devices across backends."""


def device_label_key(device_backend: str, device_id: str) -> str:
    return f"{device_backend}:{device_id}"
