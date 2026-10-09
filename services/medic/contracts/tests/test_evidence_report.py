"""X3 contract: evidence report medic.evidence/v1 (K1 T-31, T-17; H5 row of K1 section 6)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from evidence_report_check import MAX_BYTES, SCHEMA, canonical, check, sha256

FIX = ROOT / "fixtures" / "evidence-reports"
VALID = sorted((FIX / "valid").glob("*.json"))
INVALID = sorted((FIX / "invalid").glob("*.json"))
KNOWN = frozenset(tuple(k) for k in json.loads((FIX / "known-rules.json").read_text()))
ALLOWED_TYPES = {"enum", "number", "id", "timestamp", "hash"}
LOG_SHAPES = [
    r"\d{2}:\d{2}:\d{2}",
    r"(ERROR|WARNING|INFO|DEBUG)",
    r"https?://",
    r"\d+\.\d+\.\d+\.\d+",
    r"[ \"]",
]


def base() -> bytes:
    return (FIX / "valid" / "partner-week-compose.json").read_bytes()


# --- schema and fixtures -------------------------------------------------------------


def test_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture_passes_byte_for_byte(path: Path) -> None:
    assert check(path.read_bytes(), KNOWN) == []


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_fails_with_its_code(path: Path) -> None:
    doc = json.loads(path.read_text())
    assert check(canonical(doc["report"]), KNOWN) == [doc["expect"]], doc["$comment"]


def test_the_required_invalid_fixtures_exist() -> None:
    names = {p.stem for p in INVALID}
    assert {"smuggled-free-text-top-level", "smuggled-free-text-in-row"} <= names
    assert {"raw-log-line-in-id-field", "raw-log-line-as-evidence"} <= names


# --- K1 T-31: closed schema, no free text ------------------------------------------------


def _walk(node, path="$"):
    """Yield every schema node with its path ($defs resolved where referenced)."""
    if isinstance(node, dict):
        yield path, node
        for key, sub in node.items():
            yield from _walk(sub, f"{path}.{key}")
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            yield from _walk(sub, f"{path}[{i}]")


def test_every_object_is_closed() -> None:
    open_objects = [
        p
        for p, n in _walk(SCHEMA)
        if (n.get("type") == "object" or "properties" in n)
        and n.get("additionalProperties") is not False
    ]
    assert open_objects == []


def test_every_string_is_an_enum_const_or_anchored_pattern() -> None:
    loose = []
    for p, n in _walk(SCHEMA):
        if n.get("type") == "string":
            pat = n.get("pattern", "")
            anchored = (
                pat.startswith("^")
                and pat.endswith("$")
                and ".*" not in pat
                and "\\s" not in pat
            )
            if not anchored and "enum" not in n and "const" not in n:
                loose.append(p)
    assert loose == [], f"free-text-capable strings: {loose}"


def test_no_pattern_admits_a_space_or_quote() -> None:
    for p, n in _walk(SCHEMA):
        if "pattern" in n:
            for ch in (" ", '"', "'", "<", "="):
                assert not re.search(n["pattern"], ch * 3), (p, ch)


def test_every_leaf_carries_an_allowed_type_tag() -> None:
    leaves = [(p, n) for p, n in _walk(SCHEMA) if "x-medic-type" in n]
    assert leaves, "no tagged leaves found"
    assert {
        n["x-medic-type"] for _, n in leaves
    } <= ALLOWED_TYPES  # never excerpt or pack_text
    for p, n in _walk(SCHEMA):
        is_leaf = (
            n.get("type") in {"string", "integer", "boolean", "null"}
            or "enum" in n
            or "const" in n
        )
        if is_leaf:
            assert "x-medic-type" in n, f"untagged leaf {p}"


def test_no_number_is_a_float() -> None:
    assert [p for p, n in _walk(SCHEMA) if n.get("type") == "number"] == []


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_no_value_in_a_valid_report_looks_like_a_log_line(path: Path) -> None:
    def strings(v):
        if isinstance(v, dict):
            for x in v.values():
                yield from strings(x)
        elif isinstance(v, list):
            for x in v:
                yield from strings(x)
        elif isinstance(v, str):
            yield v

    for s in strings(json.loads(path.read_bytes())):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", s):
            continue  # timestamps are a fixed shape
        assert not any(re.search(shape, s) for shape in LOG_SHAPES), s


def test_canary_secret_never_validates_anywhere() -> None:
    """K1 T-31 canary: a planted secret can't ride in any string field of the report."""
    canary = "sk-ant-api03-CANARY0000"
    targets = [
        ("instance_id",),
        ("medic_version",),
        ("engine_api",),
        ("sensors_blind", 0, "signal"),
        ("rule_exposure", 0, "rule_id"),
        ("watcher_incidents", 0, "rule_id"),
        ("chain", "head", "hash"),
    ]
    for path in targets:
        doc = json.loads(base())
        node = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = canary
        assert "R-SCHEMA" in check(canonical(doc), KNOWN), path


# --- exact bytes: preview = sent ---------------------------------------------------------


def test_canonical_is_stable() -> None:
    raw = base()
    assert canonical(json.loads(raw)) == raw
    assert raw.endswith(b"}\n") and b"\r" not in raw


@pytest.mark.parametrize(
    "mangle, code",
    [
        (lambda b: b.replace(b"\n", b"\r\n"), "R-CANONICAL"),
        (lambda b: b + b" ", "R-CANONICAL"),
        (
            lambda b: json.dumps(json.loads(b), sort_keys=True).encode() + b"\n",
            "R-CANONICAL",
        ),
        (
            lambda b: (
                json.dumps(dict(reversed(json.loads(b).items())), indent=2).encode()
                + b"\n"
            ),
            "R-CANONICAL",
        ),
        (
            lambda b: b.replace(
                b'"dev_mode": false', b'"dev_mode": false,\n  "dev_mode": false', 1
            ),
            "R-JSON",
        ),
        (
            lambda b: b.replace(b'"retention_days": 90', b'"retention_days": NaN', 1),
            "R-JSON",
        ),
        (lambda b: b.replace(b'"compose"', '"composé"'.encode(), 1), "R-ENCODING"),
        (lambda b: b.replace(b'"compose"', b'"compos\\u00e9"', 1), "R-SCHEMA"),
        (lambda b: b[:-2], "R-JSON"),
        (lambda b: b + b" " * MAX_BYTES, "R-SIZE"),
    ],
    ids=[
        "crlf",
        "trailing-space",
        "compact",
        "unsorted-keys",
        "duplicate-key",
        "nan",
        "non-ascii",
        "escaped-non-ascii",
        "truncated",
        "oversize",
    ],
)
def test_any_other_spelling_is_refused(mangle, code) -> None:
    assert code in check(mangle(base()), KNOWN)


def test_sha256_is_over_the_exact_bytes() -> None:
    raw = base()
    assert sha256(raw) == sha256(canonical(json.loads(raw)))
    assert sha256(raw) != sha256(raw + b"\n")


def test_unknown_rule_passes_without_a_catalogue_but_not_with_one() -> None:
    doc = json.loads((FIX / "invalid" / "hostname-in-rule-id.json").read_text())[
        "report"
    ]
    assert check(canonical(doc)) == []
    assert check(canonical(doc), KNOWN) == ["R-RULE"]
