"""The committed column snapshot matches a database built the way Compose builds one.

``core/storage/schema.snapshot.json`` is that listing. This rebuilds it and
fails when a model or an init SQL file changes a table or column without the
snapshot being regenerated. An index-only change does not appear in the
listing, so it stays green.

The unit job has no Postgres, and its loopback guard would skip this. It
lives here because the integration job is the one that provisions Postgres.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

pytestmark = [pytest.mark.integration, pytest.mark.database]

from scripts.generate_api_v1_contract import serialize  # noqa: E402
from scripts.generate_schema_snapshot import (  # noqa: E402
    SNAPSHOT,
    ScratchDatabaseUnavailable,
    build_listing,
    postgres_skip_reason,
)


def _listing() -> dict:
    reason = postgres_skip_reason()
    if reason:
        pytest.skip(reason)
    try:
        return build_listing()
    except ScratchDatabaseUnavailable as e:
        pytest.skip(str(e))


def test_fresh_database_matches_committed_column_snapshot():
    first = _listing()
    second = _listing()
    assert serialize(first) == serialize(
        second
    ), "Two provisions of a clean database produced different column listings"
    # SQL-only: no ORM model, so schema_report() never sees it. An index on
    # the same table must not show up as its own entry.
    assert "snapshot" in first["agent_events"]
    assert "idx_agent_events_terminal" not in first

    if not SNAPSHOT.is_file():
        raise AssertionError(
            "Missing core/storage/schema.snapshot.json. Run:\n"
            "    python scripts/generate_schema_snapshot.py"
        )
    committed = SNAPSHOT.read_text(encoding="utf-8")
    assert committed == serialize(json.loads(committed)), (
        "core/storage/schema.snapshot.json is not canonical. Run:\n"
        "    python scripts/generate_schema_snapshot.py"
    )
    assert serialize(first) == committed, (
        "A table or column drifted from core/storage/schema.snapshot.json.\n"
        "If this change is intended, run:\n"
        "    python scripts/generate_schema_snapshot.py\n"
        "and commit the diff."
    )
