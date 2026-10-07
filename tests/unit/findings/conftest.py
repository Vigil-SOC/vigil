"""Fixtures for the analyst IP exclusion tests (core.findings.exclusions)."""

import time

import pytest

from core.findings import exclusions


class ActiveExclusions:
    """The active set, seeded into the real cache so no test reaches for a
    database. Seeded rather than monkeypatched: callers bind
    ``cached_active_ips`` at import, so patching the module attribute misses
    whichever of them was imported first."""

    def __init__(self, monkeypatch, ips):
        self._monkeypatch = monkeypatch
        self.set(ips)

    def set(self, ips):
        self._monkeypatch.setattr(exclusions, "_cache", frozenset(ips))
        self._monkeypatch.setattr(exclusions, "_cache_at", time.monotonic())


@pytest.fixture
def active_exclusions(monkeypatch):
    """One excluded address, 203.0.113.9 (TEST-NET-3), until a test says otherwise."""
    yield ActiveExclusions(monkeypatch, {"203.0.113.9"})
    exclusions.invalidate_cache()
