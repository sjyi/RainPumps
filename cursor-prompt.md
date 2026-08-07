# Cursor Prompt — Rooftop Rain Pump Controller

**Version:** 1.1.0

Copy everything below the line into Cursor (Agent mode) in an empty repo. Work iteratively: let it scaffold, review, then say "continue".

---

Build a production-quality Python application called **pumpd** that automatically controls rooftop rain-drain pumps based on weather forecasts. It runs on an Ubuntu server via Docker Compose. The pumps are driven by Tuya (Smart Life) smart switches that also appear in SmartThings.

Refer to `architecture.md` v1.1.0 for the full system design and `design-caveats.md` for operational limitations. This prompt is the implementation spec.

## Tech stack (use exactly this)

- Python 3.12, FastAPI + Uvicorn, APScheduler, SQLAlchemy 2.x + SQLite, Alembic, Pydantic v2 for config and models
- tinytuya for local LAN control of Tuya devices (primary control path)
- httpx for the SmartThings REST API (fallback control path) and weather APIs
- paho-mqtt for a rain-sensor input (Phase 2, build the interface now as a no-op stub when disabled)
- pytest + pytest-asyncio; ruff + mypy; Dockerfile + docker-compose.yml
- Jinja2 server-rendered dashboard (no JS framework; htmx allowed for auto-refresh via `GET /partials/status`)

## Repository layout

```
pumpd/
  app/
    main.py              # FastAPI app, lifespan starts scheduler + service
    config.py            # Pydantic Settings; loads config.yaml + .env for secrets
    db.py                # SQLAlchemy engine + session factory
    models.py            # SQLAlchemy models
    service.py           # Core orchestration: forecast ingest, evaluation, device control
    hardware_health.py   # Pump/sensor health tracking + CommandLockError
    scheduler.py         # APScheduler jobs
    weather/
      base.py            # WeatherProvider ABC -> HourlyForecast[]
      open_meteo.py      # primary, no API key (precipitation_probability, rain)
      nws.py             # secondary, US only, auto-skip outside US bbox
    devices/
      base.py            # PumpDevice ABC: turn_on(), turn_off(), get_state()
      tuya_local.py      # tinytuya adapter (device_id, ip, local_key, version)
      smartthings.py     # cloud adapter (PAT token, device_id)
      composite.py       # tries tuya_local first (retry x3), falls back to smartthings, verifies state
    signals/
      base.py            # RainSignal ABC -> RainState(is_raining, rate_mm_h, confidence, source, ts)
      forecast_signal.py # derives RainState from stored forecast
      mqtt_signal.py     # Phase 2: subscribes to MQTT topic for tipping-bucket data
    engine.py            # rules engine (pure functions, fully unit-testable)
    safety.py            # max-runtime, cooldown, watchdog enforcement
    notify.py            # ntfy POST and/or SMTP
    web/
      routes.py          # dashboard + REST + htmx partials
      templates/         # Jinja2 templates
  alembic/               # SQLite schema migrations
  tests/
  config.example.yaml
  .env.example
  pyproject.toml
  docker-compose.yml     # pumpd + mosquitto (mosquitto commented out until Phase 2)
  Dockerfile
  README.md              # setup incl. tinytuya wizard, SmartThings PAT, systemd boot
```

## Configuration (config.yaml, validated by Pydantic)

```yaml
timezone: America/New_York

location: { latitude: 0.0, longitude: 0.0 }

weather:
  poll_minutes: 30
  providers: [open_meteo, nws]   # nws auto-skipped outside US

rules:
  evaluate_minutes: 10
  precip_probability_threshold: 70     # %
  precip_amount_threshold_mm: 2.0      # sum of hourly rain within lookahead
  lookahead_hours: 2
  post_rain_drain_minutes: 30
  sensor_dry_minutes: 10               # Phase 2: stop early after sensor dry this long
  manual_revert_hours: 4               # manual_on/off auto-revert to auto
  duty_cycle: { enabled: false, on_minutes: 10, off_minutes: 20 }

safety:
  max_continuous_runtime_minutes: 60
  min_cooldown_minutes: 15
  watchdog_stale_forecast_hours: 3     # no fresh forecast -> pumps off + alert (see MQTT exception)

pumps:
  - name: north_pump
    enabled: true
    tuya: { device_id: "", ip: "192.168.1.50", local_key: "", version: 3.4 }
    smartthings: { device_id: "" }

notifications:
  ntfy: { enabled: true, url: "https://ntfy.sh", topic: "" }
  smtp:
    enabled: false
    host: ""
    port: 587
    username: ""
    from_addr: ""
    to_addrs: []

mqtt:            # Phase 2
  enabled: false
  host: mosquitto
  port: 1883
  topic: sensors/rain
  min_confidence: 0.8    # required to override stale-forecast watchdog

api:
  auth_enabled: false
  api_key: ""            # or API_KEY in .env; header X-API-Key
  lock_timeout_seconds: 30   # per-pump asyncio lock; 409 on timeout

hardware_monitor:
  enabled: true
  sensor_stale_minutes: 15
  pump_failure_threshold: 3       # consecutive failures -> fault
  verify_mismatch_threshold: 2    # consecutive verify mismatches -> fault

logging:
  json: false            # structured JSON logs when true (YAML key is `json`, not json_logs)

database_url: sqlite:///./data/pumpd.db
```

