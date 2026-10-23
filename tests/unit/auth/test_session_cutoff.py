"""The database session cutoff, compared the same way as the Redis copy."""

from datetime import datetime, timezone

from core.auth.session_cutoff import issued_before_cutoff, stamp_session_cutoff


def test_no_cutoff_leaves_the_token_alone():
    assert issued_before_cutoff({"iat": 100}, None) is False


def test_missing_iat_is_rejected_once_a_cutoff_exists():
    assert issued_before_cutoff({}, datetime(2026, 1, 1)) is True


def test_earlier_second_is_rejected_and_the_same_second_is_not():
    cutoff = datetime(2026, 1, 1, 0, 0, 1, 500000)
    cutoff_ts = int(cutoff.replace(tzinfo=timezone.utc).timestamp())
    assert issued_before_cutoff({"iat": cutoff_ts - 1}, cutoff) is True
    assert issued_before_cutoff({"iat": cutoff_ts}, cutoff) is False


def test_aware_cutoff_is_read_as_utc():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert issued_before_cutoff({"iat": 0}, cutoff) is True


def test_stamp_records_now():
    user = type("U", (), {})()
    stamp_session_cutoff(user)
    assert user.tokens_valid_after is not None
    assert user.tokens_valid_after.tzinfo is None
