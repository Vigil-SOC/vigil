"""Triage read: intake words, rank order, quiet sources, empty pickup share."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.models import (
    Case,
    FederationSource,
    Finding,
    IntakeTrigger,
    Investigation,
)
from core.storage.unit_of_work import unit_of_work
from services.api.triage_read import ROW_CAP, triage_payload

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

NOW = datetime(2099, 7, 1, 12, 0, 0)
DAY = NOW.date()


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(IntakeTrigger).filter(
                IntakeTrigger.finding_id.like("tr-%")
                | IntakeTrigger.investigation_id.like("tr-%")
                | IntakeTrigger.case_id.like("tr-%")
                | IntakeTrigger.merged_into.like("tr-%")
            ).delete(synchronize_session=False)
            session.query(Investigation).filter(
                Investigation.investigation_id.like("tr-%")
            ).delete(synchronize_session=False)
            session.query(Case).filter(Case.case_id.like("tr-%")).delete(
                synchronize_session=False
            )
            session.query(Finding).filter(Finding.finding_id.like("tr-%")).delete(
                synchronize_session=False
            )
            session.query(FederationSource).filter(
                FederationSource.source_id.like("tr-%")
            ).delete(synchronize_session=False)

    purge()
    yield
    purge()


@pytest.fixture
def client(authenticate_app):
    from services.api.routers import triage

    app = FastAPI()
    app.include_router(triage.router, prefix=triage.ROUTER_META.prefix)
    authenticate_app(app)
    return TestClient(app)


def _finding(finding_id: str, severity: str, **kw) -> None:
    with unit_of_work() as session:
        session.add(
            Finding(
                finding_id=finding_id,
                data_source=kw.pop("data_source", "tr-src"),
                severity=severity,
                external_id=kw.get("external_id"),
                evidence_links=kw.get("evidence_links"),
                title=kw.get("title"),
                description=kw.get("description"),
                entity_context=kw.get("entity_context"),
                ai_enrichment=kw.get("ai_enrichment"),
                created_at=kw.get("created_at", NOW),
                noise_marked_at=kw.get("noise_marked_at"),
            )
        )


def _case(case_id: str, activities: list | None = None) -> None:
    with unit_of_work() as session:
        session.add(
            Case(
                case_id=case_id,
                title=case_id,
                description="",
                activities=activities or [],
            )
        )


def _investigation(investigation_id: str, workflow_id: str) -> None:
    with unit_of_work() as session:
        session.add(
            Investigation(
                investigation_id=investigation_id,
                workflow_id=workflow_id,
                trigger_type="manual",
                trigger_ids=[],
                status="assigned",
                workdir="/tmp/tr",
            )
        )


def _trigger(**kw) -> int:
    with unit_of_work() as session:
        row = IntakeTrigger(
            kind=kw.get("kind", "detection"),
            state=kw["state"],
            finding_id=kw.get("finding_id"),
            priority=kw.get("priority", "low"),
            payload=kw.get("payload") or {},
            investigation_id=kw.get("investigation_id"),
            case_id=kw.get("case_id"),
            merged_into=kw.get("merged_into"),
            reason=kw.get("reason"),
            created_at=kw.get("created_at", NOW),
            decided_at=kw.get("decided_at"),
        )
        session.add(row)
        session.flush()
        return row.id


def _by_id(payload: dict) -> dict[int, dict]:
    return {row["id"]: row for row in payload["rows"]}


def _counts() -> dict:
    return triage_payload(now=NOW, day=DAY)["counts"]


def _added(before: dict, after: dict) -> dict:
    """What a test's own rows added to ``counts``.

    ``counts`` covers the whole intake table, and the database is shared with
    every other DB-backed suite, so rows they leave behind are in both reads
    and cancel out here.
    """
    added = {"total": after["total"] - before["total"]}
    for key in ("kind", "source", "state"):
        added[key] = {
            name: n - before[key].get(name, 0)
            for name, n in after[key].items()
            if n != before[key].get(name, 0)
        }
    return added


def test_five_state_words_and_an_investigation_id_with_no_door():
    before = _counts()
    _case("tr-case-1")
    _investigation("tr-inv-ask", "incident-response")
    _finding(
        "tr-waiting",
        "high",
        data_source="splunk",
        evidence_links=[{"ref": "https://console.example/alert/1"}],
        title="Odd login alert",
        description="Odd login",
    )
    waiting = _trigger(state="queued", finding_id="tr-waiting", created_at=NOW)
    started = _trigger(
        state="launched",
        finding_id="tr-waiting",
        case_id="tr-case-1",
        investigation_id="tr-inv-started",
        created_at=NOW - timedelta(seconds=10),
        decided_at=NOW,
        payload={"workflow_id": "incident-response", "document": "not an ask"},
    )
    # A second launch of the same case today still counts once.
    bare_hunt = _trigger(
        state="launched",
        case_id="tr-case-1",
        investigation_id="tr-inv-started-2",
        kind="schedule",
        created_at=NOW - timedelta(seconds=4),
        decided_at=NOW,
    )
    picked = _trigger(
        state="launched",
        kind="schedule",
        investigation_id="tr-inv-picked",
        created_at=NOW - timedelta(seconds=5),
        decided_at=NOW,
        payload={
            "workflow_id": "threat-hunt",
            "hypothesis": "Hunt for T1059\non build hosts",
        },
    )
    added = _trigger(
        state="merged",
        finding_id="tr-waiting",
        merged_into="tr-case-1",
        investigation_id="tr-inv-added",
        created_at=NOW - timedelta(seconds=8),
        decided_at=NOW,
    )
    ghost = _trigger(
        state="merged",
        kind="human_ask",
        merged_into="tr-inv-ghost",
        investigation_id="tr-inv-ask",
        created_at=NOW - timedelta(seconds=6),
        decided_at=NOW,
        payload={"document": "please\nlook", "principal": "signed-secret"},
    )
    expired = _trigger(
        state="expired",
        finding_id="tr-waiting",
        investigation_id="tr-inv-expired",
        created_at=NOW - timedelta(seconds=3),
        decided_at=NOW,
    )

    payload = triage_payload(now=NOW, day=DAY)
    rows = _by_id(payload)
    assert rows[waiting]["state_label"] == "Waiting for triage"
    assert rows[waiting]["case_door"] is None
    assert rows[waiting]["pickup_seconds"] is None
    assert rows[waiting]["source"] == "splunk"
    assert rows[waiting]["source_link"] == "https://console.example/alert/1"
    assert rows[waiting]["title"] == "Odd login alert"
    assert rows[waiting]["description"] == "Odd login"

    assert rows[started]["state_label"] == "Started a case"
    assert rows[started]["case_door"] == "tr-case-1"
    assert rows[started]["pickup_seconds"] == 10
    assert rows[started]["workflow_id"] == "incident-response"
    assert rows[started]["document"] is None

    assert rows[picked]["state_label"] == "Picked up"
    assert rows[picked]["case_door"] is None
    assert rows[picked]["source"] == "Schedule"
    assert rows[picked]["workflow_id"] == "threat-hunt"
    assert rows[picked]["document"] == "Hunt for T1059 on build hosts"
    assert rows[bare_hunt]["document"] is None

    assert rows[added]["state_label"] == "Added to a case"
    assert rows[added]["case_door"] == "tr-case-1"

    assert rows[ghost]["state_label"] == "Added to a case"
    assert rows[ghost]["case_door"] is None
    assert rows[ghost]["source"] == "Ask"
    assert rows[ghost]["document"] == "please look"
    assert "signed-secret" not in str(rows[ghost])
    assert rows[ghost]["workflow_id"] == "incident-response"

    assert rows[expired]["state_label"] == "Expired"
    assert rows[expired]["pickup_seconds"] is None

    assert payload["strip"]["cases_created_today"] == 1
    info = payload["strip_info"]
    assert set(info) == {"picked_up", "waiting", "cases_created_today", "trust_floor"}
    assert all(item["source"] and item["calculation"] for item in info.values())
    assert info["trust_floor"]["limit"]
    assert set(payload["breakdown_info"]) == {"trust", "weight", "score"}

    assert all("tr-" not in row["state_label"] for row in payload["rows"])
    counts = payload["counts"]
    # the intake states partition the rows; "dismissed" counts across them
    assert counts["total"] == sum(
        n for name, n in counts["state"].items() if name != "dismissed"
    )
    added = _added(before, counts)
    assert added["total"] == 7
    assert added["state"] == {"queued": 1, "launched": 3, "merged": 2, "expired": 1}
    assert added["kind"] == {"detection": 4, "schedule": 2, "human_ask": 1}
    assert added["source"] == {"splunk": 4, "Schedule": 2, "Ask": 1}

    filtered = triage_payload(now=NOW, day=DAY, state="queued")
    assert filtered["counts"] == counts
    # matched is the filtered rows before the cap; the table is shared with other
    # suites, so compare with the queued count rather than a literal
    assert filtered["matched"] == counts["state"]["queued"]
    assert triage_payload(now=NOW, day=DAY)["matched"] == counts["total"]
    assert filtered["strip"]["waiting"] == payload["strip"]["waiting"]
    assert waiting in [row["id"] for row in filtered["rows"]]
    assert {row["state"] for row in filtered["rows"]} == {"queued"}


def _alert_added(finding_id: str, key: str, same_alert: bool = False) -> dict:
    return {
        "activity_type": "alert_added",
        "details": {
            "finding_id": finding_id,
            "matched_key": key,
            "same_alert": same_alert,
        },
    }


def test_a_merged_row_names_what_it_matched():
    _case(
        "tr-case-m",
        [
            _alert_added("tr-m-ip", "ip:27.102.116.43"),
            _alert_added("tr-m-host", "hostname:FIN-WS-1", same_alert=True),
        ],
    )
    ids = {}
    for name, reason in (
        ("tr-m-ip", "entity_on_open_case"),
        ("tr-m-host", "entity_on_open_case"),
        ("tr-m-held", "finding_on_open_case"),
        ("tr-m-old", None),
    ):
        _finding(name, "low")
        ids[name] = _trigger(
            state="merged",
            finding_id=name,
            merged_into="tr-case-m",
            reason=reason,
            decided_at=NOW,
        )

    rows = _by_id(triage_payload(now=NOW, day=DAY))

    assert rows[ids["tr-m-ip"]]["merge_note"] == "same attacker 27.102.116.43"
    assert rows[ids["tr-m-host"]]["merge_note"] == "same alert on FIN-WS-1"
    assert rows[ids["tr-m-held"]]["merge_note"] == "already on this case"
    assert rows[ids["tr-m-old"]]["merge_note"] is None


def test_queued_rows_follow_rank_intake_row_severity_before_age():
    _finding("tr-rank-high", "critical")
    _finding("tr-rank-low", "low")
    # Younger, but a higher band. Neither is in the last quarter of the TTL.
    high = _trigger(
        state="queued",
        finding_id="tr-rank-high",
        priority="low",
        created_at=NOW - timedelta(minutes=1),
    )
    low = _trigger(
        state="queued",
        finding_id="tr-rank-low",
        priority="low",
        created_at=NOW - timedelta(minutes=30),
    )
    ids = [
        row["id"]
        for row in triage_payload(now=NOW, day=DAY)["rows"]
        if row["state"] == "queued"
    ]
    assert ids.index(high) < ids.index(low)
    high_row = next(
        row for row in triage_payload(now=NOW, day=DAY)["rows"] if row["id"] == high
    )
    assert high_row["severity_band"] == "critical"
    assert high_row["last_quarter"] is False


def test_source_quiet_when_last_success_is_older_than_its_interval():
    with unit_of_work() as session:
        session.add(
            FederationSource(
                source_id="tr-stale",
                enabled=True,
                interval_seconds=60,
                last_success_at=NOW - timedelta(seconds=120),
            )
        )
        session.add(
            FederationSource(
                source_id="tr-fresh",
                enabled=True,
                interval_seconds=60,
                last_success_at=NOW - timedelta(seconds=10),
            )
        )
        session.add(
            FederationSource(
                source_id="tr-never",
                enabled=True,
                interval_seconds=60,
                last_success_at=None,
            )
        )
        session.add(
            FederationSource(
                source_id="tr-off",
                enabled=False,
                interval_seconds=60,
                last_success_at=NOW - timedelta(seconds=120),
            )
        )
    sources = {
        row["data_source"]: row for row in triage_payload(now=NOW, day=DAY)["sources"]
    }
    assert sources["tr-stale"]["quiet"] is True
    assert sources["tr-stale"]["lag_seconds"] > 60
    assert sources["tr-fresh"]["quiet"] is False
    assert sources["tr-never"]["quiet"] is True
    assert sources["tr-never"]["lag_seconds"] is None
    assert "tr-off" not in sources


def test_zero_arrival_day_leaves_the_pickup_share_empty():
    day = datetime(2098, 3, 3)
    payload = triage_payload(day=day.date(), now=day)
    assert payload["strip"]["picked_up"]["created_today"] == 0
    assert payload["strip"]["picked_up"]["share"] is None


def test_source_filter_keeps_an_older_row_the_cap_would_drop():
    """``?source=`` applies before the 200 cap."""
    before = _counts()
    _finding(
        "tr-kept-f", "low", data_source="tr-kept", created_at=NOW - timedelta(days=2)
    )
    kept = _trigger(
        state="expired",
        finding_id="tr-kept-f",
        created_at=NOW - timedelta(days=2),
        decided_at=NOW - timedelta(days=1),
    )
    _finding("tr-other-f", "low", data_source="tr-other")
    with unit_of_work() as session:
        session.add_all(
            IntakeTrigger(
                kind="detection",
                state="expired",
                finding_id="tr-other-f",
                priority="low",
                payload={},
                created_at=NOW,
                decided_at=NOW,
            )
            for _ in range(ROW_CAP)
        )

    unfiltered = [row["id"] for row in triage_payload(now=NOW, day=DAY)["rows"]]
    assert kept not in unfiltered
    assert len(unfiltered) == ROW_CAP
    filtered = triage_payload(now=NOW, day=DAY, source="tr-kept")
    assert [row["id"] for row in filtered["rows"]] == [kept]
    # counts see the rows the cap drops, and the filters leave them alone
    counts = _counts()
    added = _added(before, counts)
    assert added["total"] == ROW_CAP + 1
    assert added["source"] == {"tr-kept": 1, "tr-other": ROW_CAP}
    assert added["state"] == {"expired": ROW_CAP + 1}
    assert filtered["counts"] == counts
    for narrow in ({"kind": "schedule"}, {"state": "queued"}):
        assert triage_payload(now=NOW, day=DAY, **narrow)["counts"] == counts


def test_queued_rows_lead_when_decided_rows_outnumber_the_cap():
    """The decided LIMIT leaves room for every queued row, ahead of them."""
    _finding("tr-cap-q", "low", data_source="tr-cap")
    _finding("tr-cap-d", "low", data_source="tr-cap")
    waiting = _trigger(
        state="queued", finding_id="tr-cap-q", created_at=NOW - timedelta(days=3)
    )
    with unit_of_work() as session:
        session.add_all(
            IntakeTrigger(
                kind="detection",
                state="launched",
                finding_id="tr-cap-d",
                priority="low",
                payload={},
                created_at=NOW,
                decided_at=NOW - timedelta(minutes=i),
            )
            for i in range(ROW_CAP + 5)
        )
    payload = triage_payload(now=NOW, day=DAY, source="tr-cap")
    rows = payload["rows"]
    assert payload["matched"] == ROW_CAP + 6
    assert len(rows) == ROW_CAP
    assert rows[0]["id"] == waiting
    decided = [row["decided_at"] for row in rows[1:]]
    assert decided == sorted(decided, reverse=True)


def test_finding_only_source_has_null_lag_and_is_not_quiet():
    _finding("tr-hook-f", "low", data_source="tr-hook", created_at=NOW)
    sources = {
        row["data_source"]: row for row in triage_payload(now=NOW, day=DAY)["sources"]
    }
    assert sources["tr-hook"]["arrivals"] == 1
    assert sources["tr-hook"]["lag_seconds"] is None
    assert sources["tr-hook"]["quiet"] is None


def test_route_returns_the_arrival_info(client):
    response = client.get("/api/triage")
    assert response.status_code == 200
    body = response.json()
    assert body["arrival_info"] == (
        "Arrivals count every finding stored today. The list is the intake rows."
    )


def test_each_alert_shows_the_verdict_triage_saved_and_nothing_it_did_not():
    _finding(
        "tr-v-done",
        "critical",
        ai_enrichment={
            "ai_triage": {
                "timestamp": "2099-07-01T11:59:00",
                "result": {
                    "severity": "critical",
                    "category": "intrusion",
                    "confidence": 0.95,
                    "reasoning": "Hidden encoded PowerShell from a UAC bypass.",
                    "recommended_action": "isolate",
                },
            },
            "triage_confidence": 0.95,
        },
    )
    _finding(
        "tr-v-partial",
        "low",
        ai_enrichment={"ai_triage": {"result": {"category": "other", "confidence": 7}}},
    )
    _finding(
        "tr-v-failed",
        "high",
        ai_enrichment={"ai_triage_error": "timed out after 60s"},
    )
    _finding("tr-v-none", "medium")
    ids = {
        name: _trigger(state="queued", finding_id=f"tr-v-{name}")
        for name in ("done", "partial", "failed", "none")
    }
    ask = _trigger(kind="human_ask", state="queued", payload={"question": "?"})

    rows = _by_id(triage_payload(now=NOW, day=DAY))

    assert rows[ids["done"]]["triage"] == {
        "state": "done",
        "severity": "critical",
        "category": "intrusion",
        "confidence": 0.95,
        "reasoning": "Hidden encoded PowerShell from a UAC bypass.",
        "triaged_at": "2099-07-01T11:59:00",
    }
    # A confidence out of range and a missing severity stay null, not defaulted.
    assert rows[ids["partial"]]["triage"] == {
        "state": "done",
        "severity": None,
        "category": "other",
        "confidence": None,
        "reasoning": None,
        "triaged_at": None,
    }
    assert rows[ids["failed"]]["triage"] == {
        "state": "failed",
        "error": "timed out after 60s",
    }
    assert rows[ids["none"]]["triage"] == {"state": "none"}
    assert rows[ask]["triage"] is None


def test_dismissed_lists_the_rows_whose_alert_was_marked_as_noise(client):
    before = _counts()
    _finding("tr-noise", "low", data_source="tr-noise-src", noise_marked_at=NOW)
    _finding("tr-kept", "low", data_source="tr-noise-src")
    noise = _trigger(state="expired", finding_id="tr-noise", decided_at=NOW)
    kept = _trigger(state="expired", finding_id="tr-kept", decided_at=NOW)

    counts = _counts()
    assert _added(before, counts)["state"] == {"expired": 2, "dismissed": 1}

    response = client.get("/api/triage", params={"state": "dismissed"})
    assert response.status_code == 200
    narrowed = response.json()
    assert narrowed["matched"] == counts["state"]["dismissed"]
    ids = {row["id"] for row in narrowed["rows"]}
    assert noise in ids and kept not in ids
    # the row still says what intake did with it
    assert _by_id(narrowed)[noise]["state"] == "expired"
    # and it narrows with the other filters
    both = triage_payload(now=NOW, day=DAY, state="dismissed", source="tr-noise-src")
    assert [row["id"] for row in both["rows"]] == [noise]


def test_a_finding_with_no_intake_row_and_no_case_is_listed_not_picked_up():
    from core.storage.models import case_findings

    before = triage_payload(now=NOW, day=DAY)
    src = "tr-loose-src"
    _finding("tr-loose", "high", data_source=src, title="Never queued", created_at=NOW - timedelta(hours=1))
    _finding("tr-loose-noise", "low", data_source=src, created_at=NOW, noise_marked_at=NOW)
    _finding("tr-blank", "low", data_source="")
    _finding("tr-cased", "low", data_source=src)
    _case("tr-case-loose")
    with unit_of_work() as session:
        session.execute(case_findings.insert().values(case_id="tr-case-loose", finding_id="tr-cased", added_at=NOW))
    _finding("tr-q", "low", data_source=src)
    _finding("tr-q-noise", "low", data_source=src, noise_marked_at=NOW)
    _finding("tr-exp", "low", data_source=src)
    queued = _trigger(state="queued", finding_id="tr-q")
    queued_noise = _trigger(state="queued", finding_id="tr-q-noise")
    expired = _trigger(state="expired", finding_id="tr-exp", decided_at=NOW - timedelta(minutes=30))

    payload = triage_payload(now=NOW, day=DAY)
    counts = payload["counts"]
    added = _added(before["counts"], counts)
    # the case-linked finding is not a row; the noise marks count across states
    assert added["state"] == {"not_picked_up": 3, "queued": 2, "expired": 1, "dismissed": 2}
    assert added["source"] == {src: 5, "Unknown": 1}
    assert added["kind"] == {"detection": 6}
    # every alert has a source, Unknown included, so the source chips add up to Alert
    assert sum(n for name, n in counts["source"].items() if name not in ("Schedule", "Ask")) == counts["kind"]["detection"]
    assert payload["matched"] == counts["total"]
    # "waiting" is still the intake queue
    assert payload["strip"]["waiting"] - before["strip"]["waiting"] == 2

    narrowed = triage_payload(now=NOW, day=DAY, source=src)
    order = [row["finding_id"] for row in narrowed["rows"]]
    # queued first, then newest first: a never-queued row by its arrival
    assert set(order[:2]) == {"tr-q", "tr-q-noise"}
    assert order[2:] == ["tr-loose-noise", "tr-exp", "tr-loose"]
    rows = {row["finding_id"]: row for row in narrowed["rows"]}
    loose = rows["tr-loose"]
    assert loose["id"] < 0 and loose["id"] != rows["tr-loose-noise"]["id"]
    assert (loose["kind"], loose["state"], loose["state_label"]) == ("detection", "not_picked_up", "Not picked up")
    assert loose["title"] == "Never queued" and loose["dismissed"] is False
    assert loose["id"] == triage_payload(now=NOW, day=DAY, source=src)["rows"][-1]["id"]
    # a noise mark is the word on a waiting or never-queued row, not on a decided one
    assert rows["tr-loose-noise"]["state_label"] == "Dismissed as noise"
    assert rows["tr-loose-noise"]["dismissed"] is True
    assert rows["tr-q-noise"]["state"] == "queued"
    assert rows["tr-q-noise"]["state_label"] == "Dismissed as noise"
    assert rows["tr-q"]["state_label"] == "Waiting for triage"
    assert rows["tr-exp"]["state_label"] == "Expired"
    assert {queued, queued_noise, expired} <= {row["id"] for row in narrowed["rows"]}

    never = triage_payload(now=NOW, day=DAY, source=src, state="not_picked_up")
    assert never["matched"] == 2
    assert {row["finding_id"] for row in never["rows"]} == {"tr-loose", "tr-loose-noise"}
    noise = triage_payload(now=NOW, day=DAY, source=src, state="dismissed")
    assert {row["finding_id"] for row in noise["rows"]} == {"tr-loose-noise", "tr-q-noise"}
    unknown = triage_payload(now=NOW, day=DAY, source="Unknown")
    assert "tr-blank" in {row["finding_id"] for row in unknown["rows"]}
    assert unknown["matched"] == counts["source"]["Unknown"]