Secrets may alternatively come from `.env`:

```
SMARTTHINGS_PAT=
API_KEY=
SMTP_PASSWORD=
TUYA_LOCAL_KEY_NORTH_PUMP=
```

## Decision priority ladder

Highest priority wins:

1. Safety hard-stops (max runtime, cooldown after safety trip, stale-forecast watchdog, engine-eval watchdog) — overridable in manual mode only with explicit `approve_safety_override=true` on the mode-change API (logged + notified; see `design-caveats.md`)
2. Manual mode (`manual_on` / `manual_off`, auto-revert after `manual_revert_hours`)
3. Phase 2 float switch (`water_present=true` forces run; `water_present=false` allows early stop after `sensor_dry_minutes`, including during `post_rain_drain`)
4. Phase 2 MQTT sensor (`is_raining` for current conditions; early stop after `sensor_dry_minutes` dry, including during `post_rain_drain`)
5. Forecast rules (pre-emptive start, continue, post-rain drain)
6. Default off

**Watchdog vs MQTT exception:** if MQTT enabled and sensor reports `is_raining=true` with `confidence >= mqtt.min_confidence`, pumps stay running even when forecasts are stale (still notify). Otherwise stale forecast triggers off.

**MQTT vs forecast for current rain:** when MQTT is enabled and confidence ≥ `mqtt.min_confidence`, the sensor overrides forecast for `is_raining` / `rate_mm_h`. Forecast still drives **pre-emptive starts** even when the sensor is dry.

## Rules engine (pure functions in engine.py)

Implement per `architecture.md` §6. All I/O mocked in tests.

```python
@dataclass(frozen=True)
class HourlyForecast:
    hour_ts: datetime
    pop_pct: float
    rain_mm: float

@dataclass(frozen=True)
class RainState:
    is_raining: bool
    rate_mm_h: float
    confidence: float  # 0.0–1.0
    source: Literal["forecast", "mqtt"]   # never "composite"
    ts: datetime
    water_present: bool | None = None     # Phase 2 float switch

@dataclass
class PumpPhase:
    name: str
    enabled: bool
    phase: Literal["idle", "pre_rain", "running", "post_rain_drain"]
    mode: Literal["auto", "manual_on", "manual_off"]
    device_on: bool
    duty_on: bool                         # current duty-cycle position
    runtime_continuous_min: int
    cooldown_until: datetime | None
    manual_revert_at: datetime | None
    post_rain_drain_started_at: datetime | None = None
    sensor_dry_since: datetime | None = None
    duty_cycle_started_at: datetime | None = None
    safety_tripped: bool = False
    safety_override_approved: bool = False

@dataclass(frozen=True)
class SafetyFlags:
    stale_forecast: bool = False
    engine_watchdog: bool = False
    mqtt_stale_override: bool = False     # MQTT rain keeps pumps on despite stale forecast

@dataclass(frozen=True)
class PumpCommand:
    pump_name: str
    action: Literal["turn_on", "turn_off", "no_op"]
    reason: str
    new_phase: Literal["idle", "pre_rain", "running", "post_rain_drain"] | None = None
    notify: bool = False

def evaluate(
    *,
    now: datetime,
    rain: RainState,
    forecast_window: list[HourlyForecast],  # next lookahead_hours (+ buffer for rain signal)
    pumps: list[PumpPhase],
    rules: RulesConfig,
    safety: SafetyFlags,
    max_runtime_minutes: int,
    min_cooldown_minutes: int = 15,
    mqtt_min_confidence: float = 0.8,
) -> EvaluateResult:  # commands: list[PumpCommand], pumps: list[PumpPhase]
    ...
```

