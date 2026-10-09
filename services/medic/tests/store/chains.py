"""Helpers over the contract's decision-record fixtures (G2), shared by the store tests."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
FIX = CONTRACTS / "fixtures" / "decision-records"
VALID = sorted((FIX / "valid").glob("*.jsonl"))
INVALID = sorted((FIX / "invalid").glob("*.json"))

# The instance every fixture chain was written by (X1 ⚑5a: one store, one id).
FIXTURE_INSTANCE = "mi_3f9a2c1b0d4e5f60"

ENVELOPE = ("seq", "prev", "hash")


def load(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in (FIX / "valid" / name).read_text().splitlines()
    ]


def draft(record: dict[str, Any]) -> dict[str, Any]:
    """The record as a caller hands it to the writer: the writer owns seq, prev and hash."""
    return deepcopy({k: v for k, v in record.items() if k not in ENVELOPE})


def seed_instance(data_dir: Path, instance_id: str = FIXTURE_INSTANCE) -> None:
    """Pre-create the instance id the fixture chains were written by."""
    data_dir.mkdir(mode=0o700, exist_ok=True)
    path = data_dir / "instance_id"
    path.write_text(instance_id + "\n")
    path.chmod(0o600)


def raw(data_dir: Path) -> sqlite3.Connection:
    """A connection that bypasses the writer, the way an attacker with file access would."""
    conn = sqlite3.connect(data_dir / "medic.db", isolation_level=None)
    conn.execute("DROP TRIGGER IF EXISTS records_append_only")
    return conn


def stored(data_dir: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(data_dir / "medic.db") as conn:
        rows = conn.execute("SELECT record FROM records ORDER BY seq").fetchall()
    return [json.loads(r[0]) for r in rows]
