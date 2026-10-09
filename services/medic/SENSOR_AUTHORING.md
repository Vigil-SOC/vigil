# Writing a Medic sensor

For D6 and every later sensor. A sensor **reads one thing and returns what it saw**. The framework (`sensors/`) does everything else: scheduling, timeouts, ids, timestamps, `sensor_health`, redaction, schema checks, and writing. If you find yourself doing one of those in a sensor, stop.

## 1. The interface

```python
@dataclass
class MySensor:
    id: str = "daemon_health"  # ^[a-z][a-z0-9_.-]{0,63}$, stable across restarts
    service: str = "soc-daemon"  # observation.schema.json $defs.service
    interval_s: int = 30  # one of 30, 60, 300, 21600 (C7 §3); fixed
    timeout_s: float = 5.0  # hard cap per collect(); < interval_s
    covers: tuple[str, ...] = ("daemon_health",)  # ids from contracts/signal_ids.json
    uses_vigil_api: bool = False  # True if it reads Vigil's API (via the gateway)

    async def collect(self, ctx: SensorContext) -> Sequence[Reading]: ...
```

`check_sensor()` refuses an unknown signal, a non-C7 interval, a timeout ≥ interval or a bad id when the sensor is registered. Return one `Reading` per signal read. Build values with `Value.counter/gauge/flag/enum/timestamp/text` (`None` = **absent**: the source didn't have the field). Counters need `Reading.epoch` (D3: it changes when the producer restarts).

## 2. Outcome: result or error?

This choice matters most, because the engine treats the two differently (semantics §2: an `error` read makes `latest` unknown).

- **The source answered, even if it said "down": that's a result.** Return values with `error=None`. 503, connection refused and "0 records" are results. Results never back off (C7 §4).
- **Medic couldn't make the read: that's an error.** Return `Reading(signal, service, error=ReadError(cls, ...))` with no values. Classes: `timeout`, `refused`, `dns`, `tls`, `http_status`, `auth`, `parse`, `too_large`, `not_installed`, `other`. Three error cycles in a row read `blind`, and then the scheduler backs off (×2 up to 4 × interval, ≤ 5 min).
- **401/403 from Vigil = `ReadError("auth", http_status=…)`.** K1 T-09: while Redis is down, *every* authenticated read is 401. Record it; a rule decides it's a Redis fault (D3). Don't guess in the sensor.
- `ReadError.detail` is untrusted text: keep it short (≤ 500 chars after redaction). Never put a response body, a traceback or a Docker `inspect` body in it.

## 3. Self-health: what the framework does for you

| Your sensor… | Within one 15 s tick the framework writes |
|---|---|
| returns readings | your samples + `sensor_health` (`reported_by: sensor`) |
| raises | an `outcome: error` sample per `covers` signal, `error.class: other`, detail = **exception type only**; `sensor_health` (`reported_by: framework`) |
| runs past `timeout_s` | the call is cancelled; same as above with `error.class: timeout` |
| ignores cancellation | it is never started twice; after 3 intervals with no finished cycle, `state: stopped` |

States: `ok` · `degraded` (some reads failed, or drops) · `blind` (3 failed cycles) · `stopped`. Built-in rule `watcher.sensor-blind` turns `blind`/`stopped` into an incident after 2 min. `sensor_health` is written at least once per interval, also during backoff. Counters (`reads`, `failures`, `dropped`, `redactor_failures`) are per run.

## 4. Rules a sensor must not break

1. **`collect()` is async and never blocks.** Use `httpx.AsyncClient` (with `trust_env=False`); wrap any unavoidable blocking call in `asyncio.to_thread`. A blocking call stalls every sensor.
2. **Read-only.** No write clients, no POST/PUT/DELETE, no Docker/K8s verbs beyond get/list/watch (C3).
3. **Don't read bodies you don't need, and cap the ones you do.** `http_ready` never reads the body.
4. **No secrets in logs, and no `extra=` on log calls.** Log through the message with `%`-style args. Medic's record factory redacts the rendered message and any traceback; `extra` fields would skip it (a test enforces this).
5. **Labels, enum values and `instance` are passed raw.** The choke point redacts them and then applies D3's label rule (`label_safe`) **once**. Don't call `label_safe` yourself: it isn't idempotent, and hashing before redaction would put a hash of a secret in the store.
6. **Time comes from the framework.** Don't stamp `observed_at`; put the source's own time in `Reading.source_ts` if it gives one.

## 5. Redaction

Every observation passes `redact.redact_observation` between the bus and the sink: key names (`*_PASSWORD=`, `"api_key":`, `secret-names.txt`), URL credentials, PEM blocks, token shapes (`Bearer`, JWT, `sk-…`, `ghp_…`, `AKIA…` …), `--secret-flag value`, and the contextual `token <value>`. Free text is redacted in full **before** it's capped. If the redactor raises, the observation is dropped and counted (`redactor_failures`). It is **pattern-only**: Medic holds no secret values. A log sensor that needs the redacted line itself (for `line_hash`) calls `Redactor().redact(line)` first; the choke point runs again, and already-redacted text is stable.

## 6. DEV_MODE (K1 T-37)

On a Vigil install with `DEV_MODE` on, every request is an admin. Then sensors with `uses_vigil_api = True` are **off**, with reason `vigil_dev_mode` in `Scheduler.status()`. **What never changes in DEV_MODE:** redaction, the schema check, timeouts, bounded buffers, read-only clients, and the rule that crashes and hangs are reported.

## 7. Tests every sensor PR ships

- **Unit, against a local stub** (real sockets where you can, `httpx.MockTransport` for DNS/TLS/connect-timeout): one case per row of your outcome table.
- **Through the framework:** `tests/sensors/harness.Rig([your_sensor])`, `await rig.tick()`, then `rig.assert_all_valid()`: every observation validates against `observation.schema.json` (log observations are checked only in tests, S3-1).
- **Canary:** if the sensor emits free text (a log line, an error string), add a case that plants each `contracts/fixtures/redaction/cases.json` input and asserts the secret isn't in `rig.sink`.
- **Fixtures:** a new secret shape is a new `must_redact` case in `contracts/fixtures/redaction/cases.json` (a contract change: its own PR), never a sensor-local filter.

Async tests: `async def test_…` runs on a fresh loop via `tests/sensors/conftest.py`. Nothing sleeps: `Rig` drives a `FakeClock`, and `rig.tick(15)` advances one tick.

## 8. Worked example: `http_ready`

`sensors/http_ready.py` reads agent `/readyz` (worker 6990, serve 6989). `/healthz` always says `ok`, so it proves nothing; `/readyz` checks the agent's dependencies. Its outcome table:

| Target answered | `ready` | `result` | outcome |
|---|---|---|---|
| 200 | true | `ready` | ok |
| 503 / other 5xx | false | `not_ready` / `http_5xx` | ok |
| refused · connected but silent · dropped | false | `refused` · `timeout` · `no_response` | ok |
| 401/403 · 3xx/4xx · connect timeout · DNS · TLS | | | error `auth` · `http_status` · `timeout` · `dns` · `tls` |

`status_code` and `latency_ms` are gauges, **absent** when nothing answered. `instance` is the target's `host:port`. Register it with `agent_worker_ready(host=…)` / `agent_serve_ready(host=…)`; the host differs per install shape. Its tests (`tests/sensors/test_http_ready.py`) are the template for yours.
