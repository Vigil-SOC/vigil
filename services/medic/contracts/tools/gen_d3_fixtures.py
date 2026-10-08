"""Regenerates the D3 fixtures. Run from contracts/: uv run python tools/gen_d3_fixtures.py"""

import copy
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from fingerprint_ref import LogLine, fingerprint

OUT = Path("fixtures/observations")
(OUT / "valid").mkdir(parents=True, exist_ok=True)
(OUT / "invalid").mkdir(parents=True, exist_ok=True)
FP = Path("fixtures/fingerprint")
FP.mkdir(parents=True, exist_ok=True)
RUN = "01JA8Z3K4M"
seq = {"n": 0}


def base(
    kind,
    signal,
    sensor,
    service,
    t,
    source_ts=None,
    shape="compose",
    instance="deeptempo-soc-daemon",
):
    seq["n"] += 1
    o = {
        "v": 1,
        "id": f"{sensor}:{RUN}:{seq['n']}",
        "kind": kind,
        "signal": signal,
        "sensor": {"id": sensor, "run": RUN, "seq": seq["n"]},
        "target": {"service": service, "instance": instance, "shape": shape},
        "t": t,
        "observed_at": t,
        "source_ts": source_ts,
        "outcome": "ok",
    }
    if kind != "sensor_health":
        o["redaction"] = {"version": "k2-0", "hits": 0}
    return o


def val(key, typ, value, trust="trusted", state="present", labels=None):
    v = {"key": key, "type": typ, "state": state, "value": value, "trust": trust}
    if labels:
        v["labels"] = labels
    return v


def logobs(signal, service, line, catalog, t, source_ts, instance, trace=None):
    f = fingerprint(line, catalog)
    o = base(
        "log",
        signal,
        f"log.{service}",
        service,
        source_ts,
        source_ts,
        instance=instance,
    )
    o["observed_at"] = t
    o["skew_ms"] = int((iso(source_ts) - iso(t)) * 1000)
    msg = line.message if line.format != "text" else line.raw
    redacted = msg[:500]
    lh = hashlib.sha256(
        f"{service}|{instance}|{source_ts}|{redacted}".encode()
    ).hexdigest()[:16]
    o["log"] = {
        "format": line.format,
        "level": f.level,
        "logger": f.logger or None,
        "template": line.template,
        "template_trust": f.template_trust,
        "message": redacted,
        "exc_type": line.exc_type,
        "frames": list(f.frames),
        "fingerprint": f.fingerprint,
        "fingerprint_basis": f.basis,
        "trace_id": trace,
        "line_hash": lh,
        "truncated": False,
    }
    if f.args:
        o["log"]["args"] = list(f.args)
    return o


from datetime import datetime


def iso(s):
    return datetime.fromisoformat(s).timestamp()


