"""The pipeline fault cells (SW I2): I-3.a, I-3.b, P-2.c and P-3.a.

Same contract and stack as config_cells.py; each verify reads Vigil (the daemon's
log, /status and /health, the database), never Medic. No Medic is in the stack.

I-3.a and I-3.b ride the federation path, because the rule matches the logger
core.federation.adapters._siem_base: Elastic is polled by federation from the
siem-stub (tests/medic_compose/stub.py), which serves whatever hits a cell writes.
"""

from __future__ import annotations

import json
import time

from tests.medic_eval.inject.config_cells import (
    ELASTIC,
    ELASTIC_STUB,
    _eventually,
    _expect,
)
from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.stack import Stack

# ---- I-3: federation polling Elastic ----------------------------------------
# Federation (global and the "elastic" source) is enabled, elastic-siem points at
# the stub, and the daemon is recreated: poll loops are built at start, and a
# fresh container has an empty log, so "warning present" is unambiguous.
# The stub ignores the query's time filter, so only the alerts' own @timestamp
# matters, and only for I-3.b.

BASE_MAX_ITEMS = 100
BASE_INTERVAL = 300
FED_BASELINE = f"false|false|{BASE_MAX_ITEMS}|{BASE_INTERVAL}|false"
FED_INTERVAL = 10  # the API's floor, so a warning shows within seconds
I3B_MAX_ITEMS = 3


def _fed_state(s: Stack) -> str:
    """global | source enabled | max_items | interval | elastic-siem enabled."""
    src = "SELECT {} FROM federation_sources WHERE source_id = 'elastic'"
    return s.sql(
        "SELECT concat_ws('|', "
        "coalesce((SELECT value->>'enabled' FROM system_config WHERE key = 'federation.settings'), 'false'), "
        f"coalesce(({src.format('enabled::text')}), 'false'), "
        f"coalesce(({src.format('max_items::text')}), '{BASE_MAX_ITEMS}'), "
        f"coalesce(({src.format('interval_seconds::text')}), '{BASE_INTERVAL}'), "
        f"coalesce((SELECT enabled::text FROM integration_configs WHERE integration_id = '{ELASTIC}'), 'false'))"
    )


def _stub_hits(s: Stack, hits: list | None) -> None:
    """What the stub answers a search with; None is the stub's default, no alerts."""
    s.py(
        "siem-stub",
        "import pathlib, sys; p = pathlib.Path('/tmp/elastic-hits.json'); "
        "p.write_text(sys.argv[1]) if sys.argv[1] else p.unlink(missing_ok=True)",
        json.dumps(hits) if hits is not None else "",
    )


def _federation(s: Stack, hits: list | None, max_items: int, interval: int) -> None:
    _stub_hits(s, hits)
    s.set_integration(ELASTIC, {"elasticsearch_url": ELASTIC_STUB}, hits is not None)
    status, body = s.api("PUT", "/api/federation/settings", {"enabled": hits is not None})
    assert status == 200, (status, body)
    status, body = s.api(
        "PATCH",
        "/api/federation/sources/elastic",
        {
            "enabled": hits is not None,
            "max_items": max_items,
            "interval_seconds": interval,
        },
    )
    assert status == 200, (status, body)
    s.recreate("soc-daemon")
    s.wait_daemon()


def _i3_inject(s: Stack, cause: str, hits: list, max_items: int) -> None:
    if cause not in s.active:
        _federation(s, hits, max_items, FED_INTERVAL)
        s.active.add(cause)


def _i3_revert(s: Stack, cause: str) -> None:
    if cause in s.active or _fed_state(s) != FED_BASELINE:
        _federation(s, None, BASE_MAX_ITEMS, BASE_INTERVAL)
    s.active.discard(cause)


def _i3_verify(s: Stack, faulted: bool, warning: str) -> None:
    if faulted:
        s.wait(lambda: s.logs("soc-daemon"), lambda out: warning in out, timeout=90)
        return
    _expect(_fed_state(s), FED_BASELINE, "federation and elastic-siem settings")
    # Federation is off, so nothing polls; the recreated daemon's log has none.
    assert warning not in s.logs("soc-daemon"), "the warning is still logged"


# ---- I-3.a: alerts the transform cannot read --------------------------------
# ElasticIngestion.transform_alert_to_finding returns None for a document it
# cannot read; the adapter drops it and logs one WARNING per batch.

I3A_WARNING = "dropped 3 of 3 alert(s) that failed to transform"


