"""The bundled medic-core pack (F7): each rule's firing and non-firing cases run
through the engine, and the whole pack passes rule_check, pack_build and pack_check
(unsigned, dev channel: F5/F2c sign).

Case files are `medic_core/<class>/<rule file>.yaml`, one per rule:

    rule: <rule id>
    defaults: {...}        # vector fields shared by every case (sensors, types…)
    cases:
      - kind: fires | silent
        id: v01-…          # the rest is one E3 vector (vector.schema.json) without
        ...                # `rules`, `start` or `covers`: the harness adds them

Each case becomes a vector with the pack rule inline and goes through the same
runner and checks as the E3 vectors (tests/engine/test_vectors.py). The E5 rule
test CLI wraps these later; E6 adds corpus replay.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from services.medic.contracts import rule_check
from services.medic.contracts.pack_build import build
from services.medic.contracts.pack_check import check_pack
from services.medic.engine import Engine, load_rule
from services.medic.tests.engine.runner import CONTRACTS, INSTANCE
from services.medic.tests.engine.test_vectors import test_vector as check_vector

PACK = Path(__file__).resolve().parents[2] / "packs" / "medic-core"
CASES = Path(__file__).resolve().parent / "medic_core"
VECTOR = Draft202012Validator(
    json.loads((CONTRACTS / "vector.schema.json").read_text())
)
START = "2026-10-08T00:00:00Z"
# A fixed import time inside the pack's validity window; F5 rebuilds at release.
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
VIGIL = "0.6.0"


def _yaml(path: Path):
    return yaml.load(path.read_text(), Loader=rule_check._StrictLoader)


def pack_rules() -> dict[str, dict]:
    return {
        doc["id"]: doc for doc in (_yaml(p) for p in sorted(PACK.glob("rules/*.yaml")))
    }


def case_files() -> list[Path]:
    return sorted(CASES.glob("*/*.yaml"))


def pack_suppression() -> list[dict]:
    """suppression.yaml as the engine takes it (E3 §6): entries without `why`."""
    entries = _yaml(PACK / "suppression.yaml")["entries"]
    return [{k: v for k, v in e.items() if k != "why"} for e in entries]


def vector_of(doc: dict, case: dict, rule: dict) -> dict:
    """One case as an E3 vector: the pack rule inline, under the pack's own
    suppression, so a suppression child keeps its 5-minute routing hold."""
    body = {k: v for k, v in case.items() if k not in ("kind", "params")}
    entry = {"inline": rule}
    if "params" in case:
        entry["params"] = case["params"]
    return {
        "covers": ["§2 rule content (F7)"],
        "start": START,
        "suppression": pack_suppression(),
        **doc.get("defaults", {}),
        **body,
        "rules": [entry],
    }


def _cases() -> list:
    out = []
    for path in case_files():
        doc = _yaml(path)
        for case in doc["cases"]:
            out.append(pytest.param(path, case["id"], id=f"{path.stem}:{case['id']}"))
    return out


@pytest.mark.parametrize(("path", "case_id"), _cases())
def test_case(path: Path, case_id: str, tmp_path: Path) -> None:
    doc = _yaml(path)
    case = next(c for c in doc["cases"] if c["id"] == case_id)
    rule = pack_rules()[doc["rule"]]
    vector = vector_of(doc, case, rule)
    errors = [e.message for e in VECTOR.iter_errors(vector)]
    assert errors == []
    if case["kind"] == "fires":
        assert vector["incidents"], "a firing case must expect an incident"
        assert {i["rule"] for i in vector["incidents"].values()} == {rule["id"]}
    else:
        assert case["kind"] == "silent"
        assert vector["incidents"] == {}, "a silent case expects no incident"
    file = tmp_path / f"{case_id}.yaml"
    file.write_text(yaml.safe_dump(vector, sort_keys=False))
    check_vector(file)


def test_every_rule_has_a_firing_and_a_silent_case_in_its_class_folder() -> None:
    assert pack_rules(), "the pack has no rules"
    kinds: dict[str, set[str]] = {}
    for path in case_files():
        doc = _yaml(path)
        rule = pack_rules().get(doc["rule"])
        assert rule is not None, f"{path}: no pack rule {doc['rule']}"
        assert path.parent.name == rule["fault"]["class"], path
        assert path.name == f"{rule['id'].replace('.', '-')}.yaml", path
        kinds.setdefault(doc["rule"], set()).update(c["kind"] for c in doc["cases"])
    for rid in pack_rules():
        assert kinds.get(rid) == {"fires", "silent"}, rid


def test_trusted_test_lines_are_lines_the_catalog_trusts() -> None:
    """A case may only feed a catalog-trusted line the pack's catalog would trust."""
    catalog = {
        (e["logger"], e["template"]) for e in _yaml(PACK / "catalog.yaml")["entries"]
    }
    for path in case_files():
        doc = _yaml(path)
        for case in doc["cases"]:
            for line in vector_of(doc, case, {}).get("logs", []):
                if line.get("trusted", True):
                    assert (line["logger"], line["template"]) in catalog, (
                        path.name,
                        case["id"],
                        line["template"],
                    )


@pytest.mark.parametrize(
    "path", sorted(PACK.glob("rules/*.yaml")), ids=lambda p: p.stem
)
def test_rule_passes_the_loader(path: Path) -> None:
    res = rule_check.check_text(path.read_text())
    assert res.ok, res.errors
    assert res.skipped is None
    rule = _yaml(path)
    # Declared, not left to the E-DETECTION lint (F7 brief).
    assert rule.get("detection") in ("event", "absence")
    assert path.name == f"{rule['id'].replace('.', '-')}.yaml"


def test_the_pack_builds_reproducibly_and_passes_pack_check() -> None:
    first, second = build(PACK), build(PACK)
    assert first == second
    res = check_pack(first, vigil_version=VIGIL, now=NOW)
    assert res.ok, res.errors
    assert res.skipped_rules == []
    assert set(res.rules) == set(pack_rules())
    manifest = res.manifest
    assert manifest["pack"]["id"] == "medic-core"
    assert manifest["pack"]["channel"] == "dev"
    assert manifest["counts"]["rules"] == len(pack_rules())


def test_the_engine_accepts_the_pack_rules_and_suppression_together() -> None:
    engine = Engine(
        [load_rule(r) for r in pack_rules().values()],
        instance_id=INSTANCE,
        install_shape="compose",
        suppression=pack_suppression(),
    )
    assert engine.tick(NOW) == []
