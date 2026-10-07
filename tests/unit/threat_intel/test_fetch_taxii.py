"""A failed TAXII fetch must raise, so the poller keeps its watermark (#1278).

An empty envelope is a real answer and still returns []. A missing
taxii2-client wheel raises too: it is in requirements.txt, so its absence is a
broken deploy and must not read as an empty feed.
"""

import builtins
import logging

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


def test_missing_taxii_client_raises(monkeypatch):
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("taxii2client"):
            raise ModuleNotFoundError("No module named 'taxii2client'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    with pytest.raises(ModuleNotFoundError, match="taxii2client"):
        fetch_taxii_collection(
            "https://taxii.example", "col-1", "tok", "cloudforce_one"
        )


def test_malformed_objects_are_one_aggregated_warning(monkeypatch, caplog):
    def _server(*_args, **_kwargs):
        return _Server([_Collection("col-1", envelope={"objects": ["x", "y"]})])

    def _boom(obj, **_kwargs):
        raise ValueError(f"bad {obj}")

    _install(monkeypatch, _server)
    monkeypatch.setattr(
        "core.threat_intel.threat_feed_service.parse_stix_indicator", _boom
    )

    with caplog.at_level(logging.WARNING):
        assert (
            fetch_taxii_collection(
                "https://taxii.example", "col-1", "tok", "cloudforce_one"
            )
            == []
        )

    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(messages) == 1
    assert "2 malformed of 2" in messages[0]
