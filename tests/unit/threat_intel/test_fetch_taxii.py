"""A failed TAXII fetch must raise, so the poller keeps its watermark (#1278).

An empty envelope is a real answer and still returns []. A missing
taxii2-client wheel raises too: it is in requirements.txt, so its absence is a
broken deploy and must not read as an empty feed.
"""

import builtins
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from core.threat_intel.threat_feed_service import fetch_taxii_collection

pytestmark = pytest.mark.unit

_CT = "application/taxii+json;version=2.1"


def _indicator(n, ip):
    return {
        "type": "indicator",
        "spec_version": "2.1",
        "id": f"indicator--0000000{n}-0000-4000-8000-000000000000",
        "created": "2026-01-01T00:00:00.000Z",
        "modified": "2026-01-01T00:00:00.000Z",
        "pattern": f"[ipv4-addr:value = '{ip}']",
        "pattern_type": "stix",
        "valid_from": "2026-01-01T00:00:00Z",
    }


class _Taxii:
    def __init__(self, pages, status=200):
        self.pages = pages
        self.status = status
        self.requests = []

    def __enter__(self):
        taxii = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                taxii.requests.append(
                    (url.path, query, self.headers.get("Authorization"))
                )
                body = taxii.route(url.path, query)
                if body is None:
                    self.send_response(404 if taxii.status == 200 else taxii.status)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", _CT)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.url = self.base + "/taxii2/"
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()

    def route(self, path, query):
        if self.status != 200:
            return None
        if path == "/taxii2/":
            return {"title": "t", "api_roots": [self.base + "/api1/"]}
        if path == "/api1/":
            return {"title": "r", "versions": [_CT], "max_content_length": 10**7}
        if path == "/api1/collections/":
            return {
                "collections": [
                    {
                        "id": "col-1",
                        "title": "c",
                        "can_read": True,
                        "can_write": False,
                        "media_types": [_CT],
                    }
                ]
            }
        if path == "/api1/collections/col-1/objects/":
            page = int(query.get("next", ["0"])[0])
            envelope = {"objects": self.pages[page]}
            if page + 1 < len(self.pages):
                envelope.update(more=True, next=str(page + 1))
            return envelope
        return None

    def object_queries(self):
        return [q for path, q, _ in self.requests if path.endswith("/objects/")]


def test_bearer_token_is_sent_and_every_page_is_read_with_the_same_filter():
    pages = [[_indicator(1, "198.51.100.1")], [_indicator(2, "198.51.100.2")]]
    with _Taxii(pages) as taxii:
        out = fetch_taxii_collection(
            taxii.url,
            "col-1",
            "tok",
            "cloudforce_one",
            since=datetime(2026, 9, 29, 12, 0, 0),
        )

    assert sorted(i.indicator_value for i in out) == ["198.51.100.1", "198.51.100.2"]
    assert {auth for _, _, auth in taxii.requests} == {"Bearer tok"}
    assert taxii.object_queries() == [
        {"added_after": ["2026-09-29T12:00:00Z"]},
        {"added_after": ["2026-09-29T12:00:00Z"], "next": ["1"]},
    ]


def test_server_failure_raises():
    with _Taxii([[]], status=503) as taxii:
        with pytest.raises(requests.HTTPError, match="503"):
            fetch_taxii_collection(taxii.url, "col-1", "tok", "cloudforce_one")


def test_missing_collection_raises():
    with _Taxii([[]]) as taxii:
        with pytest.raises(LookupError, match="col-2"):
            fetch_taxii_collection(taxii.url, "col-2", "tok", "cloudforce_one")


def test_empty_envelope_returns_nothing_and_since_is_added_after():
    watermark = datetime(2026, 9, 29, 12, 0, 0)
    with _Taxii([[]]) as taxii:
        out = fetch_taxii_collection(
            taxii.url, "col-1", "tok", "cloudforce_one", since=watermark
        )

    assert out == []
    assert taxii.object_queries() == [{"added_after": ["2026-09-29T12:00:00Z"]}]


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


def test_malformed_objects_are_one_aggregated_warning(caplog):
    with _Taxii([["x", "y"]]) as taxii:
        with caplog.at_level(logging.WARNING):
            out = fetch_taxii_collection(taxii.url, "col-1", "tok", "cloudforce_one")

    assert out == []
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(messages) == 1
    assert "2 malformed of 2" in messages[0]
