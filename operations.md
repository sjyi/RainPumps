# pumpd — Operations Guide

**Version:** 1.2.0

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

Forecast is polled every **10 minutes** (default `weather.poll_minutes`). **Current conditions** (live observation for “is it raining now”) are polled every **10 minutes** (`weather.current_poll_minutes`). Rules run every **10 minutes**.

### Weather providers

pumpd can use multiple forecast providers (default: AccuWeather, Open-Meteo, NWS). The dashboard **display provider** (`weather.display_provider`, default AccuWeather) controls which provider’s hourly bars and current observation are shown.

| Provider | Role |
|----------|------|
| **AccuWeather** | Hourly forecast + current conditions when `ACCUWEATHER_API_KEY` is set |
| **Open-Meteo** | Hourly forecast fallback |
| **NWS** | US National Weather Service hourly forecast |

Current observation from AccuWeather can override the hourly forecast for the live rain signal when fresh enough. Set `ACCUWEATHER_API_KEY` in `.env` or environment (see [startup.md](startup.md)).

---

## 2. Dashboard

Open **http://\<server\>:8080/user** for everyday status, or **/admin** for diagnostics and configuration (requires API key when auth is enabled).

Both pages auto-refresh every 30 seconds (htmx).

### User dashboard (`/user`)

| Section | Meaning |
|---------|---------|
| **Toolbar** | **Check device connections**, global **Auto**, global **Drain** (see §12) |
| **Rain signal** | Current `is_raining`, rate, source (`forecast`, `current`, or `mqtt`) |
| **Pump cards** | ON/OFF, mode, phase, runtime today, continuous runtime |
| **Next 12 h forecast** | Hourly probability bars from the display provider |
| **Per-pump controls** | Auto / Manual ON / Manual OFF with optional duration |

### Admin dashboard (`/admin`)

Everything on the user dashboard, plus:

| Section | Meaning |
|---------|---------|
| **Import pumps** | Discover/import devices, fleet sync, clear local devices (§11) |
| **Device & switch names** | Edit display names; optional cloud push (§13) |
| **Max runtime** | Per-device/switch continuous runtime limits (§13) |
| **Device display order** | Drag-and-drop order on User and Admin panels |
| **Location** | Map pin, address, geocode search |
| **Display units** | Imperial (°F, in) or metric (°C, mm) |
| **Command verification** | Delay before re-reading switch state after commands |
| **Email notifications** | SMTP and Gmail OAuth setup |
| **Test auto mode** | Rain simulation without real weather (§14) |
| **History** | 7-day control timeline chart + forecast snapshot table |
| **Hardware health** | Pump and sensor abnormality status |
| **Provider health** | AccuWeather / Open-Meteo / NWS last success and errors |
| **Recent events** | Audit log of decisions and control actions |

### Per-pump override buttons

| Button | Effect |
|--------|--------|
| **Auto** | Return that pump to automated control; clears safety override approval |
| **Manual ON** | Force pump on until revert (duration or until next auto control) |
| **Manual OFF** | Force pump off until revert |

Manual duration defaults to `rules.manual_revert_minutes` (default **5 minutes**). You can choose **For duration above** or **Until next automatic control** before applying manual ON/OFF. Legacy configs with `manual_revert_hours` are migrated automatically (e.g. `4` → 240 minutes).

The **global Auto** button on the user toolbar returns **all** pumps from manual mode to auto in one step (`POST /api/auto/start`).

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
| `POST /api/devices/mode` | Yes, when auth enabled |
| `POST /api/drain/start`, `POST /api/auto/start` | No (default) |
| `POST /api/devices/probe-status` | No (default) |
| `POST /api/pumps/sync-fleet` | Yes, when auth enabled |
| `POST /api/devices/auto-import`, `/import`, `/clear-local` | Yes, when auth enabled |
| `DELETE /api/pumps/{name}` | Yes, when auth enabled |
| `GET/POST /api/config/*` | Yes, when auth enabled |
| `GET/POST /api/simulate/*` | Yes, when auth enabled |
| `GET /api/history/*` | Yes, when auth enabled |
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

### User controls

```bash
# Post-rain drain for all eligible auto pumps
curl -X POST http://localhost:8080/api/drain/start

# Return all manual pumps to automatic control
curl -X POST http://localhost:8080/api/auto/start

# Force cloud/LAN probe of all pumps (updates online status)
curl -X POST http://localhost:8080/api/devices/probe-status
```

