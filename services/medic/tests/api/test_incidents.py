"""`GET /v1/incidents` and `GET /v1/incidents/{incident_id}` against X2's schemas."""

from __future__ import annotations

import copy
import json
from urllib.parse import quote

import pytest
import yaml

from services.medic.api.incidents import rateability
from services.medic.engine.core import BLIND_META
from services.medic.tests.api.schema import SPEC_PATH, errors
from services.medic.tests.api.test_decisions import _serve, _stop, _write
from services.medic.tests.api.test_server import KEY, assert_problem, call
from services.medic.tests.store.chains import draft, load, seed_instance

H = {"X-Medic-Key": KEY}
CHAIN = load("chain-01-pilot-day.jsonl")
OPENED = CHAIN[0]


def _opened(iid, at, *, route="routed", subject="vigil", advice=True, lane=2):
    rec = draft(OPENED)
    body = rec["body"]
    body.update(incident_id=iid, active_since=at, route=route, subject=subject)
    body["lane"] = {"value": lane, "reason": "rule"}
    if not advice:
        body["advice"] = None
    if subject == "watcher":
        body.update(copy.deepcopy(BLIND_META), runbook=None)
    rec["at"] = at
    return rec


def _rec(kind, at, iid, **body):
    return {"v": 1, "type": kind, "at": at, "body": {"incident_id": iid, **body}}


def _fb(iid, admin, value, at="2026-12-07T10:00:00Z", source="live"):
    extra = {"replay_head": "9" * 64} if source == "replay" else {}
    return _rec("feedback", at, iid, admin=admin, value=value, source=source, **extra)


def _iid(n: int) -> str:
    return f"inc_{n:08d}"


@pytest.fixture
def srv(tmp_path):
    server, data = _serve(tmp_path)
    seed_instance(data)
    server.data = data
    yield server
    _stop(server)


def _list(srv, query=""):
    target = "/v1/incidents" + (("?" + query) if query else "")
    status, headers, body = call(srv, target=target, headers=H)
    assert status == 200, body
    assert headers["content-type"] == "application/json"
    doc = json.loads(body)
    assert errors("IncidentPage", doc) == []
    return doc


def test_chain_01_folds_into_the_contract_example(srv) -> None:
    _write(srv.data, [draft(r) for r in CHAIN])
    spec = yaml.safe_load(SPEC_PATH.read_text())
    example = spec["components"]["examples"]["IncidentSummaryResolved"]["value"]
    doc = _list(srv, "state=all")
    by_id = {s["incident_id"]: s for s in doc["items"]}
    assert by_id[example["incident_id"]] == example
    # Newest opened_at first; replay feedback (seq 10) is never a rating.
    assert [s["opened_at"] for s in doc["items"]] == sorted(
        (s["opened_at"] for s in doc["items"]), reverse=True
    )
    assert by_id["inc_32e131d93ab99f90182373ae"]["ratings"] == []
    assert by_id["inc_32e131d93ab99f90182373ae"]["escalation_simulated"] is True
    assert by_id["inc_32e131d93ab99f90182373ae"]["last_seq"] == 10  # adjudication too

    status, _, body = call(
        srv, target=f"/v1/incidents/{example['incident_id']}", headers=H
    )
    assert status == 200
    detail = json.loads(body)
    assert errors("IncidentDetail", detail) == []
    assert detail == {"summary": example, "opened": CHAIN[0]}


def test_a_suppressed_child_nests_under_its_parent(srv) -> None:
    parent, child = _iid(1), _iid(2)
    _write(
        srv.data,
        [
            _opened(parent, "2026-12-07T09:00:00Z"),
            _opened(child, "2026-12-07T09:00:15Z", route="held"),
            _rec(
                "incident_updated",
                "2026-12-07T09:00:15Z",
                child,
                change="suppressed",
                suppressed_by=parent,
            ),
        ],
    )
    doc = _list(srv)
    assert [s["incident_id"] for s in doc["items"]] == [parent]
    assert doc["items"][0]["suppressed_children"] == {
        "count": 1,
        "incident_ids": [child],
    }
    assert doc["counts"] == {"incidents": 1, "rateable": 1, "rated": 0}

    rows = _list(srv, "include_suppressed=true")["items"]
    assert [s["incident_id"] for s in rows] == [child, parent]
    assert rows[0]["state"] == "suppressed" and rows[0]["suppressed_by"] == parent
    assert rows[0]["rateable"] is False and rows[0]["allowed_feedback"] == []

    _, _, body = call(srv, target=f"/v1/incidents/{child}", headers=H)
    assert json.loads(body)["summary"]["state"] == "suppressed"

    # Its parent resolves while it is still open: it is a row, not hidden.
    _write(
        srv.data,
        [
            _rec(
                "incident_resolved",
                "2026-12-07T09:10:00Z",
                parent,
                how="cleared",
                eval="false",
            )
        ],
    )
    doc = _list(srv)
    assert [s["incident_id"] for s in doc["items"]] == [child]
    assert doc["items"][0]["state"] == "suppressed"


