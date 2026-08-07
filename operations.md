# pumpd — Operations Guide

**Version:** 1.1.0

Day-to-day operation of the rooftop rain pump controller after initial setup. For installation and first boot, see [startup.md](startup.md).

---

## 1. How pumpd decides to run pumps

pumpd evaluates rules every **10 minutes** (default) using forecast data, optional MQTT sensor input, and per-pump state. Decision priority (highest wins):

1. **Safety hard-stops** — max runtime, cooldown, stale forecast, engine watchdog
2. **Manual mode** — `manual_on` / `manual_off` (can override safety **only** with approval; see §4)
3. **Float switch** — `water_present=true` forces run (Phase 2)
4. **MQTT sensor** — authoritative for current rain when confidence ≥ threshold (Phase 2)
5. **Forecast rules** — pre-emptive start, continue while raining, post-rain drain
6. **Default** — off

### Pump phases (auto mode)

```
idle → pre_rain → running → post_rain_drain → idle
```

| Phase | Behavior |
|-------|----------|
| `idle` | Pump off; may pre-emptively start if forecast thresholds met |
| `pre_rain` | Running before rain confirmed; duty cycle may apply |
| `running` | Rain active (forecast or sensor); duty cycle may apply |
| `post_rain_drain` | Runs continuously (no duty cycle) for `post_rain_drain_minutes` after rain ends |
| `idle` | Returns after drain completes or sensor/float dry stop |

Forecast is polled every **30 minutes**. Rules run every **10 minutes**.

---

## 2. Dashboard

Open **http://\<server\>:8080/user** for everyday status, or **/admin** for diagnostics (requires API key when auth is enabled).

The dashboard auto-refreshes every 30 seconds (htmx). It shows:

| Section | Meaning |
|---------|---------|
| **Rain signal** | Current `is_raining`, rate, source (`forecast` or `mqtt`) |
| **Pump cards** | ON/OFF, mode, phase, runtime today, continuous runtime |
| **Next 12 h forecast** | Hourly probability bars |
| **Hardware health** | Pump and sensor abnormality status |
| **Provider health** | Open-Meteo / NWS last success and errors |
| **Recent events** | Audit log of decisions and control actions |

### Override buttons

| Button | Effect |
|--------|--------|
| **Auto** | Return to automated control; clears safety override approval |
| **Manual ON** | Force pump on until auto-revert (default 4 h) |
| **Manual OFF** | Force pump off until auto-revert |

Manual modes auto-revert to `auto` after `manual_revert_hours` (default 4).

---

## 3. REST API

Base URL: `http://<server>:8080`

### Authentication

When `api.auth_enabled: true`, send header on protected routes:

```
X-API-Key: your_api_key
```

| Route | Auth required? |
|-------|----------------|
| `GET /health` | **Never** — use for monitoring even if keys are misconfigured |
| `GET /user`, `GET /partials/user/status` | No (default) |
| `GET /admin`, `GET /partials/admin/status` | Yes, when auth enabled |
| `POST /api/pumps/{name}/mode` | Yes, when auth enabled |
| `GET /api/status`, `/api/events`, `/api/hardware-health` | No (default) |

### Common operations

**Full status:**

```bash
curl -s http://localhost:8080/api/status | jq
```

**Health check:**

```bash
curl -s http://localhost:8080/health | jq
```

Returns **200** when healthy, **503** when degraded (stale forecast/eval, scheduler down, or hardware fault).

**Recent events:**

```bash
curl -s "http://localhost:8080/api/events?limit=50" | jq
curl -s "http://localhost:8080/api/events?pump=north_pump&since=2026-08-01T00:00:00" | jq
```

**Hardware health:**

```bash
curl -s http://localhost:8080/api/hardware-health | jq
```

---

## 4. Manual overrides and safety approval

Manual mode normally overrides forecast automation. **Safety hard-stops** (stale forecast, engine watchdog, max runtime) block manual mode **unless you explicitly approve the override**.