V = {}
# endpoint · JSON · counters with epoch (D1 daemon_status.kafka)
o = base(
    "sample",
    "daemon_status.kafka",
    "http.daemon_status",
    "soc-daemon",
    "2026-10-07T10:00:15Z",
)
o["epoch"] = "uptime:2026-10-07T08:12:03Z"
o["values"] = [
    val("connected", "bool", True),
    val("messages_consumed", "counter", 18233),
    val("decode_errors", "counter", 41),
    val("missing_id_errors", "counter", 0),
    val("last_message_at", "timestamp", "2026-10-07T10:00:11Z"),
    val(
        "last_error",
        "text",
        "Expecting value: line 1 column 1 (char 0)",
        trust="untrusted",
    ),
]
V["endpoint-json-daemon-status-kafka"] = o
# endpoint · Prometheus text · series absent until first backup (D1 backend_metrics_backup)
o = base(
    "sample",
    "backend_metrics_backup",
    "http.backend_metrics",
    "backend",
    "2026-10-07T10:00:30Z",
    instance="deeptempo-backend",
)
o["values"] = [
    val("vigil_backup_last_success_timestamp", "gauge", None, state="absent")
]
V["endpoint-prometheus-backup-absent"] = o
# endpoint · read error, never zero (D1 triage)
o = base(
    "sample",
    "triage",
    "http.triage",
    "backend",
    "2026-10-07T10:00:30Z",
    instance="deeptempo-backend",
)
o["outcome"] = "error"
o["values"] = []
o["error"] = {
    "class": "http_status",
    "http_status": 503,
    "detail": "upstream database unavailable",
}
V["endpoint-error-triage-503"] = o
# endpoint · per-source rows with labels (D1 federation_sources)
o = base(
    "sample",
    "federation_sources",
    "http.federation_sources",
    "backend",
    "2026-10-07T10:00:30Z",
    instance="deeptempo-backend",
)
o["epoch"] = "backend:2026-10-07T08:11:50Z"
o["values"] = [
    val("sources.enabled", "bool", True, labels={"source": "splunk"}),
    val(
        "sources.last_success_at",
        "timestamp",
        "2026-10-07T09:59:58Z",
        labels={"source": "splunk"},
    ),
    val("sources.consecutive_errors", "gauge", 0, labels={"source": "splunk"}),
    val("sources.dropped_total", "counter", 0, labels={"source": "splunk"}),
]
V["endpoint-json-federation-sources"] = o
# datastore · SQL view (D1 sql_findings_arrivals)
o = base(
    "sample",
    "sql_findings_arrivals",
    "sql.findings_arrivals",
    "postgres",
    "2026-10-07T10:01:00Z",
    instance="deeptempo-postgres",
)
o["values"] = [
    val("arrivals_10m", "gauge", 7, labels={"data_source": "crowdstrike"}),
    val("arrivals_10m", "gauge", 0, labels={"data_source": "sentinel"}),
]
V["datastore-sql-findings-arrivals"] = o
# datastore · Redis (D1 redis_queues)
o = base(
    "sample",
    "redis_queues",
    "redis.queues",
    "redis",
    "2026-10-07T10:00:15Z",
    instance="deeptempo-redis",
)
o["values"] = [
    val("arq_llm.zcard", "gauge", 3),
    val("arq_llm.oldest_score_ms", "gauge", 1791375600000),
    val("bull_agent_runs.wait", "gauge", 0),
    val("bull_agent_runs.failed", "gauge", 12),
]
V["datastore-redis-queues"] = o
# platform · containers (D1 containers)
o = base(
    "sample", "containers", "docker.containers", "soc-daemon", "2026-10-07T10:00:15Z"
)
o["epoch"] = "container:2026-10-07T08:12:01Z"
o["values"] = [
    val("state", "enum", "running"),
    val("health", "enum", "healthy"),
    val("restart_count", "counter", 0),
    val("started_at", "timestamp", "2026-10-07T08:12:01Z"),
]
V["platform-containers"] = o
# file · pid files, host-native (D1 pid_files)
o = base(
    "sample",
    "pid_files",
    "file.pid_files",
    "soc-daemon",
    "2026-10-07T10:00:15Z",
    shape="start_sh",
    instance="vigil-host-1",
)
o["values"] = [
    val("daemon_pid", "gauge", 48121),
    val("daemon_pid_alive", "bool", True),
    val("port_9091_pid_matches", "bool", False),
]
V["file-pid-files"] = o
# host · disk (D1 host_disk)
o = base(
    "sample",
    "host_disk",
    "host.disk",
    "host",
    "2026-10-07T10:00:00Z",
    shape="start_sh",
    instance="vigil-host-1",
)
o["values"] = [
    val("free_bytes", "gauge", 5368709120, labels={"mount": "/var/lib/vigil"}),
    val("free_ratio", "gauge", 0.07, labels={"mount": "/var/lib/vigil"}),
]
V["host-disk"] = o