def test_limit_1_pages_every_incident_once_including_ties(srv) -> None:
    same = "2026-12-07T09:00:00Z"
    ids = [_iid(n) for n in (3, 1, 2)]
    _write(
        srv.data,
        [_opened(ids[0], "2026-12-07T09:30:00Z")]
        + [_opened(i, same) for i in ids[1:]]
        + [_fb(ids[1], "user:a", "agree")],
    )
    seen, cursor, pages = [], None, []
    while True:
        doc = _list(srv, "limit=1" + (f"&cursor={cursor}" if cursor else ""))
        pages.append(doc["counts"])
        seen += [s["incident_id"] for s in doc["items"]]
        cursor = doc["next_cursor"]
        if cursor is None:
            break
    assert seen == [_iid(3), _iid(1), _iid(2)]
    # Counts ignore paging and `unrated`.
    assert pages == [{"incidents": 3, "rateable": 3, "rated": 1}] * 3
    unrated = _list(srv, "unrated=true")
    assert unrated["counts"] == pages[0]
    assert [s["incident_id"] for s in unrated["items"]] == [_iid(3), _iid(2)]
    # The cursor is bound to its filters.
    assert_problem(
        *call(
            srv,
            target=f"/v1/incidents?limit=1&lane=2&cursor={_first_cursor(srv)}",
            headers=H,
        ),
        400,
        "invalid_cursor",
    )


def _first_cursor(srv) -> str:
    return _list(srv, "limit=1")["next_cursor"]


def test_state_lane_subject_and_time_filters(srv) -> None:
    a, b, w = _iid(1), _iid(2), _iid(3)
    _write(
        srv.data,
        [
            _opened(a, "2026-12-07T09:00:00Z", lane=1),
            _opened(b, "2026-12-07T10:00:00Z"),
            _opened(w, "2026-12-07T11:00:00Z", subject="watcher", advice=False, lane=3),
            _rec(
                "incident_resolved",
                "2026-12-07T09:30:00Z",
                a,
                how="cleared",
                eval="false",
            ),
        ],
    )

    def ids(q):
        return [s["incident_id"] for s in _list(srv, q)["items"]]

    assert ids("") == [w, b]
    assert ids("state=resolved") == [a]
    assert ids("state=all&lane=1") == [a]
    assert ids("subject=watcher") == [w]
    after = quote("2026-12-07T10:00:00.5Z")
    assert ids(f"state=all&opened_after={after}") == [w]
    assert ids("state=all&opened_before=2026-12-07T10:00:00Z") == [a]


def test_rateability() -> None:
    base = {"state": "firing", "resolved_how": None, "routed_at": "x"}
    base |= {"subject": "vigil", "has_fix": True}
    assert rateability(base) == (
        ["agree", "disagree", "wrong_lane", "false_alarm"],
        None,
    )
    assert rateability(base | {"has_fix": False})[0] == [
        "agree",
        "wrong_lane",
        "false_alarm",
    ]
    assert rateability(base | {"subject": "watcher"}) == (
        ["agree", "false_alarm"],
        None,
    )
    assert rateability(base | {"state": "suppressed"}) == ([], "suppressed")
    assert rateability(
        base | {"state": "resolved", "resolved_how": "closed_quietly"}
    ) == (
        [],
        "closed_quietly",
    )
    assert rateability(base | {"state": "held", "routed_at": None}) == (
        [],
        "not_routed",
    )
    resolved = base | {"state": "resolved", "resolved_how": "cleared"}
    assert rateability(resolved)[0]


