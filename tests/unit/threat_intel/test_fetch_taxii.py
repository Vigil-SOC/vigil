"""A failed TAXII fetch must raise, so the poller keeps its watermark (#1278).

An empty envelope is a real answer and still returns []. A missing
taxii2-client wheel stays a logged no-op: that is a deploy state, and a
restart after installing it clears the in-memory watermark on purpose.
"""

from datetime import datetime

import pytest

from core.threat_intel.threat_feed_service import fetch_taxii_collection

pytestmark = pytest.mark.unit


class _Collection:
    def __init__(self, collection_id, envelope=None, params=None):
        self.id = collection_id
        self._envelope = {"objects": []} if envelope is None else envelope
        self._params = params

    def get_objects(self, **params):
        if self._params is not None:
            self._params.update(params)
        return self._envelope


class _Server:
    def __init__(self, collections):
        self.api_roots = [type("Root", (), {"collections": collections})()]


def _install(monkeypatch, server):
    monkeypatch.setattr("taxii2client.v21.Server", server)


def test_server_failure_raises(monkeypatch):
    def _down(*_args, **_kwargs):
        raise ConnectionError("503 Service Unavailable")

    _install(monkeypatch, _down)

    with pytest.raises(ConnectionError, match="503 Service Unavailable"):
        fetch_taxii_collection(
            "https://taxii.example", "col-1", "tok", "cloudforce_one"
        )


def test_missing_collection_raises(monkeypatch):
    def _server(*_args, **_kwargs):
        return _Server([_Collection("other")])

    _install(monkeypatch, _server)

    with pytest.raises(LookupError, match="col-1"):
        fetch_taxii_collection(
            "https://taxii.example", "col-1", "tok", "cloudforce_one"
        )


def test_empty_envelope_returns_nothing_and_since_is_added_after(monkeypatch):
    watermark = datetime(2026, 9, 29, 12, 0, 0)
    seen = {}

    def _server(*_args, **_kwargs):
        return _Server([_Collection("col-1", params=seen)])

    _install(monkeypatch, _server)

    assert (
        fetch_taxii_collection(
            "https://taxii.example",
            "col-1",
            "tok",
            "cloudforce_one",
            since=watermark,
        )
        == []
    )
    assert seen == {"added_after": watermark.isoformat() + "Z"}
