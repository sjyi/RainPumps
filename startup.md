# pumpd — Startup Guide

**Version:** 1.1.0

This guide covers first-time setup and starting pumpd on an Ubuntu server. For day-to-day use after the system is running, see [operations.md](operations.md). For design context, see [architecture.md](architecture.md). For known limitations, see [design-caveats.md](design-caveats.md).

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Host** | Ubuntu server (or Linux) on the **same LAN** as pump switches |
| **Runtime** | Docker + Docker Compose (production) or Python 3.12+ (development) |
| **Network** | Wired Ethernet recommended; outbound HTTPS for weather APIs |
| **Pump switches** | Tuya / Smart Life plugs or switches with **fixed LAN IPs** (DHCP reservations) |
| **Credentials** | Tuya local keys and/or SmartThings PAT (see §4) |

---

## 2. First-time setup

All commands assume you are in the `pumpd/` application directory:

```bash
cd pumpd
cp config.example.yaml config.yaml
cp .env.example .env
```

### 2.1 Edit `config.yaml`

Minimum fields to configure before production use:

| Setting | Purpose |
|---------|---------|
| `timezone` | Calendar day for runtime totals (e.g. `America/New_York`) |
| `location.latitude` / `location.longitude` | Building coordinates for forecast |
| `pumps[].name` | Unique pump identifier (used in API and env vars) |
| `pumps[].tuya.device_id` | Tuya device ID |
| `pumps[].tuya.ip` | Fixed LAN IP of the switch |
| `pumps[].tuya.local_key` | Local encryption key (or use `.env`; see §4) |
| `pumps[].tuya.version` | Protocol version (usually `3.4`) |
| `pumps[].smartthings.device_id` | SmartThings UUID for fallback (recommended) |
| `notifications.ntfy.topic` | ntfy topic for alerts (recommended) |

Optional but useful:

- `api.auth_enabled` / `api.api_key` — protect dashboard and write API (see [operations.md](operations.md))
- `hardware_monitor` — pump/sensor abnormality detection (enabled by default)
- `mqtt` — Phase 2 rain sensor (leave `enabled: false` until sensor is ready)

### 2.2 Edit `.env`

```bash
SMARTTHINGS_PAT=your_pat_here
API_KEY=your_api_key_here          # if api.auth_enabled: true
SMTP_PASSWORD=your_smtp_password   # if SMTP notifications enabled

# Tuya cloud (optional — for Admin device import)
TUYA_API_KEY=
TUYA_API_SECRET=
TUYA_API_REGION=us
TUYA_API_DEVICE_ID=any_device_id_from_smart_life

TUYA_LOCAL_KEY_NORTH_PUMP=abc123   # pattern: TUYA_LOCAL_KEY_{PUMP_NAME_UPPER}
```

Secrets in `.env` override empty values in `config.yaml`. Never commit `.env` to git.

**Device import (Admin UI):** configure at least one of:

| Source | Setup |
|--------|--------|
| SmartThings | `SMARTTHINGS_PAT` in `.env` |
| Tuya cloud | `TUYA_API_*` in `.env`, **or** `pumpd/credentials/tinytuya.json` from the wizard |
| Local keys | Upload or place `devices.json` in `pumpd/credentials/` (from `python -m tinytuya wizard`) |

After changing `.env` or credential files, restart: `docker compose up -d` (from `pumpd/`).

---

## 3. Obtain device credentials

### 3.1 Tuya local keys (primary control)

