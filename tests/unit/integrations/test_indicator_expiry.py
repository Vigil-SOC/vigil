"""Expired threat_indicators must not match (#1008).

``valid_until`` is written and indexed; lookup and the hunt-proposal read
were ignoring it. A stale hit used to be enrichment context; it is now an
intake ticket, so both reads keep only live rows (NULL expiry counts as live).

The reads run against the throwaway Postgres: the comparison is the thing under
test, and a faked session would pass it pointing either way.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.threat_intel import threat_feed_service as feed
from core.time import utcnow

pytestmark = pytest.mark.unit

SOURCE = "test-expiry"
EXPIRED = "203.0.113.1"
LIVE = "203.0.113.2"
NO_EXPIRY = "203.0.113.3"


def _indicator(value, valid_until):
    return feed.NormalizedIndicator(
        indicator_type="ip",
        indicator_value=value,
        source=SOURCE,
        collection_id=None,
        confidence=None,
        threat_level=None,
        labels=[],
        valid_from=None,
        valid_until=valid_until,
        raw_stix={},
    )


@pytest.fixture
def indicators():
    from core.storage.connection import get_db_manager
    from core.storage.models import ThreatIndicator

    def _clear():
        with get_db_manager().session_scope() as session:
            session.query(ThreatIndicator).filter_by(source=SOURCE).delete()

    _clear()
    now = utcnow()
    feed.upsert_indicators(
        [
            _indicator(EXPIRED, now - timedelta(days=1)),
            _indicator(LIVE, now + timedelta(days=1)),
            _indicator(NO_EXPIRY, None),
        ]
    )
    yield
    _clear()


@pytest.mark.usefixtures("indicators")
@pytest.mark.external_service
@pytest.mark.database
def test_lookup_keeps_live_and_unexpiring_rows_only():
    hits = feed.lookup_indicators("ip", [EXPIRED, LIVE, NO_EXPIRY])

    assert set(hits) == {LIVE, NO_EXPIRY}


@pytest.mark.usefixtures("indicators")
@pytest.mark.external_service
@pytest.mark.database
def test_recent_indicators_omit_expired_rows():
    values = {
        row["indicator_value"]
        for row in feed._recent_indicators(feed.RECENT_INDICATOR_LIMIT)
        if row.get("source") == SOURCE
    }

    assert values == {LIVE, NO_EXPIRY}


@pytest.mark.usefixtures("indicators")
@pytest.mark.external_service
@pytest.mark.database
def test_a_republished_indicator_with_no_expiry_matches_again():
    feed.upsert_indicators([_indicator(EXPIRED, None)])

    assert EXPIRED in feed.lookup_indicators("ip", [EXPIRED])


def test_an_offset_expiry_is_converted_to_utc_not_truncated():
    assert feed._parse_dt("2026-09-22T10:00:00+05:00") == datetime(2026, 9, 22, 5, 0)
    assert feed._parse_dt("2026-09-22T10:00:00Z") == datetime(2026, 9, 22, 10, 0)
