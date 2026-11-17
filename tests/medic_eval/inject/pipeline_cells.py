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

from tests.medic_eval import medic_api
from tests.medic_eval.inject.config_cells import (
    ELASTIC,
    ELASTIC_STUB,
    _eventually,
    _expect,
)
from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.stack import Stack
from tests.medic_eval.inject.survival_checks import restore

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
    status, body = s.api(
        "PUT", "/api/federation/settings", {"enabled": hits is not None}
    )
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

I3B_WARNING = (
    f"batch filled max_items={I3B_MAX_ITEMS} but no alert carried a readable time"
)


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
        _eventually(
            s, lambda: _free_slots(s) <= 0, True, "orchestrator free_slots <= 0"
        )
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


# ---- P-3.a: the scheduler task fails ----------------------------------------
# The daemon has no endpoint that stops a component, so compose.eval.yml mounts
# sitecustomize.py, which raises in the scheduler task on SIGUSR1. /health then
# reads components.scheduler "failed: RuntimeError" and answers 503. The fault
# is held until the lane-1 rule can fire, then cleared by Medic's run-fix
# (the shipped pack's restart-soc-daemon), never by recreating the container.


def _health(s: Stack) -> tuple[int, str]:
    """(HTTP status, scheduler state); the state is "" when the answer is not the daemon's JSON."""
    status, text = s.http("GET", "soc-daemon", 9091, "/health")
    try:
        return status, json.loads(text)["components"]["scheduler"]
    except (ValueError, KeyError):
        return status, ""


def _failed(state: str) -> bool:
    return state == "failed" or state.startswith("failed:")


def p3a_inject(s: Stack) -> None:
    if _failed(_health(s)[1]):
        return
    s.compose("kill", "-s", "SIGUSR1", "soc-daemon")  # PID 1 is the daemon's python
    _eventually(s, lambda: _failed(_health(s)[1]), True, "components.scheduler failed")


def p3a_revert(s: Stack) -> None:
    if _health(s) == (200, "running"):
        return
    deadline = time.monotonic() + 180
    incident = None
    while time.monotonic() < deadline and incident is None:
        page = medic_api.get(s, "/v1/incidents?state=open&limit=200") or {}
        for item in page.get("items") or []:
            rule = item.get("rule") or {}
            if (
                rule.get("id") == "pipeline.daemon-component-failed-compose"
                and item.get("state") == "firing"
            ):
                incident = item["incident_id"]
                break
        if incident is None:
            time.sleep(5)
    if incident is None:
        raise AssertionError("lane-1 soc-daemon incident did not open")
    status, body = medic_api.post(s, f"/v1/incidents/{incident}/fix", "{}")
    if status == 409 and json.loads(body).get("reason") == "fix_running":
        pass  # a second revert: the first click is still in its restart
    elif status != 201:
        raise AssertionError(f"run-fix answered {status}: {body[:300]}")
    s.wait(lambda: _health(s), lambda got: got == (200, "running"), timeout=180)


def p3a_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        _eventually(
            s,
            lambda: (_health(s)[0], _failed(_health(s)[1])),
            (503, True),
            "GET :9091/health failed",
        )
    else:
        _eventually(
            s,
            lambda: _health(s),
            (200, "running"),
            "GET :9091/health (status, scheduler)",
        )


# ---- P-3.d: the scheduler loop stalls without dying --------------------------
# SIGUSR2 (sitecustomize.py) stops TaskScheduler.run from counting passes and
# leaves the task running. Not P-3.a: that cause is the death cell, and
# rule_for binds it to pipeline.daemon-component-failed-compose. The lane-1
# rule is a suppression child, so it stays held for 5 minutes after it fires;
# the route wait and the click are in revert, after detect_within_s.
# The stack imports the source pack at bring-up (Stack.enable_medic), because
# the image's bundled pack is older than these lane-1 rules.

HUNG_RULE = "pipeline.daemon-component-hung-compose"
# 15m changes window plus for: 5m. The 5-minute route hold is not part of this.
P3D_DETECT_S = 15 * 60 + 5 * 60
# Past that window plus the 5-minute child delay, so a hang would have routed.
P3E_DETECT_S = P3D_DETECT_S + 5 * 60 + 60
# The scheduler counts a pass and then sleeps 60s (services/daemon/scheduler.py).
# Longer than the 60s sleep: a pass also runs its tasks before sleeping again.
_TICK_S = 90