**Pre-emptive start:** within the next `lookahead_hours`, if **any single hour** has pop ≥ threshold **and** **sum of hourly rain mm** in the window ≥ `precip_amount_threshold_mm` → start enabled pumps in `auto`.

**Rain signal — forecast-only:** `is_raining=true` if any hour in current + next 1 h has pop ≥ threshold OR derived rate ≥ 0.1 mm/h.

**Effective rain (MQTT enabled):** `effective_raining()` — MQTT overrides forecast for current conditions when `source=="mqtt"` and `confidence >= mqtt.min_confidence`; otherwise fall back to forecast-derived signal.

**Continue:** while `is_raining=true` (effective), keep running. Duty cycle applies **only** in `pre_rain` and `running` phases.

**Post-rain drain:** when effective rain clears, run **continuously** (no duty cycle) for `post_rain_drain_minutes`, then stop. Track via `post_rain_drain_started_at`.

**Sensor/float early stop:** when MQTT reports dry (authoritative) or float reports `water_present=false`, start `sensor_dry_since` timer; after `sensor_dry_minutes`, stop immediately — **including aborting in-progress `post_rain_drain`**.

**Per-pump phase state machine:** `idle → pre_rain → running → post_rain_drain → idle`. Each pump has independent phase, mode, runtime, cooldown.

**Multi-provider:** normalize Open-Meteo and NWS to `HourlyForecast`; for start decisions use the **more conservative** values (higher pop, higher rain sum). Log both sources.

## Behavior requirements

1. **Forecast job:** poll providers on schedule; store hourly pop + rain mm in SQLite; update `provider_health`. On startup with no forecast ever fetched, treat as stale immediately.
2. **Rules evaluation** every `evaluate_minutes`; update `engine_last_eval_at`. Pure function of inputs → decisions.
3. **Control path:** composite adapter tries Tuya local (retry ×3), falls back to SmartThings. After every command, verify via the adapter that succeeded; on verify failure retry once via alternate adapter. Log + notify on mismatch or total failure. Record success/failure in `hardware_health`. One pump failing does not block others.
4. **Concurrency:** asyncio lock per pump serializes scheduler, safety, and API commands. Lock acquisition uses `api.lock_timeout_seconds`; on timeout raise `CommandLockError` → HTTP **409**.
5. **Modes per pump:** `auto` / `manual_on` / `manual_off`. Manual overrides automation; auto-revert after `manual_revert_hours`. When a safety hard-stop is active, `manual_on` / `manual_off` require `approve_safety_override=true` in the request body; reject with 422 otherwise. Approval is stored on `pump_state.safety_override_approved`, logged to `events`, and notified.
6. **Safety:** max continuous runtime per pump; cooldown after safety trip (does not block `manual_on`, but max-runtime still applies once on); stale-forecast watchdog; engine-eval watchdog if no evaluation within `2 × evaluate_minutes`. All decisions/commands → `events` table.
7. **Hardware monitoring** (`hardware_health.py`): track per-pump control failures and verify mismatches (`pump_failure_threshold`, `verify_mismatch_threshold` → `degraded` / `fault`); track MQTT sensor freshness (`sensor_stale_minutes`). Expose on dashboard and `/api/hardware-health`. `/health` fails (503) when any component is in `fault`.
8. **Dashboard** (`/`): pump cards (state, mode, phase, runtime today, override buttons), rain signal, provider health, hardware health, next-12 h forecast bar, recent events. htmx: `GET /partials/status` refreshes every 30 s.
9. **Startup reconciliation** (in `service.startup()`):
   - Start MQTT signal (no-op stub when disabled).
   - Ensure `pump_state` rows exist for configured pumps.
   - For each pump, read physical device state via composite adapter.
   - **Device wins** for `device_on` when physical state disagrees with DB; log `reconcile` event.
   - Restore modes and phases from DB (not overwritten by reconciliation).
   - If pump is not in `manual_on`, DB intent is off, but device is physically on → command off and log reconcile event.
   - Poll forecasts immediately.