def i3a_inject(s: Stack) -> None:
    hits = [{"_id": f"eval-i3a-{n}", "_source": "not a document"} for n in range(3)]
    _i3_inject(s, "I-3.a", hits, BASE_MAX_ITEMS)


def i3a_revert(s: Stack) -> None:
    _i3_revert(s, "I-3.a")


def i3a_verify(s: Stack, faulted: bool) -> None:
    _i3_verify(s, faulted, I3A_WARNING)


# ---- I-3.b: a full batch with no readable alert time ------------------------
# A burst over max_items is not a fault: the cursor stops at the newest returned
# alert and later ticks drain the rest. Data is lost only when a full batch has no
# readable time: the cursor then moves to now and the rest of the window is
# skipped (core/federation/adapters/_base.py full_batch_cursor). No rule covers
# it, so it scores as a miss.

I3B_WARNING = f"batch filled max_items={I3B_MAX_ITEMS} but no alert carried a readable time"


def i3b_inject(s: Stack) -> None:
    # Exactly max_items hits (a short batch drains), none with an @timestamp.
    hits = [
        {"_id": f"eval-i3b-{n}", "_source": {"message": "alert without a time"}}
        for n in range(I3B_MAX_ITEMS)
    ]
    _i3_inject(s, "I-3.b", hits, I3B_MAX_ITEMS)


def i3b_revert(s: Stack) -> None:
    _i3_revert(s, "I-3.b")


def i3b_verify(s: Stack, faulted: bool) -> None:
    _i3_verify(s, faulted, I3B_WARNING)


# ---- P-2.c: every orchestrator slot held while intake work queues -----------
# Since #3579 only `executing` rows hold a slot (a parked waiting_approval run does
# not), so the cell inserts that many `executing` investigations and one queued
# trigger. max_concurrent_agents and stale_threshold are read once at daemon start,
# hence the recreate; stale_threshold is large so the supervisor does not fail the
# made-up runs. The rows are tagged, and revert deletes only them.

CAP = 2
TAG = "eval-p2c"
ORCH_DEFAULTS = {
    "enabled": False,
    "dry_run": True,  # nothing launches a real agent
    "max_concurrent_agents": 3,
    "stale_threshold": 300,
    "loop_interval": 60,
}
P2C_BASELINE = "0|0|false"
# The rule's for: 15m plus stalled_for 2h. run_cell clamps it to the window held.
P2C_DETECT_S = 8100


def _p2c_state(s: Stack) -> str:
    """tagged investigations | tagged triggers | orchestrator enabled."""
    return s.sql(
        f"SELECT (SELECT count(*) FROM investigations WHERE investigation_id LIKE '{TAG}-%') "
        f"|| '|' || (SELECT count(*) FROM intake_triggers WHERE reason = '{TAG}') "
        "|| '|' || coalesce((SELECT value->>'enabled' FROM system_config WHERE key = 'orchestrator.settings'), 'false')"
    )


def _orchestrator(s: Stack, settings: dict) -> None:
    status, body = s.api("POST", "/api/config/orchestrator", settings)
    assert status == 200, (status, body)


def p2c_inject(s: Stack) -> None:
    if _p2c_state(s) == f"{CAP}|1|true":
        return
    _orchestrator(
        s,
        {
            **ORCH_DEFAULTS,
            "enabled": True,
            "max_concurrent_agents": CAP,
            "stale_threshold": 86400,
            "loop_interval": 10,
        },
    )
    s.recreate("soc-daemon")  # an orchestrator that starts with the small cap
    s.wait_daemon()
    # The recreate fails in-flight rows (the orchestrator's shutdown), so insert after it.
    s.sql(
        "INSERT INTO investigations (investigation_id, workflow_id, trigger_type, trigger_ids, "
        "status, workdir, current_step, total_steps, iteration_count, max_iterations, "
        "input_tokens, output_tokens, cost_usd, max_cost_usd, priority, max_runtime_seconds, "
        "error_count, started_at, last_activity_at) "
        f"SELECT '{TAG}-' || n, 'eval', 'detection', '[]', 'executing', '/tmp/{TAG}', 0, 0, 0, 50, "
        "0, 0, 0, 5, 'medium', 86400, 0, now() at time zone 'utc', now() at time zone 'utc' "
        f"FROM generate_series(1, {CAP}) n ON CONFLICT DO NOTHING; "
        "INSERT INTO intake_triggers (kind, state, reason, priority, payload) "
        f"SELECT 'detection', 'queued', '{TAG}', 'medium', '{{}}' "
        f"WHERE NOT EXISTS (SELECT 1 FROM intake_triggers WHERE reason = '{TAG}')"
    )


