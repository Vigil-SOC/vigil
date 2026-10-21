"""G2 reference: canonical hashing and hash-chain verification for decision records.

The store (G3) must produce byte-identical hashes. Canonical form: JSON with keys
sorted, no whitespace, UTF-8, and integers only (the schema forbids floats), which
for these records is the same as RFC 8785 (JCS) output (inferred: keys are ASCII).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

GENESIS = "0" * 64
TICK_S = 15  # E3 §1; engine-made records are stamped with their tick (A3-5)


def incident_id(
    instance_id: str,
    rule_id: str | None,
    group: Sequence[dict[str, str]],
    active_since: str,
) -> str:
    """Deterministic incident id (A3-5): replaying a recording gives the same ids.

    'inc_' + 24 hex of sha256 over (version tag, instance, rule, group sorted by name,
    active_since). Minted once when the incident opens and stored: a flapping reopen
    (E3 §5) keeps the stored id, even though its new active_since would hash differently.
    rule_id None is an unknown signature, grouped by fingerprint (decision-record.md §3).
    """
    parts = [
        "inc1",
        instance_id,
        rule_id or "",
        canonical(sorted(group, key=lambda g: g["name"])).decode(),
        active_since,
    ]
    return "inc_" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]


class ChainError(Exception):
    """A chain check failed. `code` is stable; `seq` is the offending record."""

    def __init__(self, code: str, seq: int, detail: str) -> None:
        super().__init__(f"{code} at seq {seq}: {detail}")
        self.code = code
        self.seq = seq


def canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def record_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def seal(record: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    """Return `record` with seq, prev and hash filled in after `prev` (None = genesis)."""
    out = dict(record)
    if prev is None:
        out.setdefault("seq", 0)
        out["prev"] = GENESIS
    else:
        out["seq"] = prev["seq"] + 1
        out["prev"] = prev["hash"]
    out["hash"] = record_hash(out)
    return out


def _instant(ts: str) -> datetime:
    """A schema `ts` as an instant: "…:00.5Z" is later than "…:00Z" though it sorts first."""
    return datetime.fromisoformat(ts)


def gap_ok(body: dict[str, Any]) -> bool:
    """A `gap` must not end before it starts (E-GAP); writers check before appending."""
    return _instant(body["from"]) <= _instant(body["to"])


def _start_ok(first: dict[str, Any], anchors: dict[int, str]) -> bool:
    """The oldest remaining record must be the genesis, a store reset, or covered by an anchor."""
    if first["type"] == "store_reset":
        head = first["body"]["previous_head"]
        return first["prev"] == (head["hash"] if head else GENESIS)
    if first["prev"] == GENESIS:
        return first["seq"] == 0
    return anchors.get(first["seq"] - 1) == first["prev"]


def verify(records: Sequence[dict[str, Any]]) -> str:
    """Verify a store's records in seq order. Returns the chain head hash.

    Error codes: E-HASH (a record was edited), E-SEQ (gap, duplicate or reorder),
    E-LINK (prev doesn't match the record before it), E-UNANCHORED (the oldest
    record is neither genesis, a store reset, nor covered by an anchor), E-GAP (a
    `gap` record ends before it starts; the schema can't compare two fields).
    """
    if not records:
        return GENESIS
    anchors = {
        r["body"]["deleted_last_seq"]: r["body"]["last_deleted_hash"]
        for r in records
        if r["type"] == "anchor"
    }
    for i, rec in enumerate(records):
        if record_hash(rec) != rec["hash"]:
            raise ChainError("E-HASH", rec["seq"], "content does not match its hash")
        if rec["type"] == "gap" and not gap_ok(rec["body"]):
            raise ChainError("E-GAP", rec["seq"], "the gap ends before it starts")
        if i == 0:
            if not _start_ok(rec, anchors):
                raise ChainError(
                    "E-UNANCHORED", rec["seq"], "oldest record has no anchor"
                )
            continue
        before = records[i - 1]
        if rec["type"] == "store_reset":
            if not _start_ok(rec, anchors):
                raise ChainError(
                    "E-LINK", rec["seq"], "reset does not name the old head"
                )
            continue
        if rec["seq"] != before["seq"] + 1:
            raise ChainError("E-SEQ", rec["seq"], f"expected seq {before['seq'] + 1}")
        if rec["prev"] != before["hash"]:
            raise ChainError(
                "E-LINK", rec["seq"], "prev is not the previous record's hash"
            )
    return records[-1]["hash"]
