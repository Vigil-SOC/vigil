"""A full-pack tick stays inside Medic's CPU budget (#2014).

40 rules (the valid fixtures, cycled under distinct ids, the longest window at
48 h) × 50 groups, with 8 h of history from sensors reading every 15 s plus their
heartbeats. One warm-up tick ingests the history; the median CPU time of the
ticks after it must be ≤ 1 s on one core.
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime, timedelta

import yaml

from services.medic.engine import Engine, load_rule
from services.medic.tests.engine.runner import INSTANCE, RULES_DIR

RULES, GROUPS, HISTORY_S, INTERVAL_S, TICKS = 40, 50, 8 * 3600, 15, 7
START = datetime(2026, 1, 1, tzinfo=UTC)
NAMES = [f"g{i:02d}" for i in range(GROUPS)]


def rules() -> list:
    fixtures = [yaml.safe_load(p.read_text()) for p in sorted(RULES_DIR.glob("*.yaml"))]
    out = []
    for i in range(RULES):
        rule = dict(
            fixtures[i % len(fixtures)],
            id=f"{fixtures[i % len(fixtures)]['id']}-{i:02d}",
        )
        params = (
            {"quiet_window": "48h"}
            if "quiet_window" in rule.get("params", {})
            else None
        )
        out.append(load_rule(rule, params=params))
    return out


def _v(key: str, value, vtype: str = "gauge", **labels) -> dict:
    entry = {"key": key, "type": vtype, "state": "present", "value": value}
    entry["trust"] = "untrusted" if vtype == "text" else "trusted"
    return entry | ({"labels": labels} if labels else {})


def _values(signal: str, n: int, stamp: str) -> list[dict]:
    """One read of `signal`, the n-th since the start (counters rise with n)."""
    if signal == "federation_sources":
        return [
            v
            for s in NAMES
            for v in (
                _v("sources.enabled", True, source=s),
                _v("sources.last_success_at", stamp, "text", source=s),
            )
        ]
    if signal == "triage":  # every fifth source has delivered nothing
        return [
            _v("sources.arrivals", 0 if i % 5 == 0 else n, "counter", source=s)
            for i, s in enumerate(NAMES)
        ]
    if signal == "daemon_status.poller":
        polls = [_v("polls", n, "counter", source=s) for s in NAMES]
        return polls + [_v("webhook_findings", n // 4, "counter")]
    if signal == "daemon_status.kafka":
        return [_v("decode_errors", n // 100, "counter")] + [
            _v("missing_id_errors", 0, "counter")
        ]
    if signal == "daemon_status.processor":
        return [_v("processed", n, "counter"), _v("triaged", n - n % 3, "counter")]
    if signal == "daemon_health":
        return [_v("components.processor", "running", "text")] + [
            _v("uptime_seconds", 900 + n * INTERVAL_S)
        ]
    if signal == "bifrost_health":
        return [_v("ok", n % 40 < 38)]
    if signal == "llm_providers":
        return [
            v
            for i, p in enumerate(NAMES)
            for v in (
                _v("providers.is_default", i == 0, provider=p),
                _v("providers.has_api_key", i != 0, provider=p),
                _v("providers.is_active", True, provider=p),
            )
        ]
    if signal == "probe_findings":
        return [_v("latest_outcome", "hit", "text", probe=p) for p in NAMES]
    assert signal == "sql_investigations_stale"
    return [_v("stale_executing", 0), _v("oldest_last_activity_at", stamp, "text")]


SAMPLED = [
    "bifrost_health",
    "daemon_health",
    "daemon_status.kafka",
    "daemon_status.poller",
    "daemon_status.processor",
    "federation_sources",
    "llm_providers",
    "probe_findings",
    "sql_investigations_stale",
    "triage",
]
LOGGER, TEMPLATE = (
    "core.integrations.elastic",
    "%s configuration incomplete (missing: %s)",
)


def _base(kind: str, signal: str, sensor: str, n: int, t: int) -> dict:
    stamp = (START + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "v": 1,
        "id": f"{sensor}:01PERFRUN000:{n}",
        "kind": kind,
        "signal": signal,
        "sensor": {"id": sensor, "run": "01PERFRUN000", "seq": n},
        "target": {"service": "soc-daemon", "shape": "compose"},
        "t": stamp,
        "observed_at": stamp,
        "source_ts": None,
        "outcome": "ok",
        "redaction": {"version": "k2-0", "hits": 0},
    }


def observations(t: int) -> list[dict]:
    """Every sensor's read and heartbeat at offset t (a multiple of 15 s)."""
    n = t // INTERVAL_S
    out = []
    for signal in [*SAMPLED, "log_soc_daemon"]:
        sensor = f"s-{signal}"
        if signal != "log_soc_daemon":
            obs = _base("sample", signal, sensor, 2 * n, t)
            obs["values"] = _values(signal, n, obs["t"])
            obs["epoch"] = "e1"
            out.append(obs)
        elif n % 20 == 0:  # an incomplete-config warning every 5 min
            obs = _base("log", signal, sensor, 2 * n, t)
            name = NAMES[n // 20 % 5]
            obs["log"] = {
                "format": "python_json",
                "level": "WARNING",
                "logger": LOGGER,
                "template": TEMPLATE,
                "template_trust": "catalog",
                "message": f"{name} configuration incomplete (missing: url)",
                "fingerprint": "fp_0000000000000000",
                "line_hash": f"{n:016x}",
                "args": [name, "url"],
            }
            out.append(obs)
        beat = _base("sensor_health", "sensor_health", sensor, 2 * n + 1, t)
        beat["health"] = {
            "sensor": sensor,
            "state": "ok",
            "reported_by": "sensor",
            "covers": [signal],
            "interval_s": INTERVAL_S,
        }
        out.append(beat)
    return out


def test_a_full_pack_tick_takes_under_a_second_of_cpu() -> None:
    engine = Engine(rules(), instance_id=INSTANCE, install_shape="compose")
    for t in range(0, HISTORY_S + 1, INTERVAL_S):
        for obs in observations(t):
            engine.observe(obs)
    engine.tick(START + timedelta(seconds=HISTORY_S))  # warm-up: ingests the history
    times = []
    for k in range(1, TICKS + 1):
        t = HISTORY_S + k * INTERVAL_S
        for obs in observations(t):
            engine.observe(obs)
        before = time.process_time()
        engine.tick(START + timedelta(seconds=t))
        times.append(time.process_time() - before)
    groups = sum(len(s) for s in engine.machines.values())
    assert groups >= 15 * GROUPS  # the workload really is 50 groups wide
    assert statistics.median(times) <= 1.0, times
