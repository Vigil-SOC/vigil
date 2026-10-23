"""A person who asks for an investigation stays the caller of its tools."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.auth import tool_principal
from services.api.routers.orchestrator import (
    InvestigationCreateRequest,
    create_investigation,
)
from services.daemon.orchestrator import _caller

pytestmark = pytest.mark.unit


def test_an_ask_signs_the_person_onto_the_intake_row(monkeypatch):
    captured = {}

    def _insert(**kwargs):
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(
        "services.daemon.orchestrator.insert_intake_trigger", _insert
    )
    create_investigation(
        InvestigationCreateRequest(hypothesis="hosts phoned home"),
        user=SimpleNamespace(username="nestor"),
    )

    assert captured["kind"] == "human_ask"
    assert captured["payload"]["started_by"] == "nestor"
    assert tool_principal.verify(captured["payload"]["principal"]) == "nestor"
    assert captured["payload"]["hypothesis"] == "hosts phoned home"


def test_caller_reads_the_person_and_ignores_a_row_with_none(monkeypatch):
    rows = {
        "inv-person": SimpleNamespace(
            payload={"started_by": "nestor", "principal": "signed"}
        ),
        "inv-system": SimpleNamespace(payload={"trigger_type": "scheduled"}),
    }

    class _Query:
        def __init__(self, row):
            self._row = row

        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def first(self):
            return self._row

    class _Session:
        def query(self, _model):
            return _Query(rows[self.wanted])

    session = _Session()

    @contextmanager
    def _scope():
        yield session

    class _Manager:
        def session_scope(self):
            return _scope()

    monkeypatch.setattr(
        "core.storage.connection.get_db_manager", lambda: _Manager()
    )

    session.wanted = "inv-person"
    assert _caller("inv-person") == ("nestor", "signed")
    session.wanted = "inv-system"
    assert _caller("inv-system") == (None, None)


def test_caller_is_nobody_when_the_row_cannot_be_read(monkeypatch):
    def _down():
        raise RuntimeError("database is down")

    monkeypatch.setattr("core.storage.connection.get_db_manager", _down)
    assert _caller("inv-1") == (None, None)
