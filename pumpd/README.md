# pumpd

Production-quality rooftop rain pump controller. Polls weather forecasts, runs pumps via Tuya local LAN control (SmartThings fallback), and exposes a web dashboard with manual overrides.

**Version:** 1.1.0

## Quick start (Docker)

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# Edit config.yaml with your coordinates and pump details
docker compose up -d --build
```

Open http://localhost:8080

## Local development

```bash
cd pumpd
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp config.example.yaml config.yaml
uvicorn app.main:app --reload --port 8080
pytest
```

## Tuya local key extraction

1. Create a project at [Tuya IoT Platform](https://iot.tuya.com/)
2. Link your Smart Life app account and note device IDs
3. Install tinytuya: `pip install tinytuya`
4. Run the wizard: `python -m tinytuya wizard`
5. Copy each device's `local_key`, `ip`, and `version` into `config.yaml` (or set `TUYA_LOCAL_KEY_NORTH_PUMP` in `.env`)

Pumps must have **fixed IPs** (DHCP reservations) on the same LAN as the server.

## SmartThings PAT

1. Open [SmartThings CLI / Developer Workspace](https://developer.smartthings.com/)
2. Create a Personal Access Token with `r:devices:*` and `w:devices:*`
3. Set `SMARTTHINGS_PAT` in `.env`
4. Add each pump's SmartThings device UUID to `config.yaml` under `smartthings.device_id`

SmartThings is used only when Tuya local control fails.

## Configuration reference

See `config.example.yaml`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `rules.precip_probability_threshold` | 70 | % — any hour in lookahead must exceed |
| `rules.precip_amount_threshold_mm` | 2.0 | Sum of hourly rain mm in lookahead |
| `rules.lookahead_hours` | 2 | Pre-emptive start window |
| `safety.max_continuous_runtime_minutes` | 60 | Dry-run protection |
| `safety.watchdog_stale_forecast_hours` | 3 | Force off if no fresh forecast |

## systemd (boot on startup)

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

## Phase 2 — ESPHome rain sensor

Enable MQTT in `config.yaml` and uncomment Mosquitto in `docker-compose.yml`.

Example ESPHome snippet publishing to `sensors/rain`:

```yaml
mqtt:
  broker: !secret mqtt_broker
  topic_prefix: sensors

sensor:
  - platform: pulse_counter
    pin: GPIO4
    name: "Rain rate"
    unit_of_measurement: "mm/h"
    filters:
      - multiply: 0.2794  # tips to mm/h calibration

json:
  - platform: template
    name: "Rain JSON"
    state_topic: sensors/rain
    json_payload: >-
      {"raining": {{ rate > 0 }}, "rate_mm_h": {{ rate }}, "confidence": 1.0}
```

Optional float switch:

```yaml
binary_sensor:
  - platform: gpio
    pin: GPIO5
    name: "Basin water present"
```

Include `"water_present": true/false` in the MQTT JSON payload.

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | JSON status |
| POST | `/api/pumps/{name}/mode` | `{"mode":"auto"\|"manual_on"\|"manual_off", "approve_safety_override": false}` |
| GET | `/api/events` | Event log |
| GET | `/api/hardware-health` | Pump and sensor abnormality status |
| GET | `/health` | Health check (always unauthenticated) |

## License

MIT
