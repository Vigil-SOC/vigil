"""D7 contract: Medic trace format v1 (manifest, labels, observation lines) and its validator."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from trace_check import LABELS, MANIFEST, validate

TRACES = ROOT / "fixtures" / "traces"
VALID = sorted(p for p in (TRACES / "valid").iterdir() if p.is_dir())
EXPECTED = json.loads((TRACES / "invalid" / "expected.json").read_text())


@pytest.mark.parametrize("name", ["trace-manifest", "trace-labels"])
def test_schema_is_valid_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(
        json.loads((ROOT / f"{name}.schema.json").read_text())
    )


@pytest.mark.parametrize("trace", VALID, ids=lambda p: p.name)
def test_valid_trace_passes(trace: Path) -> None:
    assert validate(trace) == []
    assert MANIFEST.is_valid(json.loads((trace / "manifest.json").read_text()))
    if (trace / "labels.json").exists():
        assert LABELS.is_valid(json.loads((trace / "labels.json").read_text()))


def test_valid_fixtures_cover_fault_control_unlabelled_and_interrupted() -> None:
    labels = {
        p.name: json.loads((p / "labels.json").read_text())
        for p in VALID
        if (p / "labels.json").exists()
    }
    assert any(lb["fault"]["mode"] for lb in labels.values())
    assert any(lb["fault"]["mode"] is None for lb in labels.values())
    assert len(labels) < len(VALID)
    assert any(
        json.loads((p / "manifest.json").read_text())["capture"]["interrupted"]
        for p in VALID
    )


@pytest.mark.parametrize("name", sorted(EXPECTED), ids=str)
def test_invalid_trace_fails_with_its_code(name: str) -> None:
    (trace,) = (p for p in (TRACES / "invalid" / name).iterdir())
    assert {v.code for v in validate(trace)} == set(EXPECTED[name]["codes"]), EXPECTED[
        name
    ]["why"]


def test_every_check_has_an_invalid_fixture() -> None:
    documented = {
        line.split()[0]
        for line in (ROOT / "trace_check.py").read_text().split('"""')[1].splitlines()
        if line.strip().startswith("T-")
    }
    assert documented == {c for case in EXPECTED.values() for c in case["codes"]}


def test_missing_trace_dir_is_one_violation(tmp_path: Path) -> None:
    assert [v.code for v in validate(tmp_path / "nope")] == ["T-MANIFEST"]


def test_every_violation_is_reported_not_just_the_first(tmp_path: Path) -> None:
    trace = shutil.copytree(
        TRACES / "valid" / "capture-plain", tmp_path / "capture-plain"
    )
    (trace / "NOTES.txt").write_text("stray")
    triage = trace / "observations" / "http.triage.jsonl"
    triage.write_bytes(triage.read_bytes() + b"[]\n")
    codes = {v.code for v in validate(trace)}
    assert {"T-FILE-UNLISTED", "T-FILE-HASH", "T-FILE-LINES", "T-LINE-SCHEMA"} <= codes


def test_damage_is_reported_not_raised(tmp_path: Path) -> None:
    trace = shutil.copytree(
        TRACES / "valid" / "P-4.a-triage-503", tmp_path / "P-4.a-triage-503"
    )
    manifest = json.loads((trace / "manifest.json").read_text())
    manifest["capture"]["ended_at"] = (
        "2026-02-30T00:00:00Z"  # matches the pattern, isn't a date
    )
    (trace / "manifest.json").write_text(json.dumps(manifest))
    assert {v.code for v in validate(trace)} == {"T-MANIFEST-SCHEMA"}

    trace = shutil.copytree(
        TRACES / "valid" / "P-4.a-triage-503", tmp_path / "b" / "P-4.a-triage-503"
    )
    (trace / "labels.json").unlink()
    (trace / "labels.json").symlink_to(
        tmp_path / "gone.json"
    )  # a broken link isn't "no labels"
    (trace / "observations" / "elsewhere").symlink_to(
        tmp_path
    )  # a link out of the trace
    (trace / "observations" / "http.triage.jsonl").chmod(0)
    try:
        codes = {v.code for v in validate(trace)}
    finally:
        (trace / "observations" / "http.triage.jsonl").chmod(0o644)
    assert codes == {"T-LABELS", "T-FILE-UNLISTED", "T-FILE-MISSING"}
