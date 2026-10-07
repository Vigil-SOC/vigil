"""A row the database refuses at flush time must not take the batch with it (#1581)."""

from __future__ import annotations

import pytest

from core.threat_intel.threat_feed_service import (
    NormalizedIndicator,
    upsert_indicators,
)

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _indicator(value: str) -> NormalizedIndicator:
    return NormalizedIndicator(
        indicator_type="ip",
        indicator_value=value,
        source="cloudforce_one",
        collection_id="col-1",
        confidence=80,
        threat_level="high",
        labels=[],
        valid_from=None,
        valid_until=None,
        raw_stix={},
    )


def test_one_refused_row_is_skipped_and_the_rest_commit():
    from core.storage.connection import get_db_manager
    from core.storage.models import ThreatIndicator

    batch = [
        _indicator("198.51.100.1"),
        _indicator("x" * 5000),  # over the 2048-char column: refused at flush
        _indicator("198.51.100.2"),
    ]

    counts = upsert_indicators(batch)

    assert counts == {"inserted": 2, "updated": 0, "skipped": 1}
    with get_db_manager().session_scope() as session:
        stored = {
            row.indicator_value
            for row in session.query(ThreatIndicator).filter_by(source="cloudforce_one")
        }
    assert {"198.51.100.1", "198.51.100.2"} <= stored
