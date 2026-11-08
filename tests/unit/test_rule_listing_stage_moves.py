"""Listing rows carry each Vigil rule's stage-move count, its last move's actor
and from-stage, and who holds it, as GET /rules/{id}/stages reads them; SIEM
rows carry none of these (#4050)."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog

FIELDS = {"stage_moves", "stage_from", "stage_actor", "held", "held_by"}


def _rows(rule_id, *newest_first):
    """(rule_id, stage, actor, proof) rows, newest first, as the query orders them."""
    return [(rule_id, stage, actor, proof) for stage, actor, proof in newest_first]


@pytest.mark.parametrize(
    "rows, want",
    [
        # One row: the copy entered shadow; nothing to move from.
        (
            [("shadow", "system", {})],
            {"stage_moves": 1, "stage_from": None, "stage_actor": "system",
             "held": False, "held_by": None},
        ),
        # Vigil moved it back to shadow itself: the row turns amber.
        (
            [("shadow", "system", {}), ("live", "system", {}), ("shadow", "system", {})],
            {"stage_moves": 3, "stage_from": "live", "stage_actor": "system",
             "held": False, "held_by": None},
        ),
        # A person reverted it: held by them.
        (
            [("shadow", "d.lee", {"hold": True}), ("live", "system", {})],
            {"stage_moves": 2, "stage_from": "live", "stage_actor": "d.lee",
             "held": True, "held_by": "d.lee"},
        ),
        # A new version's row after the Revert does not lift the hold.
        (
            [("shadow", "system", {}), ("shadow", "d.lee", {"hold": True}),
             ("live", "system", {})],
            {"stage_moves": 3, "stage_from": "shadow", "stage_actor": "system",
             "held": True, "held_by": "d.lee"},
        ),
        # Released: no longer held.
        (
            [("shadow", "a.kim", {"hold": False}), ("shadow", "d.lee", {"hold": True}),
             ("live", "system", {})],
            {"stage_moves": 3, "stage_from": "shadow", "stage_actor": "a.kim",
             "held": False, "held_by": None},
        ),
    ],
)
def test_stage_moves_read_like_the_stages_endpoint(rows, want):
    assert catalog.stage_moves(_rows(7, *rows)) == {7: want}


def test_each_rule_is_summarised_on_its_own():
    out = catalog.stage_moves(
        _rows(1, ("live", "system", {}), ("shadow", "system", {}))
        + _rows(2, ("shadow", "d.lee", {"hold": True}), ("live", "system", {}))
    )
    assert (out[1]["stage_moves"], out[1]["held"]) == (2, False)
    assert (out[2]["held_by"], out[2]["stage_actor"]) == ("d.lee", "d.lee")


def _copy(rule_id, source):
    return SimpleNamespace(
        id=rule_id, name=f"R{rule_id}", language="kql", source=source,
        status="active", techniques=[], checks=None,
    )


def test_vigil_rows_carry_the_fields_and_siem_rows_none():
    copies = [_copy(1, "overlay"), _copy(2, "elastic")]
    at = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    built = catalog._health_rows(
        {
            "rows": copies,
            "rule_of": {c.id: c.id for c in copies},
            "stages": {1: ("shadow", at)},
            "moves": catalog.stage_moves(
                _rows(1, ("shadow", "d.lee", {"hold": True}), ("live", "system", {}))
            ),
        },
        {},
        today=date(2026, 11, 8),
    )
    rows = {
        r["rule_id"]: r
        for state in built["by_state"].values()
        for _tool, _hay, r in state
    }
    assert {k: rows[1][k] for k in FIELDS} == {
        "stage_moves": 2, "stage_from": "live", "stage_actor": "d.lee",
        "held": True, "held_by": "d.lee",
    }
    assert not FIELDS & set(rows[2])
