"""/api/logs against Postgres: ingest, search, sources filter, histogram, paging.

Events are posted in 2001, a window nothing else uses, and removed afterwards.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

WINDOW = {"start": "2001-03-04T10:00:00Z", "end": "2001-03-04T11:00:00Z"}

EVENTS = [
    {
        "id": "test-logs-1",
        "time": "2001-03-04T10:01:00Z",
        "source": "winsec",
        "event_class": "Authentication",
        "severity": "low",
        "summary": "j.doe failed logon to dc01: bad password (4625)",
        "user": "j.doe",
        "action": "logon_failed",
    },
    {
        "id": "test-logs-2",
        "time": "2001-03-04T10:02:00Z",
        "source": "zeek",
        "event_class": "Network Activity",
        "severity": "high",
        "summary": "lnx-build-01 -> 10.0.10.20 7000/tcp",
        "src_host": "lnx-build-01",
        "src_ip": "10.0.30.80",
        "dst_ip": "10.0.10.20",
        "dst_port": 7000,
        "loglm": 0.98,
        "incident": "CASE-7",
    },
    {
        "id": "test-logs-3",
        "time": "2001-03-04T10:30:00",
        "source": "Sysmon",
        "severity": "high",
        "summary": "WINWORD.EXE started powershell.exe",
        "user": "j.doe",
        "raw": {"CommandLine": "powershell -nop -enc AAAA", "User": "EXAMPLE\\j.doe"},
        "mitre": "T1059.001",
    },
    {
        "id": "test-logs-4",
        "time": "2001-03-04T10:59:00Z",
        "source": "entra",
        "event_class": "Authentication",
        "summary": "j.doe@example.com signed in to Slack from Chicago, US",
        "user": "j.doe",
        "action": "signin",
    },
]


@pytest.fixture
def client(throwaway_database):
    from services.api.routers import logs

    app = FastAPI()
    app.include_router(logs.router, prefix=logs.ROUTER_META.prefix)
    app.dependency_overrides[logs._WRITE.dependency] = lambda: None

    def purge():
        with unit_of_work() as s:
            s.execute(text("DELETE FROM log_events WHERE event_id LIKE 'test-logs-%'"))

    purge()
    yield TestClient(app)
    purge()


@pytest.fixture
def events(client):
    r = client.post("/api/logs/events", json={"events": EVENTS})
    assert r.status_code == 200, r.text
    assert r.json() == {"received": 4, "stored": 4, "skipped": 0}


def search(client, **params):
    r = client.get("/api/logs/search", params={**WINDOW, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_posting_the_same_events_again_stores_nothing_new(client, events):
    again = client.post("/api/logs/events", json={"events": EVENTS[:2]})
    assert again.json() == {"received": 2, "stored": 0, "skipped": 2}
    assert search(client)["total"] == 4


def test_a_bad_batch_is_refused_whole(client):
    bad = [{**EVENTS[0], "id": "test-logs-9"}, {**EVENTS[1], "loglm": 4}]
    assert client.post("/api/logs/events", json={"events": bad}).status_code == 422
    assert client.post("/api/logs/events", json={"events": []}).status_code == 422
    assert search(client)["total"] == 0


def test_everything_in_the_window_newest_first(client, events):
    body = search(client)
    assert body["total"] == 4
    assert [e["id"] for e in body["events"]] == [
        "test-logs-4",
        "test-logs-3",
        "test-logs-2",
        "test-logs-1",
    ]
    assert body["by_source"] == {"winsec": 1, "zeek": 1, "sysmon": 1, "entra": 1}
    assert sum(body["histogram"]["counts"]) == 4
    assert sum(body["histogram"]["incident"]) == 1


def test_free_words_reach_the_raw_record_and_phrases_match_whole(client, events):
    # A leading minus leaves out, even a bare word.
    assert search(client, q="-nop")["total"] == 3
    assert [e["id"] for e in search(client, q="AAAA")["events"]] == ["test-logs-3"]
    assert [e["id"] for e in search(client, q='"failed logon"')["events"]] == [
        "test-logs-1"
    ]


def test_keys_negation_and_either(client, events):
    assert search(client, q="user:j.doe -source:entra")["total"] == 2
    assert search(client, q="sev:high,critical")["total"] == 2
    assert [e["id"] for e in search(client, q="loglm>0.9 ip:10.0.10.")["events"]] == [
        "test-logs-2"
    ]
    assert search(client, q="incident:case-7")["total"] == 1
    assert search(client, q="incident:*")["total"] == 1
    assert search(client, q="-incident:*")["total"] == 3
    # events without the field are not left out with it (SQL's NOT NULL is NULL)
    assert search(client, q="-incident:CASE-7")["total"] == 3
    assert search(client, q="-user:j.doe")["total"] == 1


def test_backslashes_and_like_wildcards_are_literal(client, events):
    # the raw record is stored as JSON, which doubles the backslash
    assert [e["id"] for e in search(client, q="EXAMPLE\\j.doe")["events"]] == [
        "test-logs-3"
    ]
    assert search(client, q="j_doe")["total"] == 0


def test_source_filter_keeps_counts_for_every_source(client, events):
    body = search(client, q="j.doe", sources="winsec,sysmon")
    assert body["total"] == 2
    assert body["by_source"] == {"winsec": 1, "sysmon": 1, "entra": 1}


def test_paging_and_one_event(client, events):
    first = search(client, limit=3)
    rest = search(client, limit=3, offset=3)
    assert len(first["events"]) == 3
    assert [e["id"] for e in rest["events"]] == ["test-logs-1"]
    one = client.get("/api/logs/events/test-logs-3").json()
    # A time without a zone is read as UTC.
    assert one["time"] == "2001-03-04T10:30:00Z"
    assert one["mitre"] == "T1059.001"
    assert one["raw"]["CommandLine"].startswith("powershell")
    assert client.get("/api/logs/events/nope").status_code == 404


def test_sources_count_what_is_held(client, events):
    sources = {
        s["source"]: s for s in client.get("/api/logs/sources").json()["sources"]
    }
    assert sources["zeek"]["events"] >= 1


def test_a_backwards_window_is_refused(client, events):
    r = client.get(
        "/api/logs/search", params={"start": WINDOW["end"], "end": WINDOW["start"]}
    )
    assert r.status_code == 422