def _health_body(s: Stack) -> tuple[int, dict]:
    """/health from inside soc-daemon. The backend is not on this path: the
    database cell stops postgres, and the backend may go with it."""
    out = s.py(
        "soc-daemon",
        "import json, urllib.request, urllib.error\n"
        "try:\n"
        "    r = urllib.request.urlopen('http://127.0.0.1:9091/health', timeout=5)\n"
        "    print(json.dumps([r.status, r.read().decode()]))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(json.dumps([e.code, e.read().decode()]))\n"
        "except Exception as e:\n"
        "    print(json.dumps([0, str(e)]))\n",
    )
    status, text = json.loads(out)
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = {}
    return status, body if isinstance(body, dict) else {}


def _scheduler_pass(s: Stack) -> tuple[int, str, int | None]:
    status, body = _health_body(s)
    comp = (body.get("components") or {}).get("scheduler")
    count = (body.get("passes") or {}).get("scheduler")
    return (
        status,
        comp if isinstance(comp, str) else "",
        count if type(count) is int else None,
    )


def _daemon_started(s: Stack) -> str:
    return s.daemon_started()


def _incidents(s: Stack) -> list:
    return (
        medic_api.get(s, "/v1/incidents?include_suppressed=true&limit=100", paged=True)
        or []
    )


def _fix_cleared(s: Stack, incident_id: str) -> bool:
    rows = (
        medic_api.get(
            s,
            f"/v1/decisions?type=fix_verified&incident_id={incident_id}&limit=100",
            paged=True,
        )
        or []
    )
    return any(
        row.get("type") == "fix_verified"
        and (row.get("body") or {}).get("incident_id") == incident_id
        and (row.get("body") or {}).get("result") == "cleared"
        for row in rows
    )


def _fix_seqs(s: Stack) -> list:
    rows = medic_api.get(s, "/v1/decisions?type=fix_run&limit=100", paged=True) or []
    return [row.get("seq") for row in rows if row.get("type") == "fix_run"]


def _hung_routed(s: Stack, opened_after: str) -> dict | None:
    """The lane-1 hang incident that opened at or after `opened_after`, if it has routed."""
    for item in _incidents(s):
        rule = item.get("rule") or {}
        if rule.get("id") != HUNG_RULE or item.get("opened_at", "") < opened_after:
            continue
        if item.get("routed_at"):
            return item
    return None


def _wait_routed(s: Stack, opened_after: str) -> dict:
    found = s.wait(
        lambda: _hung_routed(s, opened_after),
        lambda item: item is not None,
        timeout=15 * 60,
        every=15,
    )
    return found


def _passes_moved(s: Stack) -> bool:
    """True once scheduler passes have increased across one scheduler sleep."""
    status, comp, first = _scheduler_pass(s)
    if status != 200 or comp != "running" or first is None:
        return False
    time.sleep(_TICK_S)
    status, comp, second = _scheduler_pass(s)
    return status == 200 and comp == "running" and second is not None and second > first


def p3d_inject(s: Stack) -> None:
    status, comp, count = _scheduler_pass(s)
    if (
        status == 200
        and comp == "running"
        and s.marks.get("p3d") == count
        and count is not None
    ):
        return
    _expect(status, 200, "GET :9091/health before the stall")
    _expect(comp, "running", "components.scheduler before the stall")
    assert count is not None, "passes.scheduler missing"
    s.signal_daemon("SIGUSR2")
    time.sleep(2)  # the wrapper cancels the real loop within one check
    status, comp, stalled = _scheduler_pass(s)
    time.sleep(_TICK_S)
    status, comp, count = _scheduler_pass(s)
    _expect(status, 200, "GET :9091/health while stalled")
    _expect(comp, "running", "components.scheduler while stalled")
    _expect(count, stalled, "passes.scheduler across one scheduler sleep")
    assert count is not None
    s.marks["p3d"] = count
    s.marks["p3d_at"] = int(time.time())