### Set mode via API

```bash
# Normal manual on
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "manual_on"}'

# Return to automation
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "auto"}'
```

### Override an active safety hard-stop

When safety is tripping (e.g. stale forecast watchdog), manual mode returns **422** unless you approve:

```bash
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "manual_on", "approve_safety_override": true}'
```

This is **logged** to the events table and triggers a **high-priority notification**. Use only when you have verified it is safe to run the pump despite the safety condition.

### Lock timeout (409)

If another operation holds the pump lock (scheduler + API share per-pump locks), the API returns **409** after `api.lock_timeout_seconds` (default 30). Retry after a few seconds.

---

## 5. Notifications

pumpd sends alerts via **ntfy** and/or **SMTP** when configured.

Typical alerts:

| Event | Priority |
|-------|----------|
| Pump started/stopped (with reason) | default |
| Stale forecast watchdog | high |
| Engine evaluation watchdog | high |
| Control failure / verify mismatch | high |
| Safety override approved | high |
| Forecast stale but MQTT keeps pumps running | default |

### ntfy

Set `notifications.ntfy.topic` in `config.yaml`. Subscribe on your phone:

```
https://ntfy.sh/your_topic
```

Or use the ntfy app with the same topic.

---

## 6. Monitoring

### Health endpoint

Use `/health` for Docker HEALTHCHECK, uptime monitors, and Home Assistant.

Example degraded response:

```json
{
  "status": "degraded",
  "checks": {
    "db": "ok",
    "scheduler": "ok",
    "last_forecast_age_minutes": 45.2,
    "last_eval_age_minutes": 8.1,
    "engine_watchdog": "ok",
    "hardware_faults": 1
  }
}
```

| Check | Fail when |
|-------|-----------|
| `scheduler` | APScheduler not running |
| `last_forecast_age_minutes` | > 2 × `weather.poll_minutes` (default 60 min) |
| `last_eval_age_minutes` | > 2 × `rules.evaluate_minutes` (default 20 min) |
| `hardware_faults` | Any component in `fault` status |

### Hardware health

The hardware monitor tracks:

| Component | Fault conditions |
|-----------|------------------|
| **Pumps** | Repeated control failures; verify mismatches (switch state ≠ commanded) |
| **MQTT sensor** | No messages when enabled; stale data > `sensor_stale_minutes` |

Status values: `ok`, `degraded`, `fault`. Faults appear on the dashboard and in `/api/hardware-health`.

**Limitation:** pumpd cannot detect a seized pump motor if the smart switch still reports ON. See [design-caveats.md](design-caveats.md).

### Event log

All decisions, control commands, reconciliations, provider disagreements, and safety overrides are written to the `events` table. Query via dashboard or `GET /api/events`.

---

## 7. Phase 2 — Rain sensor and float switch

When MQTT is enabled, the physical sensor **overrides the forecast** for “is it raining now” when `confidence >= mqtt.min_confidence`:

| Condition | Behavior |
|-----------|----------|
| Forecast says rain, sensor says **dry** | Pumps stop (after `sensor_dry_minutes` grace if running) |
| Forecast says dry, sensor says **rain** | Pumps start (including from `idle`) |
| Sensor dry during `post_rain_drain` | Pumps stop — drain is skipped |
| `water_present: true` | Pumps run regardless of rain signal |
| `water_present: false` for `sensor_dry_minutes` | Early stop even if forecast shows rain |

**Stale forecast exception:** if forecasts are stale but MQTT reports rain with sufficient confidence, pumps **stay running** (notification still sent).

### MQTT payload format

Publish JSON to `mqtt.topic` (default `sensors/rain`):

```json
{
  "raining": true,
  "rate_mm_h": 1.8,
  "confidence": 1.0,
  "water_present": false
}
```

Alternate key `is_raining` is also accepted for the rain flag.

---

