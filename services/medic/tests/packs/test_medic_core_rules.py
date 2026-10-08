"""Every medic-core rule fires on its firing cases and stays silent on the rest (F7).

The cases run through the same vector runner and checks as the E3 vectors
(tests/engine/test_vectors.py), so a case passes only if the engine's records open
exactly the incidents it lists, with the timings it lists. E5 wraps this as a CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from services.medic.contracts.vector_expand import expand
from services.medic.tests.engine.runner import CONTRACTS
from services.medic.tests.engine.test_vectors import test_vector as check_vector
from services.medic.tests.packs.pack_cases import (
    case_file,
    case_files,
    cases,
    load_yaml,
    pack_rules,
    vector,
)

OBSERVATION = Draft202012Validator(
    json.loads((CONTRACTS / "observation.schema.json").read_text())
)
RULES = pack_rules()
CASES = [
    pytest.param(rid, case, id=f"{rid}:{case['id']}")
    for rid in sorted(RULES)
    for case in cases(rid)
]


@pytest.mark.parametrize("rule_id", sorted(RULES))
def test_every_rule_has_a_firing_and_a_silent_case(rule_id: str) -> None:
    outcomes = [c["fires"] for c in cases(rule_id)]
    assert True in outcomes and False in outcomes, case_file(RULES[rule_id])


def test_every_case_file_belongs_to_a_pack_rule() -> None:
    files = {case_file(rule) for rule in RULES.values()}
    orphans = [p for p in case_files() if p not in files]
    assert orphans == [], "case files with no rule (renamed or misfiled?)"
    assert all(load_yaml(p)["rule"] in RULES for p in case_files())


@pytest.mark.parametrize(("rule_id", "case"), CASES)
def test_case(rule_id: str, case: dict, tmp_path: Path) -> None:
    opened = [i for i in case["incidents"].values() if i["rule"] == rule_id]
    assert bool(opened) == case["fires"], "fires must match the expected incidents"
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(vector(rule_id, case), sort_keys=False))
    check_vector(path)


@pytest.mark.parametrize(("rule_id", "case"), CASES)
def test_case_observations_are_valid_d3(rule_id: str, case: dict) -> None:
    for obs in expand(vector(rule_id, case)):
        errors = [e.message for e in OBSERVATION.iter_errors(obs)]
        assert errors == [], (obs["id"], errors)
