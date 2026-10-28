"""Shared fixtures for the daemon unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _no_open_case_lookup(request, monkeypatch):
    """Unit tests have no Postgres: the finding is on, and matches, no Case unless a test says so.

    Tests marked ``external_service`` run the real lookup against the throwaway DB.
    """
    if request.node.get_closest_marker("external_service"):
        return
    monkeypatch.setattr(
        "services.daemon.orchestrator._open_case_holding", lambda _fid: None
    )
    monkeypatch.setattr(
        "services.daemon.orchestrator._matching_cases", lambda _f, *, open: []
    )
