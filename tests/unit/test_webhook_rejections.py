"""Rejected-webhook logging: one WARNING per (endpoint, reason) per window, exact counts."""

import logging

import pytest

from core import webhook_rejections as wr


@pytest.fixture(autouse=True)
def clean_state():
    wr._counts.clear()
    wr._log_state.clear()
    yield
    wr._counts.clear()
    wr._log_state.clear()


def test_burst_logs_once_per_window_and_counts_every_rejection(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(wr, "_clock", lambda: now[0])
    caplog.set_level(logging.WARNING, logger=wr.logger.name)

    for _ in range(100):
        wr.record_rejection("ep/a", wr.BAD_SIGNATURE, "203.0.113.7")
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "endpoint=ep/a" in msg
    assert "reason=bad_signature" in msg
    assert "source_ip=203.0.113.7" in msg
    assert wr.rejection_counts()["ep/a"] == {"bad_signature": 100}

    # A different reason has its own window.
    wr.record_rejection("ep/a", wr.NO_SECRET, "203.0.113.7")
    assert len(caplog.records) == 2

    # Next window: logs again and reports what was suppressed (99 in the burst + 1).
    now[0] += wr.LOG_WINDOW_SECONDS + 1
    wr.record_rejection("ep/a", wr.BAD_SIGNATURE, "203.0.113.7")
    assert len(caplog.records) == 3
    assert "+99 similar suppressed" in caplog.records[2].getMessage()
    assert wr.rejection_counts("ep/")["ep/a"]["bad_signature"] == 101


def test_rejection_counts_filters_by_prefix():
    wr.record_rejection("darktrace/x", wr.BAD_SIGNATURE, None)
    wr.record_rejection("daemon/ingest", wr.BAD_TOKEN, None)
    assert wr.rejection_counts("daemon/") == {"daemon/ingest": {"bad_token": 1}}