1. Create a project at [Tuya IoT Platform](https://iot.tuya.com/).
2. Link your Smart Life app account and note each pump's **device ID**.
3. On a machine with Python: `pip install tinytuya`
4. Run: `python -m tinytuya wizard`
5. Copy each device's `local_key`, `ip`, and `version` into `config.yaml` or `.env`.

Pumps **must** keep the same LAN IP after setup (use DHCP reservations on your router).

### 3.2 SmartThings PAT (fallback control)

1. Open [SmartThings Developer Workspace](https://developer.smartthings.com/).
2. Create a Personal Access Token with `r:devices:*` and `w:devices:*`.
3. Set `SMARTTHINGS_PAT` in `.env`.
4. Add each pump's SmartThings device UUID to `config.yaml` under `smartthings.device_id`.

SmartThings is used only when Tuya local control fails after retries.

---

## 4. Start with Docker (recommended)

```bash
cd pumpd
docker compose up -d --build
```

The container:

- Listens on **port 8080**
- Persists SQLite data in the Docker volume `pumpd-data`
- Mounts `./config.yaml` read-only
- Loads secrets from `.env`
- Restarts automatically (`unless-stopped`)

### 4.1 Verify startup

```bash
# Health check (always works, no auth required)
curl -s http://localhost:8080/health | jq

# Container logs
docker compose logs -f pumpd

# Container status
docker compose ps
```

A healthy startup shows:

- HTTP **200** from `/health` (or **503** briefly until first forecast/eval completes)
- `"scheduler": "ok"` in health checks
- Forecast age and eval age within limits after ~1 minute

Open the dashboard: **http://localhost:8080/user** (or `/admin` for diagnostics)

Legacy URL `/` redirects to `/user`.

### 4.2 Stop and restart

```bash
docker compose down          # stop containers
docker compose up -d           # start again (no rebuild)
docker compose up -d --build   # rebuild after code changes
```

---

## 5. Start locally (development)

```bash
cd pumpd
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8080
```

SQLite database is created at `./data/pumpd.db`. Run tests with:

```bash
pytest
ruff check app tests
mypy app
```

---

## 6. What happens on first startup

When pumpd starts, it automatically:

1. **Initializes the database** — creates tables and pump state rows if missing
2. **Starts MQTT client** — if `mqtt.enabled: true`; otherwise no-op
3. **Reconciles devices** — reads physical switch state; **device wins** for `device_on` in DB; logs reconciliation events; commands off if DB intent was off but switch was physically on (unless `manual_on`)
4. **Polls forecasts** — Open-Meteo (always); NWS (US locations only)
5. **Starts scheduler** — forecast poll every 30 min (default); rules evaluation every 10 min (default)
6. **Runs first evaluation** — applies rules and may command pumps

If no forecast has ever been fetched, the **stale-forecast watchdog** treats data as stale immediately (pumps stay off until first successful poll).

Without configured pump credentials (empty Tuya keys and no SmartThings PAT), devices are not registered and control commands are skipped — forecast and dashboard still work.

---

## 7. Phase 2 — Rain sensor (optional)

Enable when ESPHome sensor and Mosquitto are ready:

1. Uncomment the `mosquitto` service in `docker-compose.yml`
2. Add a `mosquitto.conf` (allow anonymous or configure credentials)
3. In `config.yaml`:

```yaml
mqtt:
  enabled: true
  host: mosquitto
  port: 1883
  topic: sensors/rain
  min_confidence: 0.8
```

4. Restart: `docker compose up -d --build`

ESPHome should publish JSON to `sensors/rain`, for example:

```json
{"raining": true, "rate_mm_h": 2.5, "confidence": 1.0, "water_present": false}
```

See [operations.md](operations.md) §Phase 2 for sensor behavior.

---

## 8. Boot on server restart (systemd)

Deploy the `pumpd/` directory to a fixed path (e.g. `/opt/pumpd`) and create `/etc/systemd/system/pumpd.service`:

```ini
[Unit]
Description=pumpd rain pump controller
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/pumpd
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable pumpd
sudo systemctl start pumpd
sudo systemctl status pumpd
```

Adjust `WorkingDirectory` to match your deployment path.

---

## 9. Pre-production checklist

Before relying on pumpd for automatic control:

- [ ] Building coordinates set in `config.yaml`
- [ ] Each pump has Tuya IP, device ID, and local key (or SmartThings fallback)
- [ ] Pump switches have **fixed LAN IPs**
- [ ] ntfy topic configured and tested (or SMTP enabled)
- [ ] Dashboard shows **provider health** with recent Open-Meteo fetch
- [ ] Dashboard shows **next 12 h forecast** bars
- [ ] `/health` returns 200 after startup settles
- [ ] Manual test: `POST /api/pumps/{name}/mode` with `manual_on` / `manual_off` / `auto` (see [operations.md](operations.md))
- [ ] Review rules thresholds (`precip_probability_threshold`, `precip_amount_threshold_mm`) for your climate
- [ ] Read [design-caveats.md](design-caveats.md) for known limitations

---

## 10. Quick reference

| URL / command | Purpose |
|---------------|---------|
| http://localhost:8080/user | User dashboard (status, forecast, stop) |
| http://localhost:8080/admin | Admin dashboard (diagnostics, events, overrides) |
| http://localhost:8080/health | Health check (no auth) |
| http://localhost:8080/api/status | JSON status |
| `docker compose logs -f pumpd` | Live logs |
| `docker compose restart pumpd` | Restart after config change |

**Related docs:** [operations.md](operations.md) · [architecture.md](architecture.md) · [design-caveats.md](design-caveats.md)
