# pumpd Design Caveats

**Version:** 1.1.0

Operational and design limitations to understand before deploying pumpd in production.

## Tuya local control

- **Local keys rotate** when a device is re-paired in Smart Life. Control failures after re-pairing usually mean re-running the tinytuya wizard.
- Pumps need **fixed LAN IPs** (DHCP reservations). tinytuya communicates over UDP; routing or VLAN isolation can block control.
- tinytuya is community-maintained and may lag Tuya firmware changes.

## SmartThings fallback

- Requires **internet access**. It does not help during WAN outages; local Tuya is the primary path for that scenario.
- PAT tokens expire; monitor for 401 errors in provider/hardware health.

## Weather providers

- **Open-Meteo** is the primary source and works globally without an API key.
- **NWS** is US-only and auto-skipped outside the US bounding box. Hourly quantitative precipitation is often missing or zero, so NWS may add little beyond Open-Meteo.
- **Pre-emptive start** requires both high probability *and* a rain-amount sum threshold. High-probability, low-amount forecasts may not trigger a pre-emptive start.
- During **internet outages**, cached forecasts are used until the stale-forecast watchdog trips (default 3 h). Without MQTT, pumps turn off even if it is actually raining — an intentional fail-safe.

## Phase 1 vs Phase 2 rain detection

- Phase 1 relies on forecast only. Forecasts can report rain when none is falling locally.
- Phase 2 MQTT sensor can override forecast for current conditions when confidence ≥ `mqtt.min_confidence`.
- Rooftop **ESP32 Wi-Fi** must be reliable; sensor stale detection alerts when messages stop.

## Duty cycle

- Duty cycling during `pre_rain` / `running` reduces pump wear but may leave standing water if the off window is long during heavy rain. Tune `on_minutes` / `off_minutes` for your roof.

## Manual mode and safety

- Manual mode can override safety hard-stops **only** with explicit `approve_safety_override=true` on the mode-change API call. This is logged and notified.
- Without approval, safety watchdogs still apply even in manual mode.

## SQLite

- Single-host, low write volume. Not suitable for multi-instance deployment without external coordination.
- Back up the `data/` volume regularly.

## Hardware health monitoring

- Detects control failures, verify mismatches, unreachable devices, and stale sensor data.
- Cannot detect a **mechanically failed pump** (motor seized but switch still reports ON). Consider periodic manual inspection or a flow sensor in future phases.
