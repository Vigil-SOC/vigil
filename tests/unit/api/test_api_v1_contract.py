"""The /api/v1 surface is a frozen contract: it may not drift silently.

`core/api/v1/contract.snapshot.json` is the committed promise — the paths and
shapes of `/api/v1/**`, minus `x-vigil-beta` operations. This test rebuilds that
subset from the live app and fails if it differs from the committed file.

A red build here means a frozen route or response shape changed. If the change
is intended, regenerate the snapshot and commit it — a deliberate, reviewable
act:

    python scripts/generate_api_v1_contract.py

That is the whole point: a change to the contract cannot land as a side effect
of an unrelated PR (see #854, where a cleanup silently rewrote contract files).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")
os.environ.setdefault("DEV_MODE", "true")

pytestmark = pytest.mark.unit

from scripts.generate_api_v1_contract import SNAPSHOT, build_contract, serialize  # noqa: E402


def _live_contract() -> dict:
    from services.api.main import app

    return build_contract(app)


def test_v1_contract_matches_committed_snapshot():
    committed = json.loads(SNAPSHOT.read_text())
    live = _live_contract()
    assert live == committed, (
        "The /api/v1 frozen contract drifted from "
        f"{SNAPSHOT.relative_to(REPO)}.\n"
        "If this change is intended, run:\n"
        "    python scripts/generate_api_v1_contract.py\n"
        "and commit the diff. If it is not, a frozen route or shape changed by "
        "accident — revert it."
    )


def test_snapshot_is_serialized_canonically():
    # The committed file must be exactly what the generator writes, so a diff is
    # only ever a real contract change, never formatting noise.
    committed_text = SNAPSHOT.read_text()
    assert committed_text == serialize(json.loads(committed_text))


def test_no_beta_operation_is_in_the_contract():
    live = _live_contract()
    leaked = [
        f"{method.upper()} {path}"
        for path, methods in live["paths"].items()
        for method, op in methods.items()
        if isinstance(op, dict) and op.get("x-vigil-beta") is True
    ]
    assert not leaked, f"x-vigil-beta operations must be excluded: {leaked}"


def test_contract_covers_only_v1_paths():
    live = _live_contract()
    stray = [p for p in live["paths"] if not p.startswith("/api/v1")]
    assert not stray, f"non-v1 paths in the contract: {stray}"