CAT = {
    (
        "core.integrations.elastic.ingestion",
        "%s configuration incomplete (missing: %s); skipping polls until it is completed",
    )
}
T = "%s configuration incomplete (missing: %s); skipping polls until it is completed"
V["log-python-json-catalog-template"] = logobs(
    "log_soc_daemon",
    "soc-daemon",
    LogLine(
        "soc-daemon",
        "python_json",
        "WARNING",
        "core.integrations.elastic.ingestion",
        T,
        "Elastic configuration incomplete (missing: elasticsearch_url); skipping polls until it is completed",
    ),
    CAT,
    "2026-10-07T10:00:02.120000Z",
    "2026-10-07T10:00:01.998000Z",
    "deeptempo-soc-daemon",
)
V["log-python-json-fstring-exception"] = logobs(
    "log_backend",
    "backend",
    LogLine(
        "backend",
        "python_json",
        "ERROR",
        "core.storage.database_data_service",
        'Error getting findings from DB: (psycopg2.OperationalError) connection to server at "<redacted-host>", port 5432 failed: Connection refused',
        'Error getting findings from DB: (psycopg2.OperationalError) connection to server at "<redacted-host>", port 5432 failed: Connection refused',
        "sqlalchemy.exc.OperationalError",
        'Traceback (most recent call last):\n  File "/app/core/storage/database_data_service.py", line 151, in get_findings\n    rows = self.db.get_findings()\n  File "/app/core/storage/service.py", line 88, in get_findings\n    rows = s.execute(q)\n  File "/usr/local/lib/python3.12/site-packages/sqlalchemy/orm/session.py", line 2306, in execute\n    return x\nsqlalchemy.exc.OperationalError: boom',
    ),
    set(),
    "2026-10-07T10:00:05Z",
    "2026-10-07T10:00:04.870000Z",
    "deeptempo-backend",
    trace="4bf92f3577b34da6a3ce929d0e0e4736",
)
V["log-agent-json"] = logobs(
    "log_agent_worker",
    "agent-worker",
    LogLine(
        "agent-worker", "agent_json", "warn", "lease", "sweep failed", "sweep failed"
    ),
    set(),
    "2026-10-07T10:00:07Z",
    "2026-10-07T10:00:06.500000Z",
    "deeptempo-agent-worker",
)
o = logobs(
    "log_soc_daemon",
    "soc-daemon",
    LogLine(
        "soc-daemon",
        "text",
        raw="2026-10-07 10:00:01,123 - services.daemon.poller - WARNING - Splunk query failed (401): https://splunk.example.com:8089/services/search",
    ),
    set(),
    "2026-10-07T10:00:02Z",
    "2026-10-07T10:00:01.123000Z",
    "vigil-host-1",
)
o["target"]["shape"] = "start_sh"
V["log-text-start-sh"] = o
# log · clock skew: source 9 min ahead, so t falls back to observed_at
o = copy.deepcopy(V["log-agent-json"])
seq["n"] += 1
o["id"] = f"log.agent-worker:{RUN}:{seq['n']}"
o["sensor"]["seq"] = seq["n"]
o["source_ts"] = "2026-10-07T10:09:07Z"
o["skew_ms"] = 540000
o["t"] = o["observed_at"]
o["skew_flag"] = True
V["log-skewed-source-clock"] = o


def health(
    sensor, covers, state, by, last_ok, fails, counters, t, err=None, interval=15
):
    o = base(
        "sensor_health",
        "sensor_health",
        by == "framework" and "framework" or sensor,
        "medic",
        t,
        instance="deeptempo-medic",
    )
    o["health"] = {
        "sensor": sensor,
        "state": state,
        "reported_by": by,
        "covers": covers,
        "interval_s": interval,
        "last_ok_at": last_ok,
        "consecutive_failures": fails,
        "counters": counters,
    }
    if err:
        o["health"]["last_error"] = err
    return o


V["sensor-health-ok"] = health(
    "http.daemon_status",
    ["daemon_status.kafka", "daemon_status.poller"],
    "ok",
    "sensor",
    "2026-10-07T10:00:15Z",
    0,
    {"reads": 488, "failures": 0, "dropped": 0, "redactor_failures": 0},
    "2026-10-07T10:00:15Z",
)
V["sensor-health-blind-401"] = health(
    "http.triage",
    ["triage"],
    "blind",
    "sensor",
    "2026-10-07T09:58:30Z",
    6,
    {"reads": 240, "failures": 6, "dropped": 0, "redactor_failures": 0},
    "2026-10-07T10:01:30Z",
    err={"class": "auth", "http_status": 401},
    interval=30,
)
V["sensor-health-stopped-by-framework"] = health(
    "log.agent-worker",
    ["log_agent_worker"],
    "stopped",
    "framework",
    "2026-10-07T09:55:00Z",
    0,
    {"reads": 0, "failures": 0, "dropped": 112, "redactor_failures": 0},
    "2026-10-07T10:00:00Z",
)

for k, o in V.items():
    (OUT / "valid" / f"{k}.json").write_text(json.dumps(o, indent=2) + "\n")


# Invalid: each mutates one valid fixture and names the rule it breaks.
def bad(name, src, fn, why):
    o = copy.deepcopy(V[src])
    fn(o)
    o["$comment"] = why
    (OUT / "invalid" / f"{name}.json").write_text(json.dumps(o, indent=2) + "\n")


