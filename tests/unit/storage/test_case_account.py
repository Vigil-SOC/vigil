"""The case account's pydantic mirror takes exactly the JSON the contract does.

The fixtures are shared with ``services/agent/tests/core/account.test.ts``,
which hands the same files to the TypeScript compiler as a ``CaseAccount``.
Each valid one must survive a round trip unchanged, since a field dropped or
coerced on the way through is a field the case page never sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from core.storage.schemas.case_account import CaseAccount, dump_account

FIXTURES = Path(__file__).resolve().parents[3] / "services/agent/tests/fixtures/account"
VALID = sorted(FIXTURES.glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.name)
def test_a_fixture_round_trips_unchanged(path: Path):
    held = json.loads(path.read_text())
    assert dump_account(CaseAccount.model_validate(held)) == held


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.name)
def test_an_invalid_fixture_is_refused(path: Path):
    with pytest.raises(ValidationError):
        CaseAccount.model_validate(json.loads(path.read_text()))


def _used(model: BaseModel, into: set[str]) -> None:
    for name in model.model_fields_set:
        into.add(f"{type(model).__name__}.{name}")
        value = getattr(model, name)
        for one in value if isinstance(value, list) else [value]:
            if isinstance(one, BaseModel):
                _used(one, into)


def _declared(model: type[BaseModel], into: set[str]) -> None:
    for name, field in model.model_fields.items():
        if f"{model.__name__}.{name}" in into:
            continue
        into.add(f"{model.__name__}.{name}")
        for arg in getattr(field.annotation, "__args__", (field.annotation,)):
            for inner in getattr(arg, "__args__", (arg,)):
                if isinstance(inner, type) and issubclass(inner, BaseModel):
                    _declared(inner, into)


def test_every_field_is_exercised_by_some_fixture():
    # A field no fixture uses is a field the TypeScript side was never checked against.
    used: set[str] = set()
    for path in VALID:
        _used(CaseAccount.model_validate(json.loads(path.read_text())), used)
    declared: set[str] = set()
    _declared(CaseAccount, declared)
    assert sorted(declared - used) == []