### Device group mode

Set manual ON/OFF for every switch on one physical device (multi-outlet plug):

```bash
curl -X POST http://localhost:8080/api/devices/mode \
  -H "Content-Type: application/json" \
  -d '{"device_backend":"meross","device_id":"abc123","mode":"manual_on","manual_until_auto":true}'
```

### Config (Admin)

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/config/location` | Latitude, longitude, address |
| `GET/POST /api/config/display` | Imperial vs metric units |
| `GET/POST /api/config/runtime` | Max continuous runtime overrides |
| `GET/POST /api/config/names` | Device/switch display names; optional `propagate_cloud` |
| `GET/POST /api/config/device-order` | Pump card sort order |
| `GET/POST /api/config/command-verify` | Post-command verify delay |
| `GET/POST /api/config/notifications` | SMTP, ntfy, admin email |
| `GET /api/geocode/search?q=…` | Address search for location picker |
| `GET /api/geocode/reverse?lat=…&lon=…` | Reverse geocode for map pin |

### History and simulation (Admin)

```bash
curl -s http://localhost:8080/api/history/timeline | jq
curl -X POST http://localhost:8080/api/simulate/auto-rain/start
curl -s http://localhost:8080/api/simulate/status | jq
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

# Manual ON for 30 minutes
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "manual_on", "manual_duration_minutes": 30}'

# Manual ON until the next automatic rain/drain cycle
curl -X POST http://localhost:8080/api/pumps/north_pump/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "manual_on", "manual_until_auto": true}'
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

pumpd sends alerts via **ntfy** and/or **SMTP** when configured. Configure in **Admin → Email notifications** or `config.yaml`.

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

### SMTP and Gmail OAuth

Enable SMTP in config or Admin. For Gmail without an app password, use **Connect Gmail** in Admin — OAuth tokens are stored locally after `/api/auth/google/gmail/callback`. Disconnect with **Admin** or `POST /api/auth/google/gmail/disconnect`.

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

All decisions, control commands, reconciliations, provider disagreements, safety overrides, device imports, and fleet syncs are written to the `events` table. Query via dashboard or `GET /api/events`.

| Event type | Meaning |
|------------|---------|
| `device_import` | Pumps added or updated in config via Admin import |
| `device_fleet_sync` | New or idle pumps aligned to the current auto program |
| `manual_drain_start` | User clicked **Drain** — post-rain drain started for listed pumps |
| `manual_revert_all` | User clicked global **Auto** — pumps returned from manual mode |
| `display_name_cloud` | Cloud rename attempt after **Push names to cloud** (see §13) |
| `mode_change` | Pump mode changed (auto, manual_on, manual_off) |

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
| `rules.manual_revert_minutes` | 5 | Default manual override duration |
| `rules.duty_cycle` | disabled | Enable to cycle on/off during rain |
| `safety.max_continuous_runtime_minutes` | 180 | Dry-run protection (override per device in Admin) |
| `safety.min_cooldown_minutes` | 15 | Block auto restart after safety trip |
| `safety.watchdog_stale_forecast_hours` | 3 | Force off if no fresh forecast |
| `devices.control_mode` | auto | `local`, `cloud`, or `auto` command path |
| `devices.switch_stagger_seconds` | 30 | Delay between outlets on multi-outlet plugs |
| `devices.command_verify_delay_seconds` | 15 | Wait before reading switch state after command |
| `weather.display_provider` | accuweather | Dashboard forecast/current source |
| `display.units` | imperial | `imperial` or `metric` |

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

### New pump out of sync with others

If an existing pump is running in auto but a **newly imported** switch stays off (or lags by one evaluation cycle):

1. Confirm the rest of the fleet is in an active auto phase (`pre_rain`, `running`, or `post_rain_drain`) — not `idle`
2. In **Admin → Import pumps**, click **Sync pumps to auto program**
3. Or call `POST /api/pumps/sync-fleet` (see §11)
4. Check **Recent events** for `device_fleet_sync` and any control failures

If the fleet is idle, new pumps correctly stay idle until the next rain/pre-emptive start.

### Display names revert or cloud push fails

