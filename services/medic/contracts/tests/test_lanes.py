"""G1 contract: lane and routing vectors (contracts/decision-record.md section 2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from lane_ref import run_vector

VECTORS = sorted((ROOT / "vectors" / "lanes").glob("lanes-*.yaml"))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_vector_count_is_within_the_g1_brief() -> None:
    assert 6 <= len(VECTORS) <= 10


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_lane_vector(path: Path) -> None:
    vector = _load(path)
    assert vector["id"] == path.stem
    assert run_vector(vector) == vector["expect"]


def test_vectors_cover_every_lane_reason_and_route_outcome() -> None:
    reasons, outcomes = set(), set()
    for path in VECTORS:
        for got in _load(path)["expect"].values():
            reasons.add(got["lane"]["reason"])
            routed, by = got["routed_at"] is not None, got["suppressed_by"] is not None
            outcomes.add(
                {
                    (True, False): "routed",
                    (False, True): "suppressed",
                    (True, True): "unsuppressed",
                }[(routed, by)]
            )
    assert reasons == {"rule", "lane1_gate_failed", "unknown_signature"}
    assert outcomes == {"routed", "suppressed", "unsuppressed"}


def test_the_lanes_vectors_are_found() -> None:
    # A moved folder must fail loudly, not pass on zero vectors (X1 #7).
    assert len(VECTORS) == 10