## 8. Tuning rules and safety

Edit `config.yaml` and restart pumpd (`docker compose restart pumpd`).

| Setting | Default | When to adjust |
|---------|---------|----------------|
| `rules.precip_probability_threshold` | 70% | Lower = more aggressive pre-emptive starts |
| `rules.precip_amount_threshold_mm` | 2.0 mm | Sum of rain in lookahead window |
| `rules.lookahead_hours` | 2 | How far ahead to pre-empt |
| `rules.post_rain_drain_minutes` | 30 | Drain time after rain ends |
| `rules.sensor_dry_minutes` | 10 | Sensor/float dry grace before early stop |
| `rules.manual_revert_hours` | 4 | Manual override duration |
| `rules.duty_cycle` | disabled | Enable to cycle on/off during rain |
| `safety.max_continuous_runtime_minutes` | 60 | Dry-run protection |
| `safety.min_cooldown_minutes` | 15 | Block auto restart after safety trip |
| `safety.watchdog_stale_forecast_hours` | 3 | Force off if no fresh forecast |

After config changes, verify via dashboard and `/api/status`.

---

## 9. Troubleshooting

### Pumps never start

1. Check `/api/status` — are forecasts present? Is phase `idle`?
2. Confirm thresholds: pre-emptive start needs **both** high pop **and** rain sum in lookahead
3. Verify pump `enabled: true` in config
4. Check cooldown — may block auto restart after safety trip
5. Phase 1 only: no MQTT means forecast-only; see [design-caveats.md](design-caveats.md)

### Pumps won't stop

1. Check mode — `manual_on` keeps pump running
2. Check phase — `post_rain_drain` runs until timer completes
3. MQTT sensor reporting rain? Sensor overrides forecast
4. Float switch `water_present: true`?

### Control failures / hardware fault

1. Check `/api/hardware-health`
2. Verify Tuya IP hasn't changed (DHCP)
3. Re-run `python -m tinytuya wizard` if local key rotated
4. Test SmartThings fallback — is `SMARTTHINGS_PAT` valid?
5. Review events: `event_type` of `turn_on` / `turn_off` with failure details

### Stale forecast watchdog

- Forecast APIs unreachable or poll failing
- Check provider health on dashboard
- During outage, pumps turn off after `watchdog_stale_forecast_hours` unless MQTT rain exception applies

### Dashboard returns 401

- Set `X-API-Key` header or disable `api.auth_enabled` for trusted LAN
- `/health` always works without auth — use it to confirm the service is up

### 409 on mode change

- Another command is in progress; wait and retry
- Increase `api.lock_timeout_seconds` if needed

---

## 10. Maintenance

| Task | Frequency | Action |
|------|-----------|--------|
| Review event log | Weekly | `GET /api/events` or dashboard |
| Check `/health` | Automated | Monitor externally |
| Verify ntfy alerts | After config change | Trigger a test notification |
| Tuya key rotation | As needed | Re-run tinytuya wizard, update `.env` |
| Backup SQLite | Monthly | Copy Docker volume `pumpd-data` or `./data/pumpd.db` |
| Update container | As needed | `docker compose pull && docker compose up -d --build` |

### Graceful shutdown

`docker compose down` or SIGTERM:

- Scheduler stops
- Pumps commanded **off** except those in `manual_on`
- In-progress post-rain drain is **aborted** (logged)
- MQTT disconnected

---

## 11. Quick command reference

```bash
# Status
curl -s http://localhost:8080/api/status | jq '.pumps'
curl -s http://localhost:8080/health | jq

# Manual control
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"manual_off"}'

# Safety override (use with caution)
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"manual_on","approve_safety_override":true}'

# Logs (Docker)
docker compose -f pumpd/docker-compose.yml logs -f pumpd --tail=100
```

**Related docs:** [startup.md](startup.md) · [architecture.md](architecture.md) · [design-caveats.md](design-caveats.md)
