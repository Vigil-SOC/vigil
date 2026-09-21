"""Expired threat_indicators must not match (#1008).

``valid_until`` is written and indexed; lookup and the hunt-proposal read
were ignoring it. A stale hit used to be enrichment context; it is now an
intake ticket, so both reads keep only live rows (NULL expiry counts as live).
"""

from __future__ import annotations

import pytest

from core.threat_intel import threat_feed_service as feed
from core.time import utcnow

pytestmark = pytest.mark.unit


class _Query:
    def __init__(self):
        self.clauses = []

    def filter(self, *args):
        self.clauses.extend(args)
        return self

    def order_by(self, *args):
        return self

    def limit(self, n):
        return self

    def all(self):
        return []


class _Session:
    def __init__(self, query):
        self._query = query

    def query(self, model):
        return self._query

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_session(monkeypatch, query):
    db = type("DB", (), {"session_scope": lambda self: _Session(query)})()
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)
    return query


def _mentions_valid_until(clauses) -> bool:
    return any("valid_until" in str(clause) for clause in clauses)


def test_unexpired_clause_keeps_null_and_future():
    now = utcnow()
    sql = str(
        feed._unexpired_clause(now).compile(compile_kwargs={"literal_binds": True})
    ).lower()

    assert "valid_until" in sql
    assert "is null" in sql
    assert ">" in sql


def test_lookup_omits_expired_indicators(monkeypatch):
    query = _install_session(monkeypatch, _Query())

    assert feed.lookup_indicators("ip", ["203.0.113.7"]) == {}
    assert _mentions_valid_until(query.clauses)


def test_recent_indicators_omit_expired_rows(monkeypatch):
    query = _install_session(monkeypatch, _Query())

    assert feed._recent_indicators(10) == []
    assert _mentions_valid_until(query.clauses)