bad(
    "counter-without-epoch",
    "endpoint-json-daemon-status-kafka",
    lambda o: o.pop("epoch"),
    "A counter needs an epoch so resets are visible",
)
bad(
    "absent-with-value",
    "endpoint-prometheus-backup-absent",
    lambda o: o["values"][0].update(value=0),
    "Absent must not be confused with zero",
)
bad(
    "text-marked-trusted",
    "endpoint-json-daemon-status-kafka",
    lambda o: o["values"][5].update(trust="trusted"),
    "Free text is always untrusted (K1 T-05)",
)
bad(
    "error-with-values",
    "endpoint-error-triage-503",
    lambda o: o.update(values=[val("x", "gauge", 0)]),
    "A failed read carries no values, so it can't read as zero",
)
bad(
    "ok-with-error",
    "datastore-redis-queues",
    lambda o: o.update(error={"class": "timeout"}),
    "outcome ok must not carry an error",
)
bad(
    "stopped-reported-by-sensor",
    "sensor-health-stopped-by-framework",
    lambda o: o["health"].update(reported_by="sensor"),
    "A stopped sensor can't report itself",
)
bad(
    "ok-health-with-failures",
    "sensor-health-ok",
    lambda o: o["health"].update(consecutive_failures=2),
    "ok means the last read worked",
)
bad(
    "label-injection",
    "endpoint-json-federation-sources",
    lambda o: o["values"][0]["labels"].update(source="splunk\nERROR fake line"),
    "Label values are a closed charset",
)
bad(
    "raw-traceback-stored",
    "log-python-json-fstring-exception",
    lambda o: o["log"].update(exception="Traceback ..."),
    "Raw tracebacks are not stored (K1 T-01 minimise)",
)
bad(
    "bad-fingerprint",
    "log-agent-json",
    lambda o: o["log"].update(fingerprint="abc"),
    "Fingerprint must be fp1_ + 16 hex",
)
bad(
    "unknown-top-level-field",
    "host-disk",
    lambda o: o.update(note="hi"),
    "Unknown fields are refused",
)
bad(
    "non-utc-timestamp",
    "host-disk",
    lambda o: o.update(t="2026-10-07T12:00:00+02:00"),
    "Times are UTC with Z",
)
bad(
    "message-too-long",
    "log-agent-json",
    lambda o: o["log"].update(message="x" * 501),
    "Excerpts are capped at 500 chars",
)
bad(
    "log-without-redaction",
    "log-agent-json",
    lambda o: o.pop("redaction"),
    "Every sample and log passes the redactor (K2)",
)
bad(
    "sensor-health-wrong-signal",
    "sensor-health-ok",
    lambda o: o.update(signal="daemon_status"),
    "Heartbeats use signal sensor_health",
)