1. **Local names** are stored in `config.yaml` (`device_labels`, per-pump `label`). Saving in Admin should persist immediately — if names revert, check Docker logs for config write errors (read-only mount).
2. **Push names to cloud** may report `0 ok, N failed` — Meross public API does not persist renames (`/v1/Device/devInfo` is read-only). Tuya/SmartThings may succeed when credentials allow.
3. Expand the trace under the save result in Admin, or filter logs: `display_name_cloud|meross.*rename`.
4. For Meross, rename in the Meross app; use pumpd Admin for **local display names only**.

### Drain button does nothing

1. Pumps must be in **auto** mode (not manual ON/OFF)
2. Skipped pumps are listed in the API response (`skipped` with reason)
3. Check **Recent events** for `manual_drain_start`
4. Phase becomes `post_rain_drain`; runs up to `post_rain_drain_minutes`

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

## 11. Device import and fleet sync

Use **Admin → Import pumps** to discover switches from SmartThings, Tuya, and Meross and merge them into `config.yaml`. Credential setup is in [startup.md](startup.md) §Device import.

| Action | Effect |
|--------|--------|
| **Sync all devices** | Discover everything and import in one step (`POST /api/devices/auto-import`) |
| **Discover only** | List devices without writing config |
| **Import selected** | Import checked rows from the discovery table |
| **Sync pumps to auto program** | Align idle pumps with the current fleet auto state (see below) |

### Automatic sync when importing new devices

When you import a **new** pump while other pumps are already in an **active auto program**, pumpd aligns the newcomer with the fleet:

1. Saves the new pump to `config.yaml` and creates its SQLite state row
2. Reconciles **existing** pumps only (avoids forcing a new switch off before sync)
3. Copies the fleet auto context from a reference pump: phase (`pre_rain`, `running`, or `post_rain_drain`), duty-cycle state, post-rain drain timer, and related fields
4. Waits for new Meross devices to enroll in the cloud session (if applicable)
5. Runs **evaluation immediately** so the new switch is commanded ON or OFF like its peers

This avoids waiting up to `rules.evaluate_minutes` (default 10 min) for the scheduled evaluation cycle.

**When sync does not run:** if every auto pump is in `idle` (no active rain/pre-empt/drain program), new pumps stay `idle` — there is nothing to copy.

**Manual pumps are not affected** — only pumps in `auto` mode are synced.

### Manual fleet sync

If a pump was imported earlier and missed automatic sync, or still shows `idle` while siblings are running:

- **Admin:** **Import pumps → Sync pumps to auto program**
- **API:**

```bash
# Sync all auto pumps that are idle while the fleet is active
curl -X POST http://localhost:8080/api/pumps/sync-fleet \
  -H "Content-Type: application/json" \
  -d '{}'

# Sync specific pumps only
curl -X POST http://localhost:8080/api/pumps/sync-fleet \
  -H "Content-Type: application/json" \
  -d '{"pumps": ["new_pump_sw1", "new_pump_sw2"]}'
```

The API response includes `synced` (pump names), `reference_pump`, and `reference_phase`. Auto-import responses also include a `fleet_sync` object when sync ran.

### Logs and events

After import or manual sync, check:

- **Recent events** on Admin — look for `device_fleet_sync` (details list synced pumps and reference phase)
- **Docker logs** — `docker compose logs pumpd | rg device_fleet_sync`

---

## 12. User dashboard controls

The toolbar at the top of **User** (and Admin pump panels) provides fleet-wide actions.

| Control | API | Behavior |
|---------|-----|----------|
| **Check device connections** | `POST /api/devices/probe-status` | Forces a cloud/LAN probe; updates online/offline badges and syncs live switch ON/OFF into state |
| **Auto** | `POST /api/auto/start` | Returns every enabled pump from `manual_on` / `manual_off` to `auto`; clears manual revert timers; runs evaluation |
| **Drain** | `POST /api/drain/start` | Starts **post-rain drain** on all eligible auto pumps: sets phase to `post_rain_drain`, turns ON pumps that are off, runs evaluation |

**Drain** skips pumps that are disabled, in manual mode, or missing state. It does not override manual ON/OFF. Duration is `rules.post_rain_drain_minutes` (default 30). Manual drain is exempt from the stale-forecast watchdog while draining.

**Auto** (global) differs from per-pump **Auto**: it only affects pumps currently in manual mode; pumps already in auto are unchanged.

---

## 13. Admin configuration and device management

### Display names

