"""Regenerate X3 evidence-report fixtures (valid files are canonical bytes, never hand-typed).

uv run python tools/gen_x3_fixtures.py

valid/*.json    exact report bytes, as the admin would send them
invalid/*.json  {"$comment": why, "expect": error code, "report": the report}; the test writes
                "report" canonically and runs evidence_report_check.check on it
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from evidence_report_check import canonical

OUT = ROOT / "fixtures" / "evidence-reports"
ZEROS = "0" * 64
LOG_LINE = (
    '2026-12-10T03:14:07Z WARNING core.federation.runner poll failed for source "Azure Sentinel" '
    "(https://10.2.3.4/api) user=svc_vigil: 401 Unauthorized"
)


def span(intervals: int, seconds: int) -> dict:
    return {"intervals": intervals, "seconds": seconds}


def states(**kw: tuple[int, int]) -> dict:
    names = [
        "running",
        "degraded",
        "starting",
        "not_recording",
        "blind",
        "crash_looping",
        "not_running",
    ]
    return {n: span(*kw.get(n, (0, 0))) for n in names}


def row(
    rule,
    rev,
    lane,
    det,
    fix,
    opened,
    routed,
    *,
    reason="rule",
    quiet=0,
    reopened=0,
    esc=0,
    open_end=0,
    fb=None,
    wl=None,
    adj=None,
) -> dict:
    fb = {
        "agree": 0,
        "disagree": 0,
        "wrong_lane": 0,
        "false_alarm": 0,
        "unrated": routed,
    } | (fb or {})
    adj = adj or {"none": routed}
    return {
        "rule_id": rule,
        "revision": rev,
        "lane": lane,
        "lane_reason": reason,
        "detection_type": det,
        "has_fix": fix,
        "opened": opened,
        "routed": routed,
        "suppressed_quiet": quiet,
        "reopened": reopened,
        "escalation_simulated": esc,
        "open_at_end": open_end,
        "feedback_live": fb,
        "wrong_lane_suggested": {"lane1": 0, "lane2": 0, "lane3": 0} | (wl or {}),
        "adjudication": {
            "true_fault": 0,
            "false_alarm": 0,
            "watcher_caused": 0,
            "unknown": 0,
            "none": 0,
        }
        | adj,
    }


def no_restarts(**kw: int) -> dict:
    return {
        k: kw.get(k, 0)
        for k in ["clean", "signal", "oom", "watchdog_stall", "crash", "unknown"]
    }


# Partner week, readout 1 (J2): Compose, local midnight = 05:00Z.
PARTNER_WEEK = {
    "format": "medic.evidence/v1",
    "instance_id": "mi_3f9a2c1b0d4e5f60",
    "install_shape": "compose",
    "medic_version": "0.1.0",
    "engine_api": "1.0",
    "dev_mode": False,
    "generated_at": "2026-12-16T15:05:12Z",
    "period": {
        "from": "2026-12-09T05:00:00Z",
        "to": "2026-12-16T05:00:00Z",
        "seconds": 604800,
    },
    "retention_days": 90,
    "packs": [
        {
            "id": "medic-core",
            "version": "0.3.1",
            "channel": "stable",
            "active_s": 604800,
        }
    ],
    "pack_events": {"imported": 0, "reverted": 0, "override": 0, "rule_skipped": 0},
    "chain": {
        "head": {
            "seq": 4211,
            "hash": "76792190d1855c71c2b6b076ba494b9b4f8f795f24a8e2a767823e571fea4f7d",
        },
        "oldest": {"seq": 0, "hash": ZEROS, "kind": "genesis"},
        "first_seq_in_period": 3807,
        "last_seq_in_period": 4166,
        "anchors_in_period": 0,
        "resets_in_period": 0,
        "verify": "ok",
    },
    "availability": {
        "up_s": 599000,
        "states": states(
            running=(3, 590000),
            degraded=(2, 9000),
            starting=(2, 600),
            blind=(1, 1800),
            not_running=(1, 3400),
        ),
        "restarts": no_restarts(clean=1, watchdog_stall=1),
        "decisions_lost": 0,
        "store_states_seen": ["ok"],
    },
    "sensors_blind": [
        {"signal": "bifrost_routability", "intervals": 1, "seconds": 900},
        {"signal": "federation_sources", "intervals": 2, "seconds": 2700},
    ],
    "rule_exposure": [
        {
            "rule_id": "ingest.integration-config-incomplete",
            "revision": 1,
            "loaded_s": 604800,
            "exposed_s": 596300,
        },
        {
            "rule_id": "ingest.source-silent",
            "revision": 2,
            "loaded_s": 604800,
            "exposed_s": 596300,
        },
        {
            "rule_id": "llm.gateway-outage",
            "revision": 1,
            "loaded_s": 604800,
            "exposed_s": 598100,
        },
        {
            "rule_id": "pipeline.daemon-component-hung",
            "revision": 1,
            "loaded_s": 604800,
            "exposed_s": 599000,
        },
    ],
    "incidents": [
        row(
            "ingest.source-silent",
            2,
            2,
            "absence",
            True,
            3,
            3,
            reopened=1,
            fb={"agree": 1, "false_alarm": 1, "unrated": 1},
        ),
        row("llm.gateway-outage", 1, 3, "event", False, 2, 1, quiet=1),
        row(
            "pipeline.daemon-component-hung",
            1,
            1,
            "event",
            True,
            1,
            1,
            esc=1,
            fb={"wrong_lane": 1, "unrated": 0},
            wl={"lane2": 1},
        ),
    ],
    "unknown_signature": {"opened": 0, "routed": 0},
    "watcher_incidents": [
        {"rule_id": "watcher.sensor-blind", "opened": 3, "open_s": 3600}
    ],
    "replay_feedback": {
        "heads": ["9c1e5b7a2f6d4e8c0b3a5f7e9d1c2b4a6f8e0d2c4b6a8f0e1d3c5b7a9f2e4d6c"],
        "agree": 14,
        "disagree": 2,
        "wrong_lane": 1,
        "false_alarm": 0,
    },
    "overhead": {
        "source": "medic_self",
        "samples": 9983,
        "cpu_p95_millicores": 120,
        "cpu_max_millicores": 640,
        "mem_p95_mib": 210,
        "mem_max_mib": 262,
        "store_growth_kib_per_day": 2900,
        "gateway_req_per_min_p95": 9,
        "limit_hits": 0,
    },
}

# Internal Helm instance (A4 venue Q), whole J4 window, adjudicated in the store.
INTERNAL_WINDOW = deepcopy(PARTNER_WEEK) | {
    "instance_id": "mi_a07c11e2d93b4f58",
    "install_shape": "helm",
    "generated_at": "2026-12-23T09:00:00Z",
    "period": {
        "from": "2026-12-09T00:00:00Z",
        "to": "2026-12-23T00:00:00Z",
        "seconds": 1209600,
    },
    "packs": [
        {
            "id": "medic-core",
            "version": "0.3.1",
            "channel": "stable",
            "active_s": 400000,
        },
        {
            "id": "medic-core",
            "version": "0.3.2",
            "channel": "stable",
            "active_s": 809600,
        },
    ],
    "pack_events": {"imported": 1, "reverted": 0, "override": 0, "rule_skipped": 1},
    "chain": {
        "head": {
            "seq": 9120,
            "hash": "1f2e3d4c5b6a79880716253443526170f1e2d3c4b5a69788796a5b4c3d2e1f00",
        },
        "oldest": {
            "seq": 2210,
            "hash": "aa11bb22cc33dd44ee55ff6600778899aabbccddeeff00112233445566778899",
            "kind": "anchor",
        },
        "first_seq_in_period": 6402,
        "last_seq_in_period": 9120,
        "anchors_in_period": 1,
        "resets_in_period": 0,
        "verify": "ok",
    },
    "availability": {
        "up_s": 1207800,
        "states": states(
            running=(2, 1200000),
            degraded=(1, 7800),
            starting=(1, 300),
            not_running=(1, 1500),
        ),
        "restarts": no_restarts(signal=1),
        "decisions_lost": 0,
        "store_states_seen": ["near_cap", "ok"],
    },
    "sensors_blind": [],
    "rule_exposure": [
        {
            "rule_id": "ingest.source-silent",
            "revision": 2,
            "loaded_s": 1209600,
            "exposed_s": 1207800,
        },
        {
            "rule_id": "llm.gateway-outage",
            "revision": 1,
            "loaded_s": 1209600,
            "exposed_s": 1207800,
        },
        {
            "rule_id": "pipeline.run-stale",
            "revision": 1,
            "loaded_s": 809600,
            "exposed_s": 809000,
        },
    ],
    "incidents": [
        row(
            "ingest.source-silent",
            2,
            2,
            "absence",
            True,
            2,
            2,
            adj={"true_fault": 1, "false_alarm": 1},
        ),
        row("llm.gateway-outage", 1, 3, "event", False, 1, 1, adj={"true_fault": 1}),
        row(
            "pipeline.run-stale",
            1,
            3,
            "absence",
            True,
            1,
            1,
            reason="lane1_gate_failed",
            adj={"unknown": 1},
        ),
    ],
    "unknown_signature": {"opened": 1, "routed": 1},
    "watcher_incidents": [],
    "replay_feedback": {
        "heads": [],
        "agree": 0,
        "disagree": 0,
        "wrong_lane": 0,
        "false_alarm": 0,
    },
}

# Host-native lab box, switched off the whole day: nothing recorded, every list empty.
QUIET_DAY = deepcopy(PARTNER_WEEK) | {
    "instance_id": "mi_0b5d9e2a7c13f846",
    "install_shape": "start_sh",
    "generated_at": "2026-12-11T08:00:00Z",
    "period": {
        "from": "2026-12-10T00:00:00Z",
        "to": "2026-12-11T00:00:00Z",
        "seconds": 86400,
    },
    "packs": [],
    "chain": {
        "head": {
            "seq": 12,
            "hash": "5e8f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7",
        },
        "oldest": {"seq": 0, "hash": ZEROS, "kind": "genesis"},
        "first_seq_in_period": None,
        "last_seq_in_period": None,
        "anchors_in_period": 0,
        "resets_in_period": 0,
        "verify": "ok",
    },
    "availability": {
        "up_s": 0,
        "states": states(not_running=(1, 86400)),
        "restarts": no_restarts(),
        "decisions_lost": 0,
        "store_states_seen": ["ok"],
    },
    "sensors_blind": [],
    "rule_exposure": [],
    "incidents": [],
    "watcher_incidents": [],
    "replay_feedback": {
        "heads": [],
        "agree": 0,
        "disagree": 0,
        "wrong_lane": 0,
        "false_alarm": 0,
    },
    "overhead": None,
}


def mutate(fn) -> dict:
    doc = deepcopy(PARTNER_WEEK)
    fn(doc)
    return doc


def _set(path: list, value):
    def fn(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return fn


def _pop(path: list):
    def fn(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        node.pop(path[-1])

    return fn


def _hostname_rule(doc):
    doc["rule_exposure"][0]["rule_id"] = "ingest.splunk01-acme-internal"
    doc["rule_exposure"].sort(key=lambda e: (e["rule_id"], e["revision"]))


def _unsorted(doc):
    doc["incidents"].reverse()


INVALID = {
    # The two the prompt requires: free text smuggled in, and a raw log line.
    "smuggled-free-text-top-level": (
        "R-SCHEMA",
        "a free-text field the schema doesn't name",
        _set(["note"], "Splunk on splunk01.acme.internal keeps timing out"),
    ),
    "smuggled-free-text-in-row": (
        "R-SCHEMA",
        "an admin comment copied into an incident row (G2 5b: never exported)",
        _set(["incidents", 0, "comment"], "this fired while jsmith was patching"),
    ),
    "raw-log-line-in-id-field": (
        "R-SCHEMA",
        "a raw log line placed in a pattern-checked id field",
        _set(["sensors_blind", 0, "signal"], LOG_LINE),
    ),
    "raw-log-line-as-evidence": (
        "R-SCHEMA",
        "evidence excerpts never leave the site",
        _set(["incidents", 0, "evidence"], [{"excerpt": LOG_LINE}]),
    ),
    # Identifiers that would map the partner.
    "instance-id-hostname": (
        "R-SCHEMA",
        "instance_id derived from a host name (X1 5a)",
        _set(["instance_id"], "mi_vigil-prod-01"),
    ),
    "incident-id-included": (
        "R-SCHEMA",
        "incident ids stay on screen (J2)",
        _set(["incidents", 0, "incident_id"], "inc_0123456789abcdef01234567"),
    ),
    "group-value-included": (
        "R-SCHEMA",
        "group values name partner sources",
        _set(
            ["incidents", 0, "group"],
            [{"name": "source", "value": "h_9f8e7d6c5b4a3210"}],
        ),
    ),
    "admin-identity-included": (
        "R-SCHEMA",
        "admin identities never leave",
        _set(["replay_feedback", "admin"], "jsmith"),
    ),
    "pack-id-not-published": (
        "R-SCHEMA",
        "pack ids are a closed list",
        _set(["packs", 0, "id"], "medic-acme-prod"),
    ),
    "hostname-in-rule-id": (
        "R-RULE",
        "pattern-valid rule id that isn't in the pack catalogue",
        _hostname_rule,
    ),
    "signal-not-in-catalogue": (
        "R-SIGNAL",
        "pattern-valid signal that isn't a D1 signal",
        _set(["sensors_blind", 1, "signal"], "splunk_acme_prod"),
    ),
    # Types.
    "float-count": (
        "R-SCHEMA",
        "integers only; rates are computed by the reader",
        _set(["incidents", 0, "feedback_live", "agree"], 0.5),
    ),
    "rate-field": (
        "R-SCHEMA",
        "no precomputed rates",
        _set(["availability", "uptime_pct"], 99),
    ),
    "decimal-string-count": (
        "R-SCHEMA",
        "a count as a string",
        _set(["incidents", 0, "opened"], "3"),
    ),
    "negative-count": (
        "R-SCHEMA",
        "counts are >= 0",
        _set(["pack_events", "imported"], -1),
    ),
    "missing-chain-head": (
        "R-SCHEMA",
        "the chain head is required (K1 T-17)",
        _pop(["chain", "head"]),
    ),
    "period-not-whole-hour": (
        "R-SCHEMA",
        "periods start on the hour",
        _set(["period", "from"], "2026-12-09T05:30:00Z"),
    ),
    "period-over-31-days": (
        "R-SCHEMA",
        "X2 caps a period at 31 days",
        _set(["period", "seconds"], 2764800),
    ),
    # Invariants the schema can't express.
    "generated-before-period-end": (
        "R-PERIOD",
        "a report can't cover time that hasn't happened",
        _set(["generated_at"], "2026-12-15T23:00:00Z"),
    ),
    "states-dont-add-up": (
        "R-STATES",
        "every second of the period is in one state",
        _set(["availability", "states", "running", "seconds"], 589000),
    ),
    "up-not-running-plus-degraded": (
        "R-STATES",
        "up_s counts running + degraded only",
        _set(["availability", "up_s"], 600800),
    ),
    "unrated-exceeds-routed": (
        "R-ROW",
        "more unrated than routed",
        _set(["incidents", 1, "feedback_live", "unrated"], 2),
    ),
    "gate-failed-not-lane-3": (
        "R-ROW",
        "lane1_gate_failed always routes to lane 3 (G1 R3)",
        _set(["incidents", 2, "lane_reason"], "lane1_gate_failed"),
    ),
    "unsorted-incidents": (
        "R-ORDER",
        "lists are in key order, so the bytes are deterministic",
        _unsorted,
    ),
    "exposure-over-uptime": (
        "R-EXPOSURE",
        "a rule can't be exposed while the watcher was down",
        _set(["rule_exposure", 3, "exposed_s"], 600000),
    ),
    "incident-without-exposure": (
        "R-EXPOSURE",
        "every incident rule needs a denominator",
        _pop(["rule_exposure", 1]),
    ),
    "chain-seq-past-head": (
        "R-CHAIN",
        "the period can't end after the head",
        _set(["chain", "last_seq_in_period"], 4300),
    ),
}

KNOWN_RULES = sorted(
    {
        (e["rule_id"], e["revision"])
        for doc in (PARTNER_WEEK, INTERNAL_WINDOW, QUIET_DAY)
        for e in doc["rule_exposure"]
    }
)


def main() -> None:
    (OUT / "valid").mkdir(parents=True, exist_ok=True)
    (OUT / "invalid").mkdir(parents=True, exist_ok=True)
    valid = {
        "partner-week-compose": PARTNER_WEEK,
        "internal-window-helm": INTERNAL_WINDOW,
        "quiet-day-start-sh": QUIET_DAY,
    }
    for name, doc in valid.items():
        (OUT / "valid" / f"{name}.json").write_bytes(canonical(doc))
    for old in (OUT / "invalid").glob("*.json"):
        old.unlink()
    for name, (code, why, fn) in INVALID.items():
        doc = {"$comment": why, "expect": code, "report": mutate(fn)}
        (OUT / "invalid" / f"{name}.json").write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n"
        )
    (OUT / "known-rules.json").write_text(
        json.dumps([list(k) for k in KNOWN_RULES], indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
