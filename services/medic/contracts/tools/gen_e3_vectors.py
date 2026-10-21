"""Regenerates contracts/vectors/*.yaml. Run from contracts/:
uv run python tools/gen_e3_vectors.py

Vectors are the reviewed artefact; this script only saves typing. Expected values
were worked out by hand from semantics.md, tick by tick (tick = 15 s).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from fingerprint_ref import label_safe

OUT = Path("vectors")
OUT.mkdir(exist_ok=True)
START = "2026-10-07T10:00:00Z"

DAEMON = {
    "id": "http.daemon",
    "covers": [
        "daemon_status.kafka",
        "daemon_status.processor",
        "daemon_status.poller",
        "daemon_health",
    ],
    "interval": "15s",
    "service": "soc-daemon",
    "states": [["+0s", "ok"]],
}
LOGS = {
    "id": "log.soc-daemon",
    "covers": ["log_soc_daemon"],
    "interval": "15s",
    "service": "soc-daemon",
    "states": [["+0s", "ok"]],
}
FED = {
    "id": "http.federation",
    "covers": ["federation_sources"],
    "interval": "30s",
    "service": "backend",
    "states": [["+0s", "ok"]],
}
TRIAGE = {
    "id": "http.triage",
    "covers": ["triage"],
    "interval": "30s",
    "service": "backend",
    "states": [["+0s", "ok"]],
}
BIFROST = {
    "id": "http.bifrost",
    "covers": ["bifrost_health"],
    "interval": "15s",
    "service": "bifrost",
    "states": [["+0s", "ok"]],
}
REDIS = {
    "id": "redis.queues",
    "covers": ["redis_queues"],
    "interval": "15s",
    "service": "redis",
    "states": [["+0s", "ok"]],
}
BACKUP = {
    "id": "http.backend-metrics",
    "covers": ["backend_metrics_backup"],
    "interval": "60s",
    "service": "backend",
    "states": [["+0s", "ok"]],
}
VERSION = {
    "id": "http.version",
    "covers": ["version"],
    "interval": "60s",
    "service": "backend",
    "states": [["+0s", "ok"]],
}

KAFKA_TYPES = {
    "daemon_status.kafka": {
        "decode_errors": "counter",
        "missing_id_errors": "counter",
        "connected": "bool",
    }
}
ADVICE = {
    "summary": "Test rule for an evaluation vector.",
    "fix": "No action: this rule exists only in a test vector.",
}

CFG_SET = (
    "%s configuration incomplete (missing: %s); skipping polls until it is completed"
)
CFG_CLEAR = "%s configuration complete; polling resumed after %d skipped polls"
GW_SET = "Failed to connect LLM gateway, AI triage is skipped until it connects: %s"
GW_CLEAR = "%s; recovered after %d failures"


def sensor(base: dict, states: list) -> dict:
    return {**base, "states": states}


def e(at, rule, ev, state, incident=None, group=None, suppressed=None, note=None):
    out = {"at": at, "rule": rule, "eval": ev, "state": state}
    if group is not None:
        out["group"] = group
    if incident:
        out["incident"] = incident
    if suppressed is not None:
        out["suppressed"] = suppressed
    if note:
        out["note"] = note
    return out


def inc(rule, active, opened, resolving=None, resolved=None, **kw):
    out = {
        "rule": rule,
        "active_since": active,
        "opened_at": opened,
        "resolving_since": resolving,
        "resolved_at": resolved,
    }
    if resolved:
        out.setdefault("reason", "cleared")
    out.update(kw)
    return out


def rule(rid, cls, mode, causes, signals, when, lane=3, **kw):
    r = {
        "apiVersion": "medic.rules/v1",
        "kind": "Rule",
        "id": rid,
        "title": f"Vector rule {rid}",
        "revision": 1,
        "fault": {"class": cls, "mode": mode, "causes": causes},
        "lane": lane,
        "signals": signals,
        "when": when,
    }
    r.update(kw)
    r["advice"] = ADVICE
    return r


def sb(sensor_id: str) -> dict:
    return {"sensor": sensor_id}


def every(step_min: int, upto: int, f) -> list:
    return [[f"+{m}m", f(m)] for m in range(0, upto + 1, step_min)]


def ts(m: int) -> str:
    return f"2026-10-07T{10 + m // 60:02d}:{m % 60:02d}:00Z"


KAFKA = "ingest.kafka-decode-errors"
SBR = "watcher.sensor-blind"
V: dict[str, dict] = {}

# v01 · hold-for, then auto-resolve after keep_firing_for
V["v01-hold-for-fires-and-resolves"] = {
    "title": "for 10m then keep_firing_for 15m on a counter jump",
    "covers": ["§2 increase", "§4 pending/firing/resolving"],
    "end": "+55m",
    "rules": [{"ref": "ingest-kafka-decode-errors.yaml"}],
    "sensors": [DAEMON],
    "types": KAFKA_TYPES,
    "series": [
        {
            "signal": "daemon_status.kafka",
            "key": "decode_errors",
            "steps": [["+0s", 0], ["+20m", 10]],
        },
        {
            "signal": "daemon_status.kafka",
            "key": "missing_id_errors",
            "steps": [["+0s", 0]],
        },
    ],
    "expect": [
        e(
            "+5m",
            KAFKA,
            "unknown",
            "inactive",
            note="window not covered yet; lower bound 0 doesn't decide > 5",
        ),
        e("+16m", KAFKA, False, "inactive"),
        e("+20m", KAFKA, True, "pending"),
        e("+29m45s", KAFKA, True, "pending"),
        e("+30m", KAFKA, True, "firing", "A"),
        e(
            "+35m",
            KAFKA,
            False,
            "resolving",
            "A",
            note="baseline is now the +20m sample",
        ),
        e("+49m45s", KAFKA, False, "resolving", "A"),
        e("+50m", KAFKA, False, "inactive"),
    ],
    "incidents": {"A": inc(KAFKA, "+20m", "+30m", "+35m", "+50m", routed_at="+30m")},
}

# v02 · condition goes false before for elapses: no incident
BURST = rule(
    "ingest.kafka-decode-burst",
    "ingest",
    "I-3",
    ["I-3.j"],
    {
        "decode_errors": {
            "sample": {"signal": "daemon_status.kafka", "key": "decode_errors"}
        }
    },
    {
        "fn": "increase",
        "signal": "decode_errors",
        "window": "5m",
        "op": ">",
        "value": 5,
    },
    lane=2,
    **{"for": "10m", "keep_firing_for": "5m"},
)
V["v02-hold-for-resets-on-false"] = {
    "title": "A blip shorter than for never opens an incident",
    "covers": ["§4 pending→inactive"],
    "end": "+40m",
    "rules": [{"inline": BURST}],
    "sensors": [DAEMON],
    "types": KAFKA_TYPES,
    "series": [
        {
            "signal": "daemon_status.kafka",
            "key": "decode_errors",
            "steps": [["+0s", 0], ["+20m", 10]],
        }
    ],
    "expect": [
        e("+4m", BURST["id"], "unknown", "inactive"),
        e("+5m", BURST["id"], False, "inactive"),
        e("+20m", BURST["id"], True, "pending"),
        e("+24m45s", BURST["id"], True, "pending"),
        e("+25m", BURST["id"], False, "inactive"),
        e("+40m", BURST["id"], False, "inactive"),
    ],
    "incidents": {},
}

# v03 · unknown neither resets nor fires; a gap keeps the window a lower bound
V["v03-unknown-bridges-pending"] = {
    "title": "A sensor outage during pending doesn't reset the hold-for timer",
    "covers": ["§2 freshness", "§2 coverage", "§4 unknown"],
    "end": "+60m",
    "rules": [{"ref": "ingest-kafka-decode-errors.yaml"}],
    "sensors": [sensor(DAEMON, [["+0s", "ok"], ["+24m", "error"], ["+27m", "ok"]])],
    "types": KAFKA_TYPES,
    "series": [
        {
            "signal": "daemon_status.kafka",
            "key": "decode_errors",
            "steps": [["+0s", 0], ["+20m", 10]],
        },
        {
            "signal": "daemon_status.kafka",
            "key": "missing_id_errors",
            "steps": [["+0s", 0]],
        },
    ],
    "expect": [
        e("+20m", KAFKA, True, "pending"),
        e("+24m", KAFKA, True, "pending", note="newest ok read 23m45 is still fresh"),
        e("+25m", KAFKA, "unknown", "pending"),
        e("+27m", KAFKA, True, "pending", note="gap in window, but lower bound 10 > 5"),
        e("+30m", KAFKA, True, "firing", "A"),
        e(
            "+36m",
            KAFKA,
            "unknown",
            "firing",
            "A",
            note="gap 23m45..27m still inside the window: lower bound 0",
        ),
        e("+42m", KAFKA, False, "resolving", "A"),
        e("+56m45s", KAFKA, False, "resolving", "A"),
        e("+57m", KAFKA, False, "inactive"),
        e("+26m30s", SBR, True, "firing", "SB", group=sb("http.daemon")),
        e("+27m", SBR, False, "inactive", group=sb("http.daemon")),
    ],
    "incidents": {
        "A": inc(KAFKA, "+20m", "+30m", "+42m", "+57m", routed_at="+30m"),
        "SB": inc(
            SBR,
            "+24m30s",
            "+26m30s",
            "+27m",
            "+27m",
            group=sb("http.daemon"),
            routed_at="+26m30s",
        ),
    },
}

# v04 · firing never resolves while blind
V["v04-unknown-never-resolves"] = {
    "title": "A stopped sensor keeps the incident open until a real false",
    "covers": ["§2 freshness", "§4 never resolve blind", "§2 sensor-blind"],
    "end": "+95m",
    "rules": [{"ref": "ingest-kafka-decode-errors.yaml"}],
    "sensors": [sensor(DAEMON, [["+0s", "ok"], ["+33m", "stopped"], ["+60m", "ok"]])],
    "types": KAFKA_TYPES,
    "series": [
        {
            "signal": "daemon_status.kafka",
            "key": "decode_errors",
            "steps": [["+0s", 0], ["+20m", 10]],
        },
        {
            "signal": "daemon_status.kafka",
            "key": "missing_id_errors",
            "steps": [["+0s", 0]],
        },
    ],
    "expect": [
        e("+30m", KAFKA, True, "firing", "A"),
        e("+34m", KAFKA, "unknown", "firing", "A"),
        e("+59m", KAFKA, "unknown", "firing", "A"),
        e(
            "+70m",
            KAFKA,
            "unknown",
            "firing",
            "A",
            note="reads are back but the 15m window isn't covered until +75m",
        ),
        e("+75m", KAFKA, False, "resolving", "A"),
        e("+89m45s", KAFKA, False, "resolving", "A"),
        e("+90m", KAFKA, False, "inactive"),
        e("+35m30s", SBR, True, "firing", "SB", group=sb("http.daemon")),
        e("+60m", SBR, False, "inactive", group=sb("http.daemon")),
    ],
    "incidents": {
        "A": inc(KAFKA, "+20m", "+30m", "+75m", "+90m", routed_at="+30m"),
        "SB": inc(
            SBR,
            "+33m30s",
            "+35m30s",
            "+60m",
            "+60m",
            group=sb("http.daemon"),
            routed_at="+35m30s",
        ),
    },
}

# v05 · never readable: rule never fires, watcher-blind does
V["v05-unknown-never-fires"] = {
    "title": "A signal that can't be read is unknown, never healthy, and raises sensor-blind",
    "covers": ["§2 unknown", "§2 sensor-blind"],
    "end": "+10m",
    "rules": [{"ref": "ingest-kafka-decode-errors.yaml"}],
    "sensors": [sensor(DAEMON, [["+0s", "error"]])],
    "types": KAFKA_TYPES,
    "series": [
        {"signal": "daemon_status.kafka", "key": "decode_errors", "steps": [["+0s", 0]]}
    ],
    "expect": [
        e("+1m", KAFKA, "unknown", "inactive"),
        e("+10m", KAFKA, "unknown", "inactive"),
        e("+2m15s", SBR, True, "pending", group=sb("http.daemon")),
        e("+3m", SBR, True, "firing", "SB", group=sb("http.daemon")),
    ],
    "incidents": {
        "SB": inc(SBR, "+30s", "+2m30s", group=sb("http.daemon"), routed_at="+2m30s")
    },
}

# v06 · counter reset across epochs adds the new value, never a negative delta
FAST = rule(
    "ingest.kafka-decode-errors-fast",
    "ingest",
    "I-3",
    ["I-3.j"],
    {
        "decode_errors": {
            "sample": {"signal": "daemon_status.kafka", "key": "decode_errors"}
        }
    },
    {
        "fn": "increase",
        "signal": "decode_errors",
        "window": "15m",
        "op": ">",
        "value": 5,
    },
    lane=2,
    **{"for": "0s"},
)
V["v06-counter-reset-across-epoch"] = {
    "title": "increase sums across a daemon restart: +2, reset, +4 = 6",
    "covers": ["§2 increase epochs", "§2 lower bound"],
    "end": "+32m",
    "rules": [{"inline": FAST}],
    "sensors": [DAEMON],
    "types": KAFKA_TYPES,
    "series": [
        {
            "signal": "daemon_status.kafka",
            "key": "decode_errors",
            "steps": [["+0s", 40], ["+10m", 42], ["+12m", 0], ["+14m", 4]],
            "epochs": [["+0s", "e1"], ["+12m", "e2"]],
        }
    ],
    "expect": [
        e("+12m", FAST["id"], "unknown", "inactive", note="lower bound 2"),
        e("+14m", FAST["id"], True, "firing", "A", note="lower bound 6 already > 5"),
        e("+24m45s", FAST["id"], True, "firing", "A"),
        e(
            "+25m",
            FAST["id"],
            False,
            "resolving",
            "A",
            note="baseline +10m (42): reset adds 0, then +4",
        ),
        e("+30m", FAST["id"], False, "inactive"),
    ],
    "incidents": {
        "A": inc(FAST["id"], "+14m", "+14m", "+25m", "+30m", routed_at="+14m")
    },
}

# v07 · a restart is not a change: "flat" stays flat
HUNG = "pipeline.daemon-component-hung"
V["v07-changes-ignores-reset"] = {
    "title": "processed resets to 0 on restart; changes doesn't count it",
    "covers": ["§2 changes epochs", "§5 two openings are not flapping"],
    "end": "+55m",
    "rules": [{"ref": "pipeline-daemon-component-hung.yaml"}],
    "sensors": [DAEMON],
    "types": {
        "daemon_status.processor": {"processed": "counter"},
        "daemon_status.poller": {"webhook_findings": "counter"},
        "daemon_health": {"uptime_seconds": "gauge", "components.processor": "enum"},
    },
    "series": [
        {
            "signal": "daemon_health",
            "key": "components.processor",
            "steps": [["+0s", "running"]],
        },
        {
            "signal": "daemon_health",
            "key": "uptime_seconds",
            "steps": [["+0s", 3600], ["+30m", 0], ["+45m15s", 915]],
        },
        {
            "signal": "daemon_status.processor",
            "key": "processed",
            "steps": [["+0s", 100], ["+30m", 0]],
            "epochs": [["+0s", "e1"], ["+30m", "e2"]],
        },
        {
            "signal": "daemon_status.poller",
            "key": "webhook_findings",
            "steps": [[f"+{m}m", (m % 30)] for m in range(0, 55, 5)],
            "epochs": [["+0s", "e1"], ["+30m", "e2"]],
        },
    ],
    "expect": [
        e("+10m", HUNG, "unknown", "inactive"),
        e("+15m", HUNG, True, "pending"),
        e("+20m", HUNG, True, "firing", "A"),
        e("+30m", HUNG, False, "resolving", "A", note="uptime 0 < 900"),
        e("+40m", HUNG, False, "inactive"),
        e("+45m", HUNG, False, "inactive"),
        e("+45m15s", HUNG, True, "pending", note="reset 100→0 isn't a change"),
        e("+50m15s", HUNG, True, "firing", "B"),
    ],
    "incidents": {
        "A": inc(HUNG, "+15m", "+20m", "+30m", "+40m", routed_at="+20m"),
        "B": inc(HUNG, "+45m15s", "+50m15s", routed_at="+50m15s"),
    },
}

# v08 · absence needs a covered window
SILENT = "ingest.source-silent"
SPLUNK = {"source": "splunk"}
V["v08-absence-needs-coverage"] = {
    "title": "No arrivals for 1h can't fire until the watcher has watched for 1h",
    "covers": ["§2 coverage", "§2 lower bound"],
    "end": "+80m",
    "rules": [{"ref": "ingest-source-silent.yaml", "params": {"quiet_window": "1h"}}],
    "sensors": [FED, TRIAGE],
    "types": {
        "federation_sources": {
            "sources.enabled": "bool",
            "sources.last_success_at": "timestamp",
        },
        "triage": {"sources.arrivals": "counter"},
    },
    "series": [
        {
            "signal": "federation_sources",
            "key": "sources.enabled",
            "labels": SPLUNK,
            "steps": [["+0s", True]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.last_success_at",
            "labels": SPLUNK,
            "steps": every(5, 80, ts),
        },
        {
            "signal": "triage",
            "key": "sources.arrivals",
            "labels": SPLUNK,
            "steps": [["+0s", 50]],
        },
    ],
    "expect": [
        e("+30m", SILENT, "unknown", "inactive", group=SPLUNK),
        e("+59m45s", SILENT, "unknown", "inactive", group=SPLUNK),
        e("+60m", SILENT, True, "pending", group=SPLUNK),
        e("+74m45s", SILENT, True, "pending", group=SPLUNK),
        e("+75m", SILENT, True, "firing", "A", group=SPLUNK),
    ],
    "incidents": {"A": inc(SILENT, "+60m", "+75m", group=SPLUNK, routed_at="+75m")},
}

# v09 · read errors are not absence
NEVER = "ingest.source-never-polled"
ELASTIC = {"source": "elastic"}
V["v09-errors-are-not-absence"] = {
    "title": "absent_for needs successful reads; a poller outage is unknown",
    "covers": ["§2 absent_for", "§2 lower bound"],
    "end": "+86m",
    "rules": [{"ref": "ingest-source-never-polled.yaml"}],
    "sensors": [sensor(DAEMON, [["+0s", "ok"], ["+35m", "error"], ["+50m", "ok"]])],
    "types": {"daemon_status.poller": {"polls": "counter"}},
    "series": [
        {
            "signal": "daemon_status.poller",
            "key": "polls",
            "labels": ELASTIC,
            "steps": [[f"+{m}m", m * 2] for m in range(0, 35, 5)] + [["+35m", None]],
        }
    ],
    "expect": [
        e("+2m", NEVER, "unknown", "inactive", group=ELASTIC),
        e("+30m", NEVER, False, "inactive", group=ELASTIC),
        e(
            "+40m",
            NEVER,
            "unknown",
            "inactive",
            group=ELASTIC,
            note="stale; and present values in window make absent_for false",
        ),
        e(
            "+65m",
            NEVER,
            "unknown",
            "inactive",
            group=ELASTIC,
            note="gap 35..50: changes==0 is only a lower bound",
        ),
        e("+79m15s", NEVER, "unknown", "inactive", group=ELASTIC),
        e(
            "+79m30s",
            NEVER,
            True,
            "pending",
            group=ELASTIC,
            note="30m of successful reads, all absent",
        ),
        e("+84m30s", NEVER, True, "firing", "A", group=ELASTIC),
        e("+37m30s", SBR, True, "firing", "SB", group=sb("http.daemon")),
        e("+50m", SBR, False, "inactive", group=sb("http.daemon")),
    ],
    "incidents": {
        "A": inc(NEVER, "+79m30s", "+84m30s", group=ELASTIC, routed_at="+84m30s"),
        "SB": inc(
            SBR,
            "+35m30s",
            "+37m30s",
            "+50m",
            "+50m",
            group=sb("http.daemon"),
            routed_at="+37m30s",
        ),
    },
}

# v10 · latch survives an engine restart
CFG = "ingest.integration-config-incomplete"
EL = {"integration": "Elastic"}
V["v10-latch-survives-restart"] = {
    "title": "#1661 set/clear pair; the engine restarts in between",
    "covers": ["§2 latch", "§4 persistence"],
    "end": "+40m",
    "engine_restarts": ["+15m"],
    "rules": [{"ref": "ingest-integration-config-incomplete.yaml"}],
    "sensors": [LOGS],
    "logs": [
        {
            "at": "+5m",
            "signal": "log_soc_daemon",
            "level": "WARNING",
            "logger": "core.integrations.elastic.ingestion",
            "template": CFG_SET,
            "message": "Elastic configuration incomplete (missing: elasticsearch_url); skipping polls until it is completed",
        },
        {
            "at": "+30m",
            "signal": "log_soc_daemon",
            "level": "INFO",
            "logger": "core.integrations.elastic.ingestion",
            "template": CFG_CLEAR,
            "message": "Elastic configuration complete; polling resumed after 100 skipped polls",
        },
    ],
    "expect": [
        e("+5m", CFG, True, "pending", group=EL),
        e("+7m", CFG, True, "firing", "A", group=EL, note="default for 2m"),
        e(
            "+15m15s",
            CFG,
            True,
            "firing",
            "A",
            group=EL,
            note="after the engine restart",
        ),
        e("+30m", CFG, False, "resolving", "A", group=EL),
        e("+35m", CFG, False, "inactive", group=EL, note="default keep_firing_for 5m"),
    ],
    "incidents": {
        "A": inc(CFG, "+5m", "+7m", "+30m", "+35m", group=EL, routed_at="+7m")
    },
}

# v11 · latch re-arms on the next set line
GW = "llm.gateway-outage"
GW_LINE = {
    "signal": "log_soc_daemon",
    "level": "ERROR",
    "logger": "services.daemon.processor",
    "template": GW_SET,
    "message": "Failed to connect LLM gateway, AI triage is skipped until it connects: ConnectError: connection refused",
}
GW_OK = {
    "signal": "log_soc_daemon",
    "level": "INFO",
    "logger": "services.daemon.processor",
    "template": GW_CLEAR,
    "message": "LLM gateway connected for AI triage; recovered after 12 failures",
}
V["v11-latch-reopens"] = {
    "title": "#1689 enter/recover pair, then a second outage",
    "covers": ["§2 latch", "§5 two openings are not flapping"],
    "end": "+25m",
    "rules": [{"ref": "llm-gateway-outage.yaml"}],
    "sensors": [LOGS],
    "logs": [
        {**GW_LINE, "at": "+2m"},
        {**GW_OK, "at": "+10m"},
        {**GW_LINE, "at": "+20m"},
    ],
    "expect": [
        e("+2m", GW, True, "pending"),
        e("+4m", GW, True, "firing", "A"),
        e("+10m", GW, False, "resolving", "A"),
        e("+15m", GW, False, "inactive"),
        e("+20m", GW, True, "pending"),
        e("+22m", GW, True, "firing", "B"),
    ],
    "incidents": {
        "A": inc(GW, "+2m", "+4m", "+10m", "+15m", routed_at="+4m"),
        "B": inc(GW, "+20m", "+22m", routed_at="+22m"),
    },
}

# v12 · count is a lower bound across a log gap
POLLERR = rule(
    "ingest.kafka-poll-errors",
    "ingest",
    "I-3",
    ["I-3.k"],
    {
        "line": {
            "log": {
                "signal": "log_soc_daemon",
                "logger": {"eq": "services.daemon.kafka_ingestor"},
                "template": {"eq": "Kafka consumer poll error: %s"},
            }
        }
    },
    {"fn": "count", "signal": "line", "window": "10m", "op": ">=", "value": 3},
    **{"for": "0s", "keep_firing_for": "0s"},
)
V["v12-count-lower-bound"] = {
    "title": "Lines seen still count during a log gap; absence of lines doesn't",
    "covers": ["§2 count", "§2 lower bound", "§4 never resolve blind"],
    "end": "+40m",
    "rules": [{"inline": POLLERR}],
    "sensors": [sensor(LOGS, [["+0s", "ok"], ["+18m", "stopped"], ["+25m", "ok"]])],
    "logs": [
        {
            "at": "+0s",
            "signal": "log_soc_daemon",
            "level": "ERROR",
            "logger": "services.daemon.kafka_ingestor",
            "template": "Kafka consumer poll error: %s",
            "message": "Kafka consumer poll error: KafkaTimeoutError",
            "repeat": {"every": "1m", "until": "+12m"},
        }
    ],
    "expect": [
        e("+1m", POLLERR["id"], "unknown", "inactive"),
        e("+2m", POLLERR["id"], True, "firing", "A"),
        e(
            "+18m30s",
            POLLERR["id"],
            True,
            "firing",
            "A",
            note="gap, but 4 lines already seen",
        ),
        e("+21m", POLLERR["id"], "unknown", "firing", "A"),
        e("+34m45s", POLLERR["id"], "unknown", "firing", "A"),
        e("+35m", POLLERR["id"], False, "inactive"),
        e("+20m30s", SBR, True, "firing", "SB", group=sb("log.soc-daemon")),
        e("+25m", SBR, False, "inactive", group=sb("log.soc-daemon")),
    ],
    "incidents": {
        "A": inc(POLLERR["id"], "+2m", "+2m", "+35m", "+35m", routed_at="+2m"),
        "SB": inc(
            SBR,
            "+18m30s",
            "+20m30s",
            "+25m",
            "+25m",
            group=sb("log.soc-daemon"),
            routed_at="+20m30s",
        ),
    },
}

# v13 · incident flapping damping
BACKLOG = rule(
    "llm.queue-backlog",
    "llm",
    "L-3",
    ["L-3.a"],
    {"depth": {"sample": {"signal": "redis_queues", "key": "arq_llm.zcard"}}},
    {"fn": "latest", "signal": "depth", "op": ">", "value": 100},
    **{"for": "0s", "keep_firing_for": "0s"},
)
V["v13-incident-flapping"] = {
    "title": "Third opening in 60m reopens the last incident and holds it 30m",
    "covers": ["§5 flapping"],
    "end": "+72m",
    "rules": [{"inline": BACKLOG}],
    "sensors": [REDIS],
    "types": {"redis_queues": {"arq_llm.zcard": "gauge"}},
    "series": [
        {
            "signal": "redis_queues",
            "key": "arq_llm.zcard",
            "steps": [
                ["+0s", 0],
                ["+5m", 150],
                ["+10m", 0],
                ["+20m", 150],
                ["+25m", 0],
                ["+35m", 150],
                ["+40m", 0],
            ],
        }
    ],
    "expect": [
        e("+5m", BACKLOG["id"], True, "firing", "A"),
        e("+10m", BACKLOG["id"], False, "inactive"),
        e("+20m", BACKLOG["id"], True, "firing", "B"),
        e("+25m", BACKLOG["id"], False, "inactive"),
        e(
            "+35m",
            BACKLOG["id"],
            True,
            "firing",
            "B",
            note="third opening in 60m: B reopened, flapping",
        ),
        e("+40m", BACKLOG["id"], False, "resolving", "B"),
        e("+69m45s", BACKLOG["id"], False, "resolving", "B"),
        e("+70m", BACKLOG["id"], False, "inactive"),
    ],
    "incidents": {
        "A": inc(BACKLOG["id"], "+5m", "+5m", "+10m", "+10m", routed_at="+5m"),
        "B": inc(
            BACKLOG["id"],
            "+20m",
            "+20m",
            "+40m",
            "+70m",
            flapping=True,
            reopen_count=1,
            routed_at="+20m",
        ),
    },
}

# v14 · rule-level signal flapping with changes()
FLAP = "llm.gateway-flapping"
V["v14-signal-flapping-rule"] = {
    "title": "changes(bifrost ok, 30m) >= 4",
    "covers": ["§2 changes", "§2 lower bound"],
    "end": "+72m",
    "rules": [{"ref": "llm-gateway-flapping.yaml"}],
    "sensors": [BIFROST],
    "types": {"bifrost_health": {"ok": "bool"}},
    "series": [
        {
            "signal": "bifrost_health",
            "key": "ok",
            "steps": [
                ["+0s", True],
                ["+10m", False],
                ["+12m", True],
                ["+14m", False],
                ["+16m", True],
            ],
        }
    ],
    "expect": [
        e("+15m", FLAP, "unknown", "inactive", note="lower bound 3"),
        e("+16m", FLAP, True, "pending"),
        e("+18m", FLAP, True, "firing", "A"),
        e("+39m45s", FLAP, True, "firing", "A"),
        e("+40m", FLAP, False, "resolving", "A"),
        e("+69m45s", FLAP, False, "resolving", "A"),
        e("+70m", FLAP, False, "inactive"),
    ],
    "incidents": {"A": inc(FLAP, "+16m", "+18m", "+40m", "+70m", routed_at="+18m")},
}

# v15 · per-group evaluation
V["v15-grouping-per-source"] = {
    "title": "One silent source of two opens one incident for that source only",
    "covers": ["§3 groups"],
    "end": "+80m",
    "rules": [{"ref": "ingest-source-silent.yaml", "params": {"quiet_window": "1h"}}],
    "sensors": [FED, TRIAGE],
    "types": {
        "federation_sources": {
            "sources.enabled": "bool",
            "sources.last_success_at": "timestamp",
        },
        "triage": {"sources.arrivals": "counter"},
    },
    "series": [
        s
        for src in ("splunk", "elastic")
        for s in (
            {
                "signal": "federation_sources",
                "key": "sources.enabled",
                "labels": {"source": src},
                "steps": [["+0s", True]],
            },
            {
                "signal": "federation_sources",
                "key": "sources.last_success_at",
                "labels": {"source": src},
                "steps": every(5, 80, ts),
            },
        )
    ]
    + [
        {
            "signal": "triage",
            "key": "sources.arrivals",
            "labels": SPLUNK,
            "steps": every(5, 80, lambda m: m),
        },
        {
            "signal": "triage",
            "key": "sources.arrivals",
            "labels": ELASTIC,
            "steps": [["+0s", 50]],
        },
    ],
    "expect": [
        e(
            "+10m",
            SILENT,
            False,
            "inactive",
            group=SPLUNK,
            note="5 arrivals seen: == 0 is decided false",
        ),
        e("+30m", SILENT, "unknown", "inactive", group=ELASTIC),
        e("+60m", SILENT, True, "pending", group=ELASTIC),
        e("+75m", SILENT, True, "firing", "A", group=ELASTIC),
        e("+75m", SILENT, False, "inactive", group=SPLUNK),
    ],
    "incidents": {"A": inc(SILENT, "+60m", "+75m", group=ELASTIC, routed_at="+75m")},
}

# v16 · group cap and overflow
ERR = rule(
    "ingest.source-erroring",
    "ingest",
    "I-2",
    ["I-2.a"],
    {
        "errors": {
            "sample": {
                "signal": "federation_sources",
                "key": "sources.consecutive_errors",
                "by": ["source"],
            }
        }
    },
    {"fn": "latest", "signal": "errors", "op": ">", "value": 3},
    lane=2,
    group_by=["source"],
    **{"for": "0s", "keep_firing_for": "0s"},
)
OVF = {"source": "__overflow__"}
V["v16-group-overflow"] = {
    "title": "With a cap of 2 groups, the third source lands in the overflow group",
    "covers": ["§3 cap"],
    "end": "+12m",
    "limits": {"group_cap": 2},
    "rules": [{"inline": ERR}],
    "sensors": [FED],
    "types": {"federation_sources": {"sources.consecutive_errors": "gauge"}},
    "series": [
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": {"source": "a"},
            "steps": [["+0s", 0], ["+1m", 5]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": {"source": "b"},
            "steps": [["+0s", 0]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": {"source": "c"},
            "from": "+5m",
            "steps": [["+5m", 0], ["+10m", 5]],
        },
    ],
    "expect": [
        e("+1m", ERR["id"], True, "firing", "A", group={"source": "a"}),
        e("+10m", ERR["id"], False, "inactive", group={"source": "b"}),
        e("+10m", ERR["id"], True, "firing", "O", group=OVF),
        e("+10m", ERR["id"], True, "firing", "A", group={"source": "a"}),
    ],
    "incidents": {
        "A": inc(ERR["id"], "+1m", "+1m", group={"source": "a"}, routed_at="+1m"),
        "O": inc(ERR["id"], "+10m", "+10m", group=OVF, routed_at="+10m"),
    },
}


# v17–v19 · dependency suppression (B5: P-4 is suppressed by any open class-2 incident)
def suppression_vector(
    title: str,
    covers: list,
    set_at: str,
    triaged_resumes: bool,
    end: str,
    expect: list,
    incidents: dict,
) -> dict:
    triaged = [["+0s", 0], ["+5m", 10], ["+10m", 20]]
    if triaged_resumes:
        triaged += [[f"+{m}m", 20 + (m - 55) * 2] for m in range(60, 95, 5)]
    return {
        "title": title,
        "covers": covers,
        "end": end,
        "rules": [
            {"ref": "llm-gateway-outage.yaml"},
            {"ref": "pipeline-triage-stalled.yaml"},
        ],
        "suppression": [{"parent": {"class": "llm"}, "children": {"mode": "P-4"}}],
        "sensors": [DAEMON, LOGS],
        "types": {
            "daemon_status.processor": {"processed": "counter", "triaged": "counter"}
        },
        "series": [
            {
                "signal": "daemon_status.processor",
                "key": "processed",
                "steps": every(5, 95, lambda m: m * 2),
            },
            {"signal": "daemon_status.processor", "key": "triaged", "steps": triaged},
        ],
        "logs": [{**GW_LINE, "at": set_at}, {**GW_OK, "at": "+60m"}],
        "expect": expect,
        "incidents": incidents,
    }


STALL = "pipeline.triage-stalled"
V["v17-suppression-basic"] = suppression_vector(
    "Triage stalls because the LLM gateway is down: the P-4 incident is suppressed",
    ["§6 suppression"],
    "+10m",
    True,
    "+92m",
    [
        e("+12m", GW, True, "firing", "P"),
        e("+40m", STALL, True, "pending"),
        e("+50m", STALL, True, "firing", "C", suppressed=True),
        e("+60m", STALL, False, "resolving", "C", suppressed=True),
        e("+65m", GW, False, "inactive"),
        e(
            "+75m",
            STALL,
            False,
            "resolving",
            "C",
            note="resolving, so it closes quietly",
        ),
        e("+90m", STALL, False, "inactive"),
    ],
    {
        "P": inc(GW, "+10m", "+12m", "+60m", "+65m", routed_at="+12m"),
        "C": inc(
            STALL,
            "+40m",
            "+50m",
            "+60m",
            "+90m",
            reason="closed_quietly",
            suppressed_by="P",
            routed_at=None,
        ),
    },
)
V["v18-suppression-routing-hold"] = suppression_vector(
    "The symptom fires 3 min before the root cause; the 5-min routing hold lets it be suppressed",
    ["§6 routing hold"],
    "+51m",
    True,
    "+92m",
    [
        e(
            "+50m",
            STALL,
            True,
            "firing",
            "C",
            suppressed=False,
            note="held: not routed until +55m",
        ),
        e("+53m", GW, True, "firing", "P"),
        e("+54m", STALL, True, "firing", "C", suppressed=True),
        e("+60m", STALL, False, "resolving", "C", suppressed=True),
        e("+65m", GW, False, "inactive"),
        e("+90m", STALL, False, "inactive"),
    ],
    {
        "P": inc(GW, "+51m", "+53m", "+60m", "+65m", routed_at="+53m"),
        "C": inc(
            STALL,
            "+40m",
            "+50m",
            "+60m",
            "+90m",
            reason="closed_quietly",
            suppressed_by="P",
            routed_at=None,
        ),
    },
)
V["v19-suppression-child-outlives-parent"] = suppression_vector(
    "Triage stays stalled after the gateway recovers: unsuppressed 10 min after the parent resolves",
    ["§6 child outlives parent"],
    "+10m",
    False,
    "+80m",
    [
        e("+50m", STALL, True, "firing", "C", suppressed=True),
        e("+65m", GW, False, "inactive"),
        e("+70m", STALL, True, "firing", "C", suppressed=True),
        e("+75m", STALL, True, "firing", "C", suppressed=False),
    ],
    {
        "P": inc(GW, "+10m", "+12m", "+60m", "+65m", routed_at="+12m"),
        "C": inc(STALL, "+40m", "+50m", suppressed_by="P", routed_at="+75m"),
    },
)

# v20 · upgrade window delays, never hides
DISC = rule(
    "ingest.kafka-disconnected",
    "ingest",
    "I-2",
    ["I-2.a"],
    {"connected": {"sample": {"signal": "daemon_status.kafka", "key": "connected"}}},
    {"fn": "latest", "signal": "connected", "op": "==", "value": False},
    **{"for": "2m"},
)
V["v20-upgrade-window"] = {
    "title": "A version change holds new incidents for 15 min; transients vanish, persistent faults fire at the end",
    "covers": ["§7 upgrade windows"],
    "end": "+40m",
    "rules": [{"ref": "ingest-kafka-decode-errors.yaml"}, {"inline": DISC}],
    "sensors": [DAEMON, VERSION],
    "types": {**KAFKA_TYPES, "version": {"version": "enum"}},
    "series": [
        {
            "signal": "version",
            "key": "version",
            "steps": [["+0s", "1.8.0"], ["+20m", "1.9.0"]],
        },
        {
            "signal": "daemon_status.kafka",
            "key": "decode_errors",
            "steps": [["+0s", 0], ["+18m", 10]],
        },
        {
            "signal": "daemon_status.kafka",
            "key": "missing_id_errors",
            "steps": [["+0s", 0]],
        },
        {
            "signal": "daemon_status.kafka",
            "key": "connected",
            "steps": [["+0s", True], ["+22m", False]],
        },
    ],
    "expect": [
        e("+18m", KAFKA, True, "pending"),
        e(
            "+28m",
            KAFKA,
            True,
            "pending",
            note="for elapsed, but inside the upgrade window",
        ),
        e("+33m", KAFKA, False, "inactive", note="transient: never fired"),
        e("+30m", DISC["id"], True, "pending"),
        e("+35m", DISC["id"], True, "firing", "A", note="window over at +35m"),
    ],
    "incidents": {
        "A": inc(
            DISC["id"], "+22m", "+35m", held_by_upgrade_until="+35m", routed_at="+35m"
        )
    },
}

# v21 · Kleene any/all
FAIL_SIGS = {
    "failed": {"sample": {"signal": "redis_queues", "key": "bull_agent_runs.failed"}},
    "errors": {"sample": {"signal": "daemon_status.processor", "key": "errors"}},
}
CONDS = [
    {"fn": "latest", "signal": "failed", "op": ">", "value": 10},
    {"fn": "latest", "signal": "errors", "op": ">", "value": 10},
]
ANY = rule(
    "pipeline.failures-any",
    "pipeline",
    "P-1",
    ["P-1.e"],
    FAIL_SIGS,
    {"any": CONDS},
    **{"for": "0s"},
)
ALL = rule(
    "pipeline.failures-all",
    "pipeline",
    "P-1",
    ["P-1.e"],
    FAIL_SIGS,
    {"all": CONDS},
    **{"for": "0s"},
)
V["v21-kleene-any-all"] = {
    "title": "any(true, unknown) fires; all(true, unknown) stays unknown",
    "covers": ["§2 Kleene"],
    "end": "+10m",
    "rules": [{"inline": ANY}, {"inline": ALL}],
    "sensors": [REDIS, sensor(DAEMON, [["+0s", "error"]])],
    "types": {
        "redis_queues": {"bull_agent_runs.failed": "gauge"},
        "daemon_status.processor": {"errors": "counter"},
    },
    "series": [
        {
            "signal": "redis_queues",
            "key": "bull_agent_runs.failed",
            "steps": [["+0s", 20]],
        },
        {"signal": "daemon_status.processor", "key": "errors", "steps": [["+0s", 0]]},
    ],
    "expect": [
        e("+1m", ANY["id"], True, "firing", "A"),
        e("+1m", ALL["id"], "unknown", "inactive"),
        e("+10m", ALL["id"], "unknown", "inactive"),
        e("+3m", SBR, True, "firing", "SB", group=sb("http.daemon")),
    ],
    "incidents": {
        "A": inc(ANY["id"], "+0s", "+0s", routed_at="+0s"),
        "SB": inc(SBR, "+30s", "+2m30s", group=sb("http.daemon"), routed_at="+2m30s"),
    },
}

# v22 · absent is a fact for latest, and needs coverage for absent_for
BK = {
    "backup": {
        "sample": {
            "signal": "backend_metrics_backup",
            "key": "vigil_backup_last_success_timestamp",
        }
    }
}
STALE = rule(
    "pipeline.backup-stale",
    "pipeline",
    "P-6",
    ["P-6.c"],
    BK,
    {"fn": "age", "signal": "backup", "op": ">", "value": 86400},
    lane=2,
    **{"for": "0s"},
)
NEVERB = rule(
    "pipeline.backup-never",
    "pipeline",
    "P-6",
    ["P-6.c"],
    BK,
    {"fn": "absent_for", "signal": "backup", "window": "1h"},
    lane=2,
    detection="absence",
    **{"for": "0s"},
)
V["v22-absent-value"] = {
    "title": "An absent backup gauge makes age() false and absent_for(1h) true once covered",
    "covers": ["§2 latest absent", "§2 absent_for"],
    "end": "+60m",
    "rules": [{"inline": STALE}, {"inline": NEVERB}],
    "sensors": [BACKUP],
    "types": {
        "backend_metrics_backup": {"vigil_backup_last_success_timestamp": "timestamp"}
    },
    "series": [
        {
            "signal": "backend_metrics_backup",
            "key": "vigil_backup_last_success_timestamp",
            "steps": [["+0s", None]],
        }
    ],
    "expect": [
        e("+30m", STALE["id"], False, "inactive"),
        e("+30m", NEVERB["id"], "unknown", "inactive"),
        e("+57m45s", NEVERB["id"], "unknown", "inactive"),
        e("+58m", NEVERB["id"], True, "firing", "A"),
    ],
    "incidents": {"A": inc(NEVERB["id"], "+58m", "+58m", routed_at="+58m")},
}

# v23 · group retirement (X1 ⚑2a): a source deleted while its incident is open
GONE = {"source": "gone"}
V["v23-group-retired"] = {
    "title": "A group with no observations for 24 h of covered sensor time retires and its open incident resolves as group_retired",
    "covers": ["§3 group lifetime"],
    "end": "+24h15m",
    "rules": [{"inline": ERR}],
    "sensors": [FED],
    "types": {"federation_sources": {"sources.consecutive_errors": "gauge"}},
    "series": [
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": {"source": "a"},
            "steps": [["+0s", 0]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": GONE,
            "to": "+10m",
            "steps": [["+0s", 5]],
        },
    ],
    "expect": [
        e("+1m", ERR["id"], True, "firing", "A", group=GONE),
        e(
            "+12h",
            ERR["id"],
            "unknown",
            "firing",
            "A",
            group=GONE,
            note="no fresh value: unknown keeps the incident firing (§4)",
        ),
        e("+24h9m45s", ERR["id"], "unknown", "firing", "A", group=GONE),
        e(
            "+24h10m",
            ERR["id"],
            "unknown",
            "inactive",
            group=GONE,
            note="24 h since its last observation (+10m), sensor covered throughout: retired",
        ),
        e("+24h10m", ERR["id"], False, "inactive", group={"source": "a"}),
    ],
    "incidents": {
        "A": inc(
            ERR["id"],
            "+0s",
            "+0s",
            resolved="+24h10m",
            reason="group_retired",
            group=GONE,
            routed_at="+0s",
        )
    },
}

# v24 · what never retires (S0 review R1, PROVISIONAL): a set latch, and the {} group
AZ = {"integration": label_safe("Azure Sentinel")}  # free-text arg → h_ group (X1 ⚑1a)
V["v24-latched-and-ungrouped-never-retire"] = {
    "title": "A set latch and a rule's single {} group stay open through 25 quiet hours",
    "covers": ["§3 group lifetime", "§2 latch", "§3 label-safe values"],
    "end": "+25h",
    "rules": [
        {"ref": "ingest-integration-config-incomplete.yaml"},
        {"ref": "llm-gateway-outage.yaml"},
    ],
    "sensors": [LOGS],
    "logs": [
        {
            "at": "+1m",
            "signal": "log_soc_daemon",
            "level": "WARNING",
            "logger": "core.integrations.sentinel.ingestion",
            "template": CFG_SET,
            "message": "Azure Sentinel configuration incomplete (missing: client_secret); skipping polls until it is completed",
        },
        {**GW_LINE, "at": "+1m"},
    ],
    "expect": [
        e("+3m", CFG, True, "firing", "A", group=AZ),
        e("+3m", GW, True, "firing", "B"),
        e(
            "+24h1m15s",
            CFG,
            True,
            "firing",
            "A",
            group=AZ,
            note="no line for 24 h, but the latch is set: a set latch never retires",
        ),
        e("+25h", GW, True, "firing", "B", note="the {} group never retires"),
    ],
    "incidents": {
        "A": inc(CFG, "+1m", "+3m", group=AZ, routed_at="+3m"),
        "B": inc(GW, "+1m", "+3m", routed_at="+3m"),
    },
}

# v25 · a resolving incident retires; a group that comes back starts fresh
ERR_KEEP = {**ERR, "id": "ingest.source-erroring-slow", "keep_firing_for": "1h"}
RES, BACK = {"source": "res"}, {"source": "back"}
V["v25-retire-resolving-and-return"] = {
    "title": "A resolving incident retires with its group; the same group returning later opens a new incident",
    "covers": ["§3 group lifetime"],
    "end": "+24h30m",
    "rules": [{"inline": ERR_KEEP}],
    "sensors": [FED],
    "types": {"federation_sources": {"sources.consecutive_errors": "gauge"}},
    "series": [
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": {"source": "a"},
            "steps": [["+0s", 0]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": RES,
            "to": "+10m",
            "steps": [["+0s", 5], ["+5m", 0]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": BACK,
            "to": "+10m",
            "steps": [["+0s", 5]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": BACK,
            "from": "+24h20m",
            "steps": [["+24h20m", 5]],
        },
    ],
    "expect": [
        e("+5m", ERR_KEEP["id"], False, "resolving", "R", group=RES),
        e(
            "+12h",
            ERR_KEEP["id"],
            "unknown",
            "resolving",
            "R",
            group=RES,
            note="unknown holds a resolving incident open (§4)",
        ),
        e("+24h10m", ERR_KEEP["id"], "unknown", "inactive", group=RES),
        e("+24h10m", ERR_KEEP["id"], "unknown", "inactive", group=BACK),
        e(
            "+24h20m",
            ERR_KEEP["id"],
            True,
            "firing",
            "B2",
            group=BACK,
            note="a returning group starts fresh: a new incident",
        ),
    ],
    "incidents": {
        "R": inc(
            ERR_KEEP["id"],
            "+0s",
            "+0s",
            resolving="+5m",
            resolved="+24h10m",
            reason="group_retired",
            group=RES,
            routed_at="+0s",
        ),
        "B1": inc(
            ERR_KEEP["id"],
            "+0s",
            "+0s",
            resolved="+24h10m",
            reason="group_retired",
            group=BACK,
            routed_at="+0s",
        ),
        "B2": inc(
            ERR_KEEP["id"], "+24h20m", "+24h20m", group=BACK, routed_at="+24h20m"
        ),
    },
}

# v26 · a blind period restarts the 24 h
V["v26-blind-restarts-retirement"] = {
    "title": "Retirement needs 24 h of covered silence: a blind period in the middle restarts it",
    "covers": ["§3 group lifetime", "§2 coverage"],
    "end": "+36h45m",
    "rules": [{"inline": ERR}],
    "sensors": [sensor(FED, [["+0s", "ok"], ["+12h", "error"], ["+12h30m", "ok"]])],
    "types": {"federation_sources": {"sources.consecutive_errors": "gauge"}},
    "series": [
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": {"source": "a"},
            "steps": [["+0s", 0]],
        },
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "labels": GONE,
            "to": "+10m",
            "steps": [["+0s", 5]],
        },
    ],
    "expect": [
        e("+1m", ERR["id"], True, "firing", "A", group=GONE),
        e(
            "+24h10m",
            ERR["id"],
            "unknown",
            "firing",
            "A",
            group=GONE,
            note="24 h since its last observation, but the sensor was blind 12h–12h30m",
        ),
        e(
            "+36h30m",
            ERR["id"],
            "unknown",
            "inactive",
            group=GONE,
            note="24 h of covered silence after the blind period ended",
        ),
    ],
    "incidents": {
        "A": inc(
            ERR["id"],
            "+0s",
            "+0s",
            resolved="+36h30m",
            reason="group_retired",
            group=GONE,
            routed_at="+0s",
        )
    },
}

# v27 · the {} group of an ungrouped rule never retires, even with no data for 25 h
ERR0 = rule(
    "ingest.kafka-consumer-erroring",
    "ingest",
    "I-2",
    ["I-2.a"],
    {
        "errors": {
            "sample": {
                "signal": "federation_sources",
                "key": "sources.consecutive_errors",
            }
        }
    },
    {"fn": "latest", "signal": "errors", "op": ">", "value": 3},
    lane=3,
    **{"for": "0s", "keep_firing_for": "0s"},
)
V["v27-ungrouped-never-retires"] = {
    "title": "A rule without group_by has one {} group, which never retires even after 25 h with no observations",
    "covers": ["§3 group lifetime"],
    "end": "+25h",
    "rules": [{"inline": ERR0}],
    "sensors": [FED],
    "types": {"federation_sources": {"sources.consecutive_errors": "gauge"}},
    "series": [
        {
            "signal": "federation_sources",
            "key": "sources.consecutive_errors",
            "to": "+10m",
            "steps": [["+0s", 5]],
        }
    ],
    "expect": [
        e("+1m", ERR0["id"], True, "firing", "A"),
        e(
            "+25h",
            ERR0["id"],
            "unknown",
            "firing",
            "A",
            note="{} never retires; unknown keeps it firing",
        ),
    ],
    "incidents": {"A": inc(ERR0["id"], "+0s", "+0s", routed_at="+0s")},
}

for vid, body in V.items():
    doc = {
        "id": vid,
        "title": body.pop("title"),
        "covers": body.pop("covers"),
        "start": START,
        **body,
    }
    (OUT / f"{vid}.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)
    )
print(len(V), "vectors")