def p2c_revert(s: Stack) -> None:
    if _p2c_state(s) == P2C_BASELINE:
        return
    s.sql(
        f"DELETE FROM investigations WHERE investigation_id LIKE '{TAG}-%'; "
        f"DELETE FROM intake_triggers WHERE reason = '{TAG}'"
    )
    _orchestrator(s, ORCH_DEFAULTS)
    s.recreate("soc-daemon")
    s.wait_daemon()


def _free_slots(s: Stack) -> int:
    status, text = s.http("GET", "soc-daemon", 9091, "/status")
    _expect(status, 200, "GET :9091/status")
    return json.loads(text)["orchestrator"]["free_slots"]


def p2c_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        _eventually(s, lambda: _free_slots(s) <= 0, True, "orchestrator free_slots <= 0")
        time.sleep(15)  # an intake pass (loop_interval is 10 s) has seen the queued row
        # Still queued, and the made-up runs were not failed as stale.
        _expect(
            s.sql(
                f"SELECT string_agg(state, ',') FROM intake_triggers WHERE reason = '{TAG}'"
            ),
            "queued",
            "intake trigger state",
        )
        _expect(
            s.sql(
                f"SELECT count(*) FROM investigations WHERE investigation_id LIKE '{TAG}-%' AND status = 'executing'"
            ),
            str(CAP),
            "executing investigations",
        )
        _expect(_free_slots(s), 0, "free_slots")
    else:
        _expect(_p2c_state(s), P2C_BASELINE, "tagged rows and orchestrator setting")
        _eventually(s, lambda: _free_slots(s) > 0, True, "orchestrator free_slots > 0")


# ---- P-3.a: the scheduler task is cancelled ---------------------------------
# The daemon has no endpoint or signal that stops a component, so compose.eval.yml
# mounts sitecustomize.py, which cancels the task named "scheduler" on SIGUSR1.
# /health then reads components.scheduler "stopped" and answers 503; recreating the
# daemon starts it again.


def _health(s: Stack) -> tuple[int, str]:
    """(HTTP status, scheduler state); the state is "" when the answer is not the daemon's JSON."""
    status, text = s.http("GET", "soc-daemon", 9091, "/health")
    try:
        return status, json.loads(text)["components"]["scheduler"]
    except (ValueError, KeyError):
        return status, ""


def p3a_inject(s: Stack) -> None:
    if _health(s)[1] == "stopped":
        return
    s.compose("kill", "-s", "SIGUSR1", "soc-daemon")  # PID 1 is the daemon's python
    _eventually(s, lambda: _health(s)[1], "stopped", "components.scheduler")


def p3a_revert(s: Stack) -> None:
    if _health(s) == (200, "running"):
        return
    s.recreate("soc-daemon")
    s.wait_daemon()


def p3a_verify(s: Stack, faulted: bool) -> None:
    want = (503, "stopped") if faulted else (200, "running")
    _eventually(s, lambda: _health(s), want, "GET :9091/health (status, scheduler)")


CELLS = (
    Cell(
        "I-3.a",
        3,
        120,
        i3a_inject,
        i3a_verify,
        i3a_revert,
        (
            "enable federation and its elastic source, polling a siem-stub that serves unreadable alerts",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        (
            "disable federation and elastic-siem, clear the stub's alerts",
            "docker compose up -d --force-recreate soc-daemon",
        ),
    ),
    # No rule covers I-3.b, so it scores as a miss.
    Cell(
        "I-3.b",
        3,
        120,
        i3b_inject,
        i3b_verify,
        i3b_revert,
        (
            f"serve a full batch (max_items={I3B_MAX_ITEMS}) of alerts with no @timestamp to federated elastic",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        (
            "disable federation and elastic-siem, clear the stub's alerts",
            "docker compose up -d --force-recreate soc-daemon",
        ),
    ),
    Cell(
        "P-2.c",
        2,
        P2C_DETECT_S,
        p2c_inject,
        p2c_verify,
        p2c_revert,
        (
            f"enable the orchestrator with max_concurrent_agents={CAP}",
            f"insert {CAP} executing investigations and a queued intake trigger",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        (
            "delete the inserted rows, disable the orchestrator",
            "docker compose up -d --force-recreate soc-daemon",
        ),
    ),
    Cell(
        "P-3.a",
        3,
        120,
        p3a_inject,
        p3a_verify,
        p3a_revert,
        ("docker compose kill -s SIGUSR1 soc-daemon (cancels the scheduler task)",),
        ("docker compose up -d --force-recreate soc-daemon",),
    ),
)