10. **Shutdown:** stop scheduler; command all pumps off except `manual_on`; abort in-progress post-rain drain (log it); flush DB; disconnect MQTT.

## Database schema (SQLAlchemy + Alembic)

- `forecasts(provider, hour_ts, pop_pct, rain_mm, fetched_at)`
- `provider_health(provider, last_ok_at, last_error, last_error_at)`
- `pump_state(name, phase, mode, device_on, duty_on, runtime_continuous_min, cooldown_until, manual_revert_at, runtime_today_min, post_rain_drain_started_at, sensor_dry_since, duty_cycle_started_at, safety_override_approved, updated_at)`
- `events(id, ts, pump_name, event_type, reason, details_json)`
- `engine_meta(key, value)` — e.g. `engine_last_eval_at`
- `hardware_health(component_id, component_type, status, last_ok_at, last_error, last_error_at, consecutive_failures, details_json)` — `component_type`: `pump` | `sensor`; `status`: `ok` | `degraded` | `fault`

Runtime today uses configured `timezone`.

## REST API

| Method | Path | Auth | Notes |
| :---- | :---- | :---- | :---- |
| GET | `/` | optional | Dashboard |
| GET | `/partials/status` | optional | htmx partial |
| GET | `/api/status` | optional | pumps, rain, forecast 12 h, provider health, hardware health |
| POST | `/api/pumps/{name}/mode` | required if auth on | body: `{"mode":"auto"\|"manual_on"\|"manual_off","approve_safety_override":false}` |
| GET | `/api/events` | optional | query: `limit` (default 50, max 500), `pump`, `since` |
| GET | `/api/hardware-health` | optional | JSON: component health summary |
| GET | `/health` | **none** | always unauthenticated; 200 if db + scheduler ok, forecast/eval ages within limits, no hardware faults; else 503 |

Errors: `404` unknown pump; `409` command lock timeout; `422` validation (invalid mode, safety override required).

### Health endpoint

`GET /health` returns:

```json
{
  "status": "ok",
  "checks": {
    "db": "ok",
    "scheduler": "ok",
    "last_forecast_age_minutes": 12,
    "last_eval_age_minutes": 3,
    "engine_watchdog": "ok",
    "hardware_faults": 0
  }
}
```

- `scheduler`: APScheduler running and jobs registered.
- `last_forecast_age_minutes`: age of newest forecast row (fail if > `poll_minutes × 2`).
- `last_eval_age_minutes`: age of `engine_last_eval_at` (fail if > `2 × evaluate_minutes`).
- `hardware_faults`: count of components with `status == "fault"`.
- Docker `HEALTHCHECK` should call this endpoint.

## Quality bar

- Unit tests for rules engine: pre-emptive start (any-hour + sum aggregation), duty cycle (not during post_rain_drain), post-rain drain completion, sensor-overrides-forecast, sensor-dry early stop (including during post_rain_drain), float dry early stop during post_rain_drain, stale-forecast watchdog, watchdog+MQTT exception, max-runtime cutoff, cooldown vs manual_on, cooldown blocks auto start, manual revert, manual_on with safety override bypasses stale forecast, stale forecast overrides manual_on without approval, mqtt dry overrides forecast rain, mqtt starts from idle, multi-pump independent behavior, forecast_is_raining current hour. Mock all I/O.
- Unit tests for hardware health: pump success resets consecutive failures; consecutive control failures reach degraded then fault at threshold; verify mismatch fault at separate threshold; sensor stale detection when no messages or age > `sensor_stale_minutes`; sensor message updates health to ok.
- Integration tests: composite adapter retry/verify/fallback (mocked devices); command lock timeout returns 409.
- Type hints throughout; mypy and ruff clean.
- Structured logging (JSON when `logging.json=true`) with decision reasons ("started north_pump: 82% prob, 4.1mm sum in 2h").
- Docker: `HEALTHCHECK` calling `/health`; restart policy; SQLite on a named volume.
- README: docker compose quickstart, tinytuya wizard for local keys, SmartThings PAT setup, config reference, systemd unit example, Phase 2 rain-sensor section (ESPHome tipping-bucket YAML publishing to `sensors/rain`, optional float switch). Reference `design-caveats.md` for deployment limitations.

Start by scaffolding the repo, config models, DB schema, and the rules engine with its tests. Then implement weather providers, device adapters, hardware health, service orchestration, scheduler, safety, and finally the web dashboard.