def p3d_revert(s: Stack) -> None:
    if s.marks.get("p3d_clear") and _scheduler_pass(s)[:2] == (200, "running"):
        return
    opened_after = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(s.marks.get("p3d_at", 0))
    )
    incident = _wait_routed(s, opened_after)
    started = _daemon_started(s)
    status, body = medic_api.post(
        s, f"/v1/incidents/{incident['incident_id']}/fix", "{}"
    )
    if status == 409 and json.loads(body).get("reason") == "fix_running":
        pass
    elif status != 201:
        raise AssertionError(f"run-fix answered {status}: {body[:300]}")
    s.wait(lambda: _passes_moved(s), lambda ok: ok, timeout=300, every=5)
    assert _daemon_started(s) != started, "soc-daemon was not restarted"
    s.wait(
        lambda: _fix_cleared(s, incident["incident_id"]),
        lambda ok: ok,
        timeout=240,
        every=5,
    )
    s.marks["p3d_clear"] = 1


def p3d_verify(s: Stack, faulted: bool) -> None:
    status, comp, _count = _scheduler_pass(s)
    if faulted:
        _expect((status, comp), (200, "running"), "stalled scheduler still running")
        assert s.marks.get("p3d") is not None
    else:
        _expect((status, comp), (200, "running"), "scheduler after the run-fix")


def p3e_inject(s: Stack) -> None:
    """Postgres down, no hang. The daemon is not recreated: that would open
    ingest.processor-without-database and reset the pass counters."""
    if s.marks.get("p3e"):
        return
    s.p3e = {  # type: ignore[attr-defined]
        "started": _daemon_started(s),
        "fixes": tuple(_fix_seqs(s)),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # compose.eval.yml sets restart: no, so a service that exits while
        # Postgres is down stays down. Remember what was already down.
        "down": tuple(s.not_running()),
    }
    s.marks["p3e"] = 1
    s.compose("stop", "postgres")


def p3e_revert(s: Stack) -> None:
    try:
        if not s.marks.get("p3e"):
            return
        held = getattr(s, "p3e", {})
        routed = _hung_routed(s, held.get("at", ""))
        assert routed is None, (
            f"lane-1 hang routed during the database outage: {routed}"
        )
        assert tuple(_fix_seqs(s)) == held.get("fixes", ()), "a fix_run was recorded"
        assert _daemon_started(s) == held.get("started"), "State.StartedAt changed"
    finally:
        s.marks.pop("p3e", None)
        if "postgres" in s.not_running():
            s.compose("start", "postgres")
            s.wait(
                lambda: s.compose("ps", "--format", "json", "postgres").stdout,
                lambda o: '"healthy"' in o,
                timeout=180,
            )
        restore(s, set(getattr(s, "p3e", {}).get("down", ())))


def p3e_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        assert "postgres" in s.not_running(), "postgres is still running"
        before = _health_body(s)
        _expect(before[0], 200, "GET :9091/health while postgres is down")
        _expect(
            (before[1].get("components") or {}).get("scheduler"), "running", "scheduler"
        )
        time.sleep(_TICK_S)
        after = _health_body(s)
        _expect(after[0], 200, "GET :9091/health after one scheduler sleep")
        for name in ("processor", "scheduler"):
            was = (before[1].get("passes") or {}).get(name)
            now = (after[1].get("passes") or {}).get(name)
            assert type(was) is int and type(now) is int and now > was, (
                f"passes.{name} did not advance ({was} -> {now})"
            )
    else:
        assert "postgres" not in s.not_running(), "postgres was not started"


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
        180,
        p3a_inject,
        p3a_verify,
        p3a_revert,
        ("docker compose kill -s SIGUSR1 soc-daemon (scheduler task raises)",),
        ("POST /v1/incidents/{id}/fix (restart soc-daemon)",),
    ),
    Cell(
        "P-3.d",
        1,
        P3D_DETECT_S,
        p3d_inject,
        p3d_verify,
        p3d_revert,
        ("docker compose kill -s SIGUSR2 soc-daemon (scheduler loop stalls)",),
        ("POST /v1/incidents/{id}/fix once routed (restart soc-daemon)",),
    ),
    Cell(
        "P-3.e",
        1,
        P3E_DETECT_S,
        p3e_inject,
        p3e_verify,
        p3e_revert,
        ("docker compose stop postgres",),
        ("docker compose start postgres",),
    ),
)
