"""The case account's vocabularies are declared twice and must say the same thing.

The shared fixtures pin every field to both sides, but only the values some
fixture uses: a disposition or a standing added on one side alone fails nothing
there. Each closed list in ``contracts/account.ts`` is compared here with the
pydantic ``Literal`` that mirrors it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from core.storage.schemas import case_account as py

AGENT = Path(__file__).resolve().parents[3] / "services" / "agent"
ACCOUNT = AGENT / "contracts" / "account.ts"
EVENTS = AGENT / "contracts" / "events.ts"

# Each `as const` list in account.ts and what mirrors it on this side.
MIRRORED = {
    "DISPOSITIONS": get_args(py.Disposition),
    "CONFIDENCES": get_args(py.Confidence),
    "EXPLANATION_ROLES": get_args(py.ExplanationRole),
    "STANDINGS": get_args(py.Standing),
    "STANCES": get_args(py.Stance),
    "CHECK_STATUSES": get_args(py.CheckStatus),
    "STRENGTHS": get_args(py.Strength),
    "SALIENCES": get_args(py.Salience),
    "CHAIN_STATUSES": get_args(py.ChainStatus),
    "ACCOUNT_ROLES": get_args(py.AccountRole),
    "GAP_KINDS": get_args(py.GapKind),
    "ENTITY_TYPES": py.ENTITY_TYPES,
}

pytestmark = pytest.mark.unit


def ts_list(path: Path, name: str) -> tuple[str, ...]:
    match = re.search(rf"{name}\s*=\s*\[([^\]]*)\]\s*as const", path.read_text())
    assert match, f"{name} not found in {path}"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


@pytest.mark.parametrize("name", sorted(MIRRORED))
def test_each_vocabulary_agrees(name: str):
    assert ts_list(ACCOUNT, name) == tuple(MIRRORED[name])


def test_every_vocabulary_is_compared():
    # The one way this ratchet could stop failing: a list added to the contract
    # and to nothing here.
    declared = set(re.findall(r"export const (\w+) = \[[^\]]*\] as const", ACCOUNT.read_text()))
    assert declared == set(MIRRORED)


def test_the_run_kinds_are_the_agent_layers():
    assert ts_list(EVENTS, "RUN_KINDS") == get_args(py.RunKind)