# Fingerprint examples: groups of lines that must share a fingerprint; frozen expected values.
groups = {
    "catalog-template-different-args": (
        [
            LogLine(
                "soc-daemon",
                "python_json",
                "WARNING",
                "core.integrations.elastic.ingestion",
                T,
                "Elastic configuration incomplete (missing: elasticsearch_url); skipping polls until it is completed",
            ),
            LogLine(
                "soc-daemon",
                "python_json",
                "WARNING",
                "core.integrations.elastic.ingestion",
                T,
                "Elastic configuration incomplete (missing: api_key, elasticsearch_url); skipping polls until it is completed",
            ),
        ],
        True,
    ),
    "fstring-ids-and-numbers": (
        [
            LogLine(
                "soc-daemon",
                "python_json",
                "ERROR",
                "services.daemon.orchestrator",
                "Failed to save investigation 3f2a9c1e-1111-2222-3333-444455556666 to DB after 3 attempts",
                "Failed to save investigation 3f2a9c1e-1111-2222-3333-444455556666 to DB after 3 attempts",
            ),
            LogLine(
                "soc-daemon",
                "python_json",
                "ERROR",
                "services.daemon.orchestrator",
                "Failed to save investigation 0b0b0b0b-aaaa-bbbb-cccc-ddddeeeeffff to DB after 12 attempts",
                "Failed to save investigation 0b0b0b0b-aaaa-bbbb-cccc-ddddeeeeffff to DB after 12 attempts",
            ),
        ],
        False,
    ),
    "fstring-hosts-ports-quoted": (
        [
            LogLine(
                "backend",
                "python_json",
                "ERROR",
                "core.storage.database_data_service",
                'Error getting findings from DB: connection to server at "10.0.0.5", port 5432 failed: Connection refused',
                'Error getting findings from DB: connection to server at "10.0.0.5", port 5432 failed: Connection refused',
            ),
            LogLine(
                "backend",
                "python_json",
                "ERROR",
                "core.storage.database_data_service",
                'Error getting findings from DB: connection to server at "db.internal", port 6432 failed: Connection refused',
                'Error getting findings from DB: connection to server at "db.internal", port 6432 failed: Connection refused',
            ),
        ],
        False,
    ),
    "text-urls-timestamps": (
        [
            LogLine(
                "soc-daemon",
                "text",
                raw="2026-10-07 10:00:01,123 - services.daemon.poller - WARNING - Splunk query failed (401): https://splunk.a.example:8089/services/search at 2026-10-07T10:00:01Z",
            ),
            LogLine(
                "soc-daemon",
                "text",
                raw="2026-10-08 23:59:59,999 - services.daemon.poller - WARNING - Splunk query failed (403): https://splunk.b.example/services/search at 2026-10-08T23:59:59.5Z",
            ),
        ],
        False,
    ),
    "same-exception-different-line-numbers": (
        [
            LogLine(
                "backend",
                "python_json",
                "ERROR",
                "core.storage.service",
                "%s failed",
                "DatabaseService.get_findings failed",
                "sqlalchemy.exc.OperationalError",
                'Traceback (most recent call last):\n  File "/app/core/storage/service.py", line 88, in get_findings\n    x\n  File "/usr/local/lib/python3.12/site-packages/sqlalchemy/orm/session.py", line 2306, in execute\n    y\nE: boom',
            ),
            LogLine(
                "backend",
                "python_json",
                "ERROR",
                "core.storage.service",
                "%s failed",
                "DatabaseService.get_findings failed",
                "sqlalchemy.exc.OperationalError",
                'Traceback (most recent call last):\n  File "/opt/vigil/core/storage/service.py", line 97, in get_findings\n    x\n  File "/venv/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 2410, in execute\n    y\nE: different text',
            ),
        ],
        True,
    ),
}
must_differ = [
    (
        "level-matters",
        LogLine(
            "soc-daemon", "python_json", "WARNING", "a.b", "x happened", "x happened"
        ),
        LogLine(
            "soc-daemon", "python_json", "ERROR", "a.b", "x happened", "x happened"
        ),
    ),
    (
        "service-matters",
        LogLine("backend", "python_json", "ERROR", "a.b", "x happened", "x happened"),
        LogLine(
            "soc-daemon", "python_json", "ERROR", "a.b", "x happened", "x happened"
        ),
    ),
    (
        "exc-type-matters",
        LogLine(
            "backend",
            "python_json",
            "ERROR",
            "core.storage.service",
            "%s failed",
            "f failed",
            "sqlalchemy.exc.OperationalError",
        ),
        LogLine(
            "backend",
            "python_json",
            "ERROR",
            "core.storage.service",
            "%s failed",
            "f failed",
            "sqlalchemy.exc.IntegrityError",
        ),
    ),
    (
        "fstring-matching-catalog-is-untrusted",
        LogLine(
            "soc-daemon",
            "python_json",
            "WARNING",
            "core.integrations.elastic.ingestion",
            T,
            "Elastic configuration incomplete (missing: x); skipping polls until it is completed",
        ),
        LogLine(
            "soc-daemon",
            "python_json",
            "WARNING",
            "core.integrations.elastic.ingestion",
            T,
            T,
        ),
    ),
]
CATL = [list(c) for c in CAT]


def ser(l):
    return {k: v for k, v in l.__dict__.items() if v is not None}


ex = {"algorithm": "fp1", "catalog": CATL, "same": [], "differ": []}
for name, (lines, cat) in groups.items():
    fps = [fingerprint(l, CAT if cat else set()).fingerprint for l in lines]
    assert len(set(fps)) == 1, (name, fps)
    ex["same"].append(
        {
            "name": name,
            "use_catalog": cat,
            "expected": fps[0],
            "lines": [ser(l) for l in lines],
        }
    )
for name, a, b in must_differ:
    fa, fb = fingerprint(a, CAT).fingerprint, fingerprint(b, CAT).fingerprint
    assert fa != fb, name
    ex["differ"].append(
        {
            "name": name,
            "use_catalog": True,
            "expected": [fa, fb],
            "lines": [ser(a), ser(b)],
        }
    )
(FP / "examples.json").write_text(json.dumps(ex, indent=2) + "\n")
print(
    len(V),
    "valid;",
    len(list((OUT / "invalid").glob("*.json"))),
    "invalid;",
    len(ex["same"]),
    "same-groups;",
    len(ex["differ"]),
    "differ-pairs",
)
