#!/usr/bin/env python3
"""Discover and import pump devices from SmartThings and Tuya.

Examples:
  cd pumpd
  python scripts/import_devices.py discover
  python scripts/import_devices.py import --all
  python scripts/import_devices.py import --name north_pump --smartthings-id XXX --tuya-id YYY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import EnvSettings, PumpConfig, SmartThingsPumpConfig, TuyaConfig, save_pumps
from app.device_import import discover_all, resolve_credential_paths, slugify_pump_name


async def cmd_discover(args: argparse.Namespace) -> int:
    env = EnvSettings()
    base = Path(args.config).parent
    paths = resolve_credential_paths(base)
    result = await discover_all(
        smartthings_pat=env.smartthings_pat,
        tuya_api_key=env.tuya_api_key,
        tuya_api_secret=env.tuya_api_secret,
        tuya_api_region=env.tuya_api_region,
        tuya_api_device_id=env.tuya_api_device_id,
        tuya_config_file=paths.get("tinytuya_json"),
        tuya_devices_file=paths.get("devices_json"),
        lan_scan=not args.no_lan,
    )
    print(json.dumps(result, indent=2))
    return 0


async def cmd_import(args: argparse.Namespace) -> int:
    env = EnvSettings()
    base = Path(args.config).parent
    paths = resolve_credential_paths(base)
    result = await discover_all(
        smartthings_pat=env.smartthings_pat,
        tuya_api_key=env.tuya_api_key,
        tuya_api_secret=env.tuya_api_secret,
        tuya_api_region=env.tuya_api_region,
        tuya_api_device_id=env.tuya_api_device_id,
        tuya_config_file=paths.get("tinytuya_json"),
        tuya_devices_file=paths.get("devices_json"),
        lan_scan=not args.no_lan,
    )
    pumps: list[PumpConfig] = []
    for dev in result["devices"]:
        if not args.all:
            continue
        pumps.append(
            PumpConfig(
                name=slugify_pump_name(dev["label"]),
                enabled=True,
                tuya=TuyaConfig(
                    device_id=dev.get("tuya_device_id", ""),
                    ip=dev.get("tuya_ip", ""),
                    local_key=dev.get("tuya_local_key", ""),
                    version=float(dev.get("tuya_version", 3.4)),
                    switch_code=dev.get("tuya_switch_code", ""),
                ),
                smartthings=SmartThingsPumpConfig(
                    device_id=dev.get("smartthings_device_id", ""),
                ),
            )
        )
    if args.name:
        pumps = [
            PumpConfig(
                name=args.name,
                tuya=TuyaConfig(
                    device_id=args.tuya_id or "",
                    ip=args.tuya_ip or "",
                    local_key=args.tuya_key or "",
                    version=args.tuya_version,
                ),
                smartthings=SmartThingsPumpConfig(device_id=args.smartthings_id or ""),
            )
        ]
    if not pumps:
        print("No pumps to import. Use --all or --name with device IDs.", file=sys.stderr)
        return 1
    saved = save_pumps(args.config, pumps, mode=args.mode)
    print(f"Saved {len(saved)} pump(s) to {args.config}")
    for p in saved:
        print(f"  - {p.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import SmartThings / Tuya pump devices")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    discover_p = sub.add_parser("discover", help="List discoverable devices as JSON")
    discover_p.add_argument("--no-lan", action="store_true", help="Skip Tuya LAN UDP scan")

    import_p = sub.add_parser("import", help="Write devices to config.yaml")
    import_p.add_argument("--all", action="store_true", help="Import all discovered devices")
    import_p.add_argument("--mode", choices=["merge", "replace"], default="merge")
    import_p.add_argument("--no-lan", action="store_true")
    import_p.add_argument("--name", help="Pump name for manual import")
    import_p.add_argument("--smartthings-id", default="")
    import_p.add_argument("--tuya-id", default="")
    import_p.add_argument("--tuya-ip", default="")
    import_p.add_argument("--tuya-key", default="")
    import_p.add_argument("--tuya-version", type=float, default=3.4)

    args = parser.parse_args()
    if args.command == "discover":
        return asyncio.run(cmd_discover(args))
    if args.command == "import":
        return asyncio.run(cmd_import(args))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