On Admin pump cards, click the pencil icon to edit **device name** and **switch/display name**. **Save** writes to `config.yaml`. **Push names to cloud** (optional checkbox) attempts Tuya, Meross, and SmartThings renames.

| Backend | Cloud push |
|---------|------------|
| **Tuya / SmartThings** | May succeed when cloud credentials are configured |
| **Meross** | **Not supported** — API accepts requests but does not persist names. Rename in the Meross app |

After save, check the expandable **trace** for per-device results. Events of type `display_name_cloud` appear in **Recent events**. Docker log lines are prefixed `display_name_cloud`.

Explicit labels you set in Admin are not overwritten by the next Meross/Tuya import sync.

### Location and units

**Admin → Location:** set coordinates via map, address search, or manual entry. Saved to `config.location` and used for all weather providers.

**Admin → Display units:** toggle imperial vs metric for temperatures and precipitation on dashboards and APIs.

### Max runtime

**Admin → Max runtime:** system default (3 h) plus optional per-device and per-switch overrides. Pumps turn off after continuous runtime exceeds the effective limit (switch > device > system).

### Device display order

Drag devices in **Admin → Device display order** and **Save**. Order applies to User and Admin pump card groups.

### Command verification

After sending ON/OFF, pumpd waits `devices.command_verify_delay_seconds` (configurable in Admin) then re-reads switch state. Mismatches count toward hardware fault thresholds. Max retry attempts are fixed in config (`command_verify_max_attempts`).

### Email notifications

**Admin → Email notifications:** configure ntfy topic, SMTP, admin email, and public base URL. **Connect Gmail** uses Google OAuth (`/api/auth/google/gmail/start`) for SMTP without storing an app password in config.

Send a test notification from Admin or `POST /api/config/notifications/test`.

### Remove pumps and clear devices

| Action | API | Effect |
|--------|-----|--------|
| Remove one pump | `DELETE /api/pumps/{name}` | Removes pump from config and SQLite state |
| Clear all local devices | `POST /api/devices/clear-local` | Removes all pumps from config; does **not** unlink cloud accounts |

Re-import with **Sync all devices** when ready.

### Device control mode

`devices.control_mode` in config: `local` (LAN first), `cloud` (Tuya/Meross cloud + SmartThings), or `auto` (try all paths). Multi-outlet plugs stagger switch commands by `switch_stagger_seconds`.

---

## 14. Testing and history

### Rain simulation (Admin)

**Admin → Test auto mode → Start rain simulation** runs a ~2 minute scripted forecast: ~45 s simulated rain, then dry, then ~1 min post-rain drain. Sends **real pump commands** — use only with pumps set to **Auto** and when it is safe to run them.

| API | Purpose |
|-----|---------|
| `POST /api/simulate/auto-rain/start` | Start simulation |
| `POST /api/simulate/auto-rain/stop` | Cancel in progress |
| `GET /api/simulate/status` | Current simulation phase |

### History timeline

**Admin → History** shows a horizontal timeline of pump ON/OFF and phases for the last 7 days. **Now** is on the right; zoom and pan with controls. Data from `GET /api/history/timeline`. Forecast snapshot table uses `GET /api/history/forecasts`.

---

## 15. Logs

pumpd logs to **stdout** (Docker container logs), not a file in the repo.

```bash
cd pumpd && docker compose logs -f pumpd
```

Useful filters:

```bash
docker compose logs pumpd | rg 'display_name_cloud|meross.*rename|device_fleet_sync|manual_drain'
```

**Recent events** on Admin duplicates many audit entries (`mode_change`, `display_name_cloud`, `device_fleet_sync`, control failures, etc.). For name-change debugging, prefer the Admin save **trace** plus `display_name_cloud` log lines.

---

## 16. Quick command reference

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

# Sync idle pumps to current auto program
curl -X POST http://localhost:8080/api/pumps/sync-fleet \
  -H "Content-Type: application/json" \
  -d '{}'

# User toolbar
curl -X POST http://localhost:8080/api/drain/start
curl -X POST http://localhost:8080/api/auto/start
curl -X POST http://localhost:8080/api/devices/probe-status

# Logs (Docker)
docker compose -f pumpd/docker-compose.yml logs -f pumpd --tail=100
```

**Related docs:** [startup.md](startup.md) · [architecture.md](architecture.md) · [design-caveats.md](design-caveats.md)