def test_held_then_routed_and_latest_rating_per_admin(srv) -> None:
    iid = _iid(1)
    _write(srv.data, [_opened(iid, "2026-12-07T09:00:00Z", route="held")])
    row = _list(srv)["items"][0]
    assert (row["state"], row["routed_at"], row["rateable"]) == ("held", None, False)
    _write(
        srv.data,
        [
            _rec("incident_updated", "2026-12-07T09:05:00Z", iid, change="routed"),
            _fb(iid, "user:a", "agree"),
            _fb(iid, "user:b", "false_alarm"),
            _fb(iid, "user:a", "wrong_lane", at="2026-12-07T10:01:00Z"),
        ],
    )
    row = _list(srv)["items"][0]
    assert (row["state"], row["routed_at"]) == ("firing", "2026-12-07T09:05:00Z")
    assert [(r["admin"], r["value"]) for r in row["ratings"]] == [
        ("user:b", "false_alarm"),
        ("user:a", "wrong_lane"),
    ]


def test_reopened_clears_the_resolution(srv) -> None:
    iid = _iid(1)
    _write(
        srv.data,
        [
            _opened(iid, "2026-12-07T09:00:00Z"),
            _rec(
                "incident_updated",
                "2026-12-07T09:05:00Z",
                iid,
                change="resolving",
                eval="false",
            ),
            _rec(
                "incident_resolved",
                "2026-12-07T09:10:00Z",
                iid,
                how="cleared",
                eval="false",
            ),
            _rec(
                "incident_updated",
                "2026-12-07T09:20:00Z",
                iid,
                change="reopened",
                reopen_count=1,
            ),
        ],
    )
    row = _list(srv)["items"][0]
    assert (row["state"], row["resolved_at"], row["reopen_count"]) == (
        "firing",
        None,
        1,
    )
    assert row["last_eval"] == "true"


@pytest.mark.parametrize(
    "query, field",
    [
        ("state=closed", "state"),
        ("lane=4", "lane"),
        ("subject=vigi", "subject"),
        ("unrated=yes", "unrated"),
        ("include_suppressed=1", "include_suppressed"),
        ("opened_after=2026-12-07", "opened_after"),
        ("opened_before=2026-13-07T00:00:00Z", "opened_before"),
        ("limit=0", "limit"),
        ("limit=201", "limit"),
        ("lane=1&lane=2", "lane"),
        ("nope=1", "nope"),
    ],
)
def test_bad_list_parameters_are_400(srv, query, field) -> None:
    doc = assert_problem(
        *call(srv, target="/v1/incidents?" + query, headers=H), 400, "invalid_parameter"
    )
    assert doc["field"] == field


def test_list_accepts_limit_200(srv) -> None:
    _write(srv.data, [])
    assert _list(srv, "limit=200")["items"] == []


def test_bad_cursor_and_detail_errors(srv) -> None:
    _write(srv.data, [])
    assert_problem(
        *call(srv, target="/v1/incidents?cursor=AAAA", headers=H), 400, "invalid_cursor"
    )
    assert_problem(
        *call(srv, target="/v1/incidents?cursor=%25", headers=H), 400, "invalid_cursor"
    )
    assert_problem(
        *call(srv, target="/v1/incidents/inc_00000009", headers=H), 404, "not_found"
    )
    doc = assert_problem(
        *call(srv, target="/v1/incidents/nope", headers=H), 400, "invalid_parameter"
    )
    assert doc["field"] == "incident_id"
    assert_problem(
        *call(srv, target="/v1/incidents/inc_00000009?x=1", headers=H),
        400,
        "invalid_parameter",
    )
    assert_problem(*call(srv, target="/v1/incidents"), 401, "unauthorized")
    # Feedback (#2085), pack (#2025) and export stay unserved.
    for target in ("/v1/incidents/inc_00000009/feedback", "/v1/pack", "/v1/export"):
        assert_problem(*call(srv, target=target, headers=H), 404, "not_found")


def test_no_store_is_503_busy(tmp_path) -> None:
    srv, _ = _serve(tmp_path)  # the data dir was never created
    try:
        assert_problem(*call(srv, target="/v1/incidents", headers=H), 503, "busy")
        assert_problem(
            *call(srv, target=f"/v1/incidents/{_iid(1)}", headers=H), 503, "busy"
        )
    finally:
        _stop(srv)


def test_a_purged_opening_is_not_an_incident(srv) -> None:
    _write(srv.data, [_fb(_iid(7), "user:a", "agree")])
    assert _list(srv, "state=all")["items"] == []
