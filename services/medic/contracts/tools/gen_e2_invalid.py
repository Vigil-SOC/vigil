"""Regenerates contracts/fixtures/rules/invalid/. Run from contracts/:
uv run python tools/gen_e2_invalid.py
Each file's first line names the one error code the loader must report."""

import copy
from pathlib import Path

import yaml

OUT = Path("fixtures/rules/invalid")
OUT.mkdir(parents=True, exist_ok=True)
BASE = yaml.safe_load(
    Path("fixtures/rules/valid/ingest-kafka-decode-errors.yaml").read_text()
)


def emit(name: str, code: str, why: str, mutate=None, text: str | None = None) -> None:
    if text is None:
        rule = copy.deepcopy(BASE)
        mutate(rule)
        text = yaml.safe_dump(rule, sort_keys=False, allow_unicode=True)
    (OUT / f"{name}.yaml").write_text(f"# expect: {code}\n# why: {why}\n{text}")


def setp(path, value):
    def f(rule):
        node = rule
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value

    return f


emit(
    "code-in-rule",
    "E-SCHEMA",
    "No code: unknown key 'expr' (K1 T-20)",
    setp(["expr"], "__import__('os').system('id')"),
)
emit(
    "template-in-fix",
    "E-SCHEMA",
    "Fix text is static (K1 T-05)",
    setp(["advice", "fix"], "Run {{ $labels.message }} to fix it"),
)
emit(
    "missing-lane",
    "E-SCHEMA",
    "Every rule carries a lane (G1 consumes it)",
    lambda r: r.pop("lane"),
)
emit(
    "unknown-function",
    "E-SCHEMA",
    "Only engine API 1.0 functions",
    setp(["when", "any", 0, "fn"], "eval"),
)
emit(
    "author-lowers-trust",
    "E-SCHEMA",
    "Authors can only raise input_trust (E1 4a)",
    setp(["input_trust"], "trusted"),
)
emit(
    "unknown-signal",
    "E-SIGNAL-UNKNOWN",
    "Signals must be D1 signal IDs",
    setp(["signals", "decode_errors", "sample", "signal"], "daemon_stats.kafka"),
)
emit(
    "undefined-reference",
    "E-REF",
    "Conditions may only name declared signals",
    setp(["when", "any", 1, "signal"], "missing_ids"),
)
emit(
    "window-too-long",
    "E-WINDOW",
    "Windows are capped at 48h",
    setp(["when", "any", 0, "window"], "72h"),
)
emit(
    "too-deep",
    "E-DEPTH",
    "Nesting is capped at 3 levels",
    setp(
        ["when"],
        {
            "all": [
                {
                    "any": [
                        {
                            "not": {
                                "all": [
                                    {
                                        "fn": "increase",
                                        "signal": "decode_errors",
                                        "window": "15m",
                                        "op": ">",
                                        "value": 5,
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "fn": "increase",
                    "signal": "missing_id",
                    "window": "15m",
                    "op": ">",
                    "value": 5,
                },
            ]
        },
    ),
)
emit(
    "group-by-unknown",
    "E-GROUP",
    "group_by must name a signal's by-label or log key",
    setp(["group_by"], ["topic"]),
)


def lane1_single_log(rule):
    rule["lane"] = 1
    rule["signals"] = {
        "line": {
            "log": {
                "signal": "log_soc_daemon",
                "template": {"eq": "Kafka consumer poll error: %s"},
            }
        }
    }
    rule["when"] = {
        "fn": "count",
        "signal": "line",
        "window": "5m",
        "op": ">=",
        "value": 1,
    }


emit(
    "lane1-single-log-line",
    "E-LANE1",
    "No single log line alone may reach lane 1 (K1 T-05 (2))",
    lane1_single_log,
)


def fn_type(rule):
    rule["signals"]["line"] = {
        "log": {
            "signal": "log_soc_daemon",
            "template": {"eq": "Kafka consumer poll error: %s"},
        }
    }
    rule["when"]["any"].append(
        {"fn": "increase", "signal": "line", "window": "15m", "op": ">", "value": 1}
    )


emit(
    "fn-type-mismatch",
    "E-FN-TYPE",
    "increase needs a sample signal, not log lines",
    fn_type,
)
emit(
    "backtracking-regex",
    "E-RE2",
    "Linear-time regex only (K1 T-10)",
    text="""\
apiVersion: medic.rules/v1
kind: Rule
id: ingest.bad-regex
title: Regex that needs a backtracking engine
revision: 1
fault: {class: ingest, mode: I-1, causes: [I-1.b]}
lane: 2
signals:
  line: {log: {signal: log_soc_daemon, template: {re2: '^(a+)+\\1$'}}}
when: {fn: count, signal: line, window: 5m, op: ">=", value: 1}
advice: {summary: Bad regex rule, fix: Nothing to do.}
""",
)
emit(
    "huge-regex",
    "E-RE2",
    "RE2 program size is capped",
    text="""\
apiVersion: medic.rules/v1
kind: Rule
id: ingest.huge-regex
title: Regex too large to compile cheaply
revision: 1
fault: {class: ingest, mode: I-1, causes: [I-1.b]}
lane: 2
signals:
  line: {log: {signal: log_soc_daemon, template: {re2: '(abc|def|ghi){1,300}'}}}
when: {fn: count, signal: line, window: 5m, op: ">=", value: 1}
advice: {summary: Huge regex rule, fix: Nothing to do.}
""",
)
emit(
    "yaml-alias",
    "E-YAML-ALIAS",
    "YAML aliases are refused (billion laughs)",
    text="""\
apiVersion: medic.rules/v1
kind: Rule
id: ingest.alias
title: Rule using YAML aliases
revision: 1
fault: &f {class: ingest, mode: I-3, causes: [I-3.j]}
lane: 2
signals:
  d: {sample: {signal: daemon_status.kafka, key: decode_errors}}
when: {fn: increase, signal: d, window: 15m, op: ">", value: 5}
advice: {summary: *f, fix: x}
""",
)
emit(
    "yaml-duplicate-key",
    "E-YAML-DUPKEY",
    "A reviewer would read one lane, the engine another",
    text="""\
apiVersion: medic.rules/v1
kind: Rule
id: ingest.dupkey
title: Rule with a duplicated key
revision: 1
fault: {class: ingest, mode: I-3, causes: [I-3.j]}
lane: 3
signals:
  d: {sample: {signal: daemon_status.kafka, key: decode_errors}}
when: {fn: increase, signal: d, window: 15m, op: ">", value: 5}
advice: {summary: Duplicate key rule, fix: Nothing to do.}
lane: 1
""",
)
emit(
    "yaml-python-tag",
    "E-YAML",
    "YAML tags that construct objects are refused",
    text="""\
apiVersion: medic.rules/v1
kind: Rule
id: ingest.tag
title: !!python/object/apply:os.system ["id"]
revision: 1
""",
)
print(len(list(OUT.glob("*.yaml"))), "invalid fixtures")
