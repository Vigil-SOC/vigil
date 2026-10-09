"""Regenerate G2 decision-record fixtures (hashes are computed, never hand-typed).

uv run python tools/gen_g2_fixtures.py
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from decision_chain import incident_id, record_hash, seal
from fingerprint_ref import label_safe

INSTANCE = "mi_3f9a2c1b0d4e5f60"  # random at first start (X1 ⚑5a)
SHAPE = "helm"

OUT = ROOT / "fixtures" / "decision-records"
# X1 ⚑1a: the group value is label_safe(args[0]); the raw arg only appears in the excerpt.
EXC = {
    "text": "WARNING core.integrations Azure Sentinel configuration incomplete (missing: client_secret); skipping polls until it is completed",
    "redaction_version": "k2-1.0",
}

OPEN_LANE2 = {
    "subject": "vigil",
    "instance_id": INSTANCE,
    "install_shape": SHAPE,
    "rule": {
        "id": "ingest.integration-config-incomplete",
        "revision": 1,
        "pack_id": "medic-core",
        "pack_version": "0.1.0",
        "engine_api": "1.0",
        "input_trust": "untrusted",
    },
    "fault": {"class": "ingest", "mode": "I-1", "causes": ["I-1.b"]},
    "detection_type": "event",
    "active_since": "2026-12-07T09:13:00Z",
    "group": [{"name": "integration", "value": label_safe("Azure Sentinel")}],
    "route": "routed",
    "lane": {"value": 2, "reason": "rule"},
    "evidence": [
        {
            "observation_id": "log.soc-daemon:a1b2c3d4e5:1042",
            "signal": "log_soc_daemon",
            "t": "2026-12-07T09:12:58Z",
            "fingerprint": "fp1_3f9a2c1b0d4e5f60",
            "excerpt": EXC,
        },
    ],
    "advice": {
        "summary": "An integration is enabled but its configuration is incomplete.",
        "fix": "Open Settings > Integrations, open the integration named in the evidence, fill in the missing fields and save. Polling resumes on the next cycle.",
    },
    "runbook": None,
    "would_have": {
        "L0": "record",
        "L1": "notify_admin",
        "L2": {"action": "notify_admin", "not_eligible": "not_lane1"},
    },
}

OPEN_LANE1 = {
    "subject": "vigil",
    "instance_id": INSTANCE,
    "install_shape": SHAPE,
    "rule": {
        "id": "pipeline.agent-worker-down",
        "revision": 2,
        "pack_id": "medic-core",
        "pack_version": "0.1.0",
        "engine_api": "1.0",
        "input_trust": "trusted",
    },
    "fault": {"class": "pipeline", "mode": "P-6", "causes": ["P-6.a"]},
    "detection_type": "event",  # a readiness 503 is an event (S0 review R7)
    "active_since": "2026-12-07T10:00:00Z",
    "group": [],
    "route": "routed",
    "lane": {"value": 1, "reason": "rule"},
    "evidence": [
        {
            "observation_id": "http.agent:9a8b7c6d5e:310",
            "signal": "agent_worker_readyz",
            "t": "2026-12-07T10:01:30Z",
            "key": "status",
            "value": 503,
        },
        {
            "observation_id": "k8s.pods:1a2b3c4d5e:77",
            "signal": "pods",
            "t": "2026-12-07T10:01:45Z",
            "key": "restarts",
            "value": "4",
        },
    ],
    "advice": {
        "summary": "The agent worker is running but not ready.",
        "fix": "Restart the agent worker. If it fails readiness again within 10 minutes, collect a support bundle.",
    },
    "runbook": {"id": "restart-agent-worker", "verify_after_s": 300},
    "would_have": {
        "L0": "record",
        "L1": "propose_runbook",
        "L2": {"action": "run_runbook"},
    },
}

OPEN_SELF = {
    "subject": "watcher",
    "instance_id": INSTANCE,
    "install_shape": SHAPE,
    "rule": {
        "id": "watcher.sensor-blind",
        "revision": 1,
        "pack_id": "medic-builtin",
        "pack_version": "1.0",
        "engine_api": "1.0",
        "input_trust": "trusted",
    },
    "fault": None,
    "detection_type": "event",
    "active_since": "2026-12-07T11:00:00Z",
    "group": [
        {"name": "sensor", "value": "sensor.api"}
    ],  # one group per sensor (E3 §2; R8)
    "route": "routed",
    "lane": {"value": 3, "reason": "rule"},
    "evidence": [
        {
            "observation_id": "sensor.api:0f1e2d3c4b:12",
            "signal": "sensor_health",
            "t": "2026-12-07T11:00:00Z",
            "key": "state",
            "value": "blind",
        }
    ],
    "advice": None,
    "runbook": None,
    "would_have": {
        "L0": "record",
        "L1": "prepare_support_bundle",
        "L2": {"action": "prepare_support_bundle", "not_eligible": "not_lane1"},
    },
}

# A suppression child (E3 6): held for 5 min, then routed because no parent fired.
OPEN_CHILD = {
    **OPEN_SELF,
    "subject": "vigil",
    "rule": {**OPEN_LANE1["rule"], "id": "pipeline.triage-stalled", "revision": 1},
    "fault": {"class": "pipeline", "mode": "P-4", "causes": ["P-4.a", "P-4.b"]},
    "detection_type": "absence",  # triage-stalled declares it (R7)
    "group": [],
    "active_since": "2026-12-07T12:00:00Z",
    "route": "held",
    "evidence": [],
}


def with_id(body: dict) -> dict:
    """Fill in the derived incident id (A3-5)."""
    rule = body["rule"]["id"] if body["rule"] else None
    return {
        "incident_id": incident_id(
            body["instance_id"], rule, body.get("group", []), body["active_since"]
        ),
        **body,
    }


OPEN_LANE2, OPEN_LANE1, OPEN_SELF, OPEN_CHILD = map(
    with_id, (OPEN_LANE2, OPEN_LANE1, OPEN_SELF, OPEN_CHILD)
)
ID2, ID1, IDS, IDC = (
    b["incident_id"] for b in (OPEN_LANE2, OPEN_LANE1, OPEN_SELF, OPEN_CHILD)
)
CORE_010 = {"id": "medic-core", "version": "0.1.0"}
CORE_011 = {"id": "medic-core", "version": "0.1.1"}
# Head hash of the replay store an admin reviewed (E6); the replay itself is a separate store.
REPLAY_HEAD = "9" * 64


def rec(at: str, type_: str, body: dict) -> dict:
    return {"v": 1, "at": at, "type": type_, "body": body}


def chain(raw: list[dict], start: dict | None = None) -> list[dict]:
    out, prev = [], start
    for r in raw:
        prev = seal(r, prev)
        out.append(prev)
    return out


def pilot_day() -> list[dict]:
    return chain(
        [
            rec("2026-12-07T09:15:00Z", "incident_opened", OPEN_LANE2),
            rec(
                "2026-12-07T09:40:00Z",
                "feedback",
                {
                    "incident_id": ID2,
                    "value": "agree",
                    "source": "live",
                    "admin": "user:7f3c",
                    "comment": {
                        "text": "Fixed, the key had expired.",
                        "redaction_version": "k2-1.0",
                    },
                },
            ),
            rec(
                "2026-12-07T09:36:15Z",
                "incident_updated",
                {
                    "incident_id": ID2,
                    "change": "resolving",
                    "eval": "false",
                },
            ),
            rec(
                "2026-12-07T09:41:15Z",
                "incident_resolved",
                {
                    "incident_id": ID2,
                    "how": "cleared",
                    "eval": "false",
                },
            ),
            rec("2026-12-07T10:02:00Z", "incident_opened", OPEN_LANE1),
            rec(
                "2026-12-07T10:12:00Z",
                "incident_updated",
                {
                    "incident_id": ID1,
                    "change": "escalation_simulated",
                    "simulated_failures": 2,
                },
            ),
            rec("2026-12-07T11:02:00Z", "incident_opened", OPEN_SELF),
            rec("2026-12-07T12:02:00Z", "incident_opened", OPEN_CHILD),
            rec(
                "2026-12-07T12:07:00Z",
                "incident_updated",
                {"incident_id": IDC, "change": "routed"},
            ),
            rec(
                "2026-12-10T16:00:00Z",
                "adjudication",
                {
                    "incident_id": ID1,
                    "verdict": "true_fault",
                    "true_lane": 1,
                    "by": "craig",
                    "basis": "weekly_readout",
                },
            ),
            rec(
                "2026-12-10T16:01:00Z",
                "feedback",
                {
                    "incident_id": ID1,
                    "value": "wrong_lane",
                    "suggested_lane": 3,
                    "source": "replay",
                    "admin": "user:7f3c",
                    "replay_head": REPLAY_HEAD,
                },
            ),
            # X1 ⚑3a: pack lifecycle (wall-clock `at`: not engine output).
            rec(
                "2026-12-10T16:05:00Z",
                "pack_event",
                {
                    "event": "rule_skipped",
                    "pack": CORE_010,
                    "rule": "llm.budget-exhausted",
                    "reason": "needs_engine_minor",
                },
            ),
            rec(
                "2026-12-10T16:10:00Z",
                "pack_event",
                {
                    "event": "imported",
                    "pack": CORE_011,
                    "previous": CORE_010,
                    "source": "admin_import",
                    "by": "user:7f3c",
                },
            ),
            rec(
                "2026-12-10T17:00:00Z",
                "pack_event",
                {
                    "event": "reverted",
                    "pack": CORE_010,
                    "previous": CORE_011,
                    "by": "user:7f3c",
                },
            ),
            rec(
                "2026-12-11T07:00:00Z",
                "pack_event",
                {
                    "event": "override",
                    "pack": CORE_011,
                    "previous": CORE_010,
                    "by": "user:7f3c",
                },
            ),
        ]
    )


def after_purge(day: list[dict]) -> list[dict]:
    """The first 3 records were purged: an anchor is appended, then they are deleted."""
    full = chain(
        [
            rec(
                "2027-03-08T00:00:05Z",
                "anchor",
                {
                    "reason": "retention",
                    "deleted_first_seq": 0,
                    "deleted_last_seq": 2,
                    "deleted_count": 3,
                    # `at` isn't monotonic across types: the range is min..max (R13).
                    "deleted_from": min(r["at"] for r in day[:3]),
                    "deleted_to": max(r["at"] for r in day[:3]),
                    "last_deleted_hash": day[2]["hash"],
                },
            )
        ],
        day[-1],
    )
    return day[3:] + full


def store_reset(day: list[dict]) -> list[dict]:
    head = day[-1]
    first = seal(
        {
            "v": 1,
            "seq": head["seq"] + 1,
            "at": "2026-12-11T08:00:00Z",
            "type": "store_reset",
            "body": {
                "reason": "corruption",
                "moved_aside": "medic.db.corrupt-20261211T080000Z",
                "previous_head": {"seq": head["seq"], "hash": head["hash"]},
                "instance_id": INSTANCE,
            },
        },
        None,
    )
    first["prev"] = head["hash"]
    first["hash"] = record_hash(first)
    return [first] + chain(
        [
            rec(
                "2026-12-11T08:05:00Z",
                "incident_opened",
                with_id(
                    {
                        **{k: v for k, v in OPEN_LANE2.items() if k != "incident_id"},
                        "active_since": "2026-12-11T08:03:00Z",
                    }
                ),
            )
        ],
        first,
    )


INVALID = {
    "free-text-field": (
        "an unlisted free-text field (K1 T-04)",
        lambda r: r["body"].__setitem__("notes", "admin says it was the firewall"),
    ),
    "excerpt-too-long": (
        "an excerpt over 500 chars",
        lambda r: r["body"]["evidence"][0]["excerpt"].__setitem__("text", "x" * 501),
    ),
    "excerpt-without-redaction": (
        "an excerpt that doesn't say which redactor ran",
        lambda r: r["body"]["evidence"][0]["excerpt"].pop("redaction_version"),
    ),
    "float-value": (
        "a float, which would make the hash language-dependent",
        lambda r: r["body"]["evidence"][0].__setitem__("value", 0.5),
    ),
    "too-much-evidence": (
        "11 evidence items (cap 10, C4 sizing)",
        lambda r: r["body"].__setitem__("evidence", r["body"]["evidence"] * 11),
    ),
    "lane-four": ("lane 4", lambda r: r["body"]["lane"].__setitem__("value", 4)),
    "vigil-lane-null": (
        "a Vigil incident with no lane",
        lambda r: r["body"].__setitem__("lane", {"value": None, "reason": "rule"}),
    ),
    "missing-would-have": (
        "no would_have block",
        lambda r: r["body"].pop("would_have"),
    ),
    "templated-group-value": (
        "a group value carrying injected text",
        lambda r: r["body"]["group"][0].__setitem__("value", "elastic; DROP TABLE"),
    ),
    "bad-prev-hash": (
        "prev that isn't a sha256 hex",
        lambda r: r.__setitem__("prev", "abc"),
    ),
}

INVALID_FEEDBACK = {
    "feedback-admin-free-text": (
        "an admin field with a display name (identity must be a session id)",
        lambda r: r["body"].__setitem__("admin", "Jane Doe <jane@example.com>"),
    ),
    "feedback-suggested-lane-on-agree": (
        "suggested_lane on an 'agree'",
        lambda r: r["body"].__setitem__("suggested_lane", 1),
    ),
}


INVALID_PACK_EVENT = {
    "pack-event-skip-without-rule": (
        "a rule_skipped event that doesn't name the rule",
        lambda r: r["body"].pop("rule"),
    ),
    "pack-event-admin-import-without-by": (
        "an admin import that doesn't record who imported it (F3: importer identity)",
        lambda r: r.__setitem__(
            "body",
            {
                "event": "imported",
                "pack": r["body"]["pack"],
                "previous": None,
                "source": "admin_import",
            },
        ),
    ),
    "pack-event-unknown-event": (
        "an event outside the four lifecycle events",
        lambda r: r["body"].__setitem__("event", "deleted"),
    ),
}

INVALID_IDENTITY = {
    "instance-id-hostname": (
        "an instance_id derived from a hostname (X1 ⚑5a: random only)",
        lambda r: r["body"].__setitem__(
            "instance_id", "vigil-0.vigil.svc.cluster.local"
        ),
    ),
}


def main() -> None:
    day = pilot_day()
    for name, records in {
        "chain-01-pilot-day": day,
        "chain-02-after-purge": after_purge(day),
        "chain-03-store-reset": store_reset(day),
    }.items():
        path = OUT / "valid" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    (OUT / "invalid").mkdir(parents=True, exist_ok=True)
    first = {r["type"]: r for r in reversed(day)}
    for table, base in (
        (INVALID, first["incident_opened"]),
        (INVALID_FEEDBACK, first["feedback"]),
        (INVALID_PACK_EVENT, first["pack_event"]),
        (INVALID_IDENTITY, first["incident_opened"]),
    ):
        for name, (why, mutate) in table.items():
            doc = deepcopy(base)
            mutate(doc)
            doc = {"$comment": f"Refused: {why}.", **doc}
            (OUT / "invalid" / f"{name}.json").write_text(
                json.dumps(doc, indent=2) + "\n"
            )


if __name__ == "__main__":
    main()
