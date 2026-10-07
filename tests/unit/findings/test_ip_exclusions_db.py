"""Analyst IP exclusions against Postgres: the store, the findings filter, the
HTTP surface, and that ingest -- LogLM included -- ignores exclusions entirely.

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.findings import exclusions as ex
from core.storage.models import Finding, IpExclusion
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

SCANNER = "203.0.113.9"
HOST = "192.0.2.45"


@pytest.fixture(autouse=True)
def _clean():
    ex.invalidate_cache()
    with unit_of_work() as session:
        session.query(IpExclusion).delete()
        session.query(Finding).filter(Finding.finding_id.like("ipx-%")).delete(
            synchronize_session=False
        )
    yield
    ex.invalidate_cache()


def _seed(**findings):
    """``fid=entity_context`` pairs, stored with a fixed severity and status."""
    service = DatabaseService()
    for fid, context in findings.items():
        service.create_finding(
            finding_id=fid,
            mitre_predictions={},
            data_source="loglm",
            entity_context=context,
            severity="high",
            status="new",
            timestamp=utcnow(),
            anomaly_score=0.9,
        )


def _ids(view):
    return sorted(
        f.finding_id
        for f in DatabaseService().get_findings(exclusions=view)
        if f.finding_id.startswith("ipx-")
    )


def _exclude(ip=SCANNER, reason="known scanner", **kw):
    with unit_of_work() as session:
        return ex.create_exclusion(
            session, ip=ip, reason=reason, created_by="analyst-1", **kw
        )


def _standard_findings():
    _seed(
        **{
            "ipx-scalar": {"src_ip": SCANNER, "hostnames": ["web-1"]},
            "ipx-list": {"src_ips": [HOST], "dest_ips": [SCANNER]},
            "ipx-other": {"src_ips": ["192.0.2.46"]},
            "ipx-nocontext": None,
            "ipx-evidence-only": {
                "src_ip": HOST,
                "source_evidence": {"records": [{"dst_ip": SCANNER}]},
            },
        }
    )


# --- the findings filter ------------------------------------------------------


def test_hide_only_and_include_partition_the_queue():
    _standard_findings()
    _exclude()
    everything = [
        "ipx-evidence-only",
        "ipx-list",
        "ipx-nocontext",
        "ipx-other",
        "ipx-scalar",
    ]
    assert _ids("include") == everything
    assert _ids("hide") == ["ipx-evidence-only", "ipx-nocontext", "ipx-other"]
    assert _ids("only") == ["ipx-list", "ipx-scalar"]

    service = DatabaseService()
    assert service.count_findings(exclusions="hide") + service.count_findings(
        exclusions="only"
    ) == service.count_findings(exclusions="include")


def test_zero_active_exclusions_match_include_and_hide_nothing():
    """The common case: no active row, so hide is include, only is empty, and
    the exclusion list does not scan findings for a total."""
    _standard_findings()
    assert _ids("hide") == _ids("include")
    service = DatabaseService()
    assert service.count_findings(exclusions="hide") == service.count_findings(
        exclusions="include"
    )
    assert _ids("only") == []
    assert service.count_findings(exclusions="only") == 0
    with unit_of_work() as session:
        rows, total = ex.list_exclusions_with_total(session)
    assert rows == []
    assert total == 0


def test_the_default_view_is_unchanged_for_existing_callers():
    _standard_findings()
    _exclude()
    assert len(_ids("include")) == 5
    assert sorted(
        f.finding_id
        for f in DatabaseService().get_findings()
        if f.finding_id.startswith("ipx-")
    ) == _ids("include")


def test_removal_restores_findings_unchanged():
    _standard_findings()
    row = _exclude()
    before = {
        f.finding_id: (f.severity, f.status) for f in DatabaseService().get_findings()
    }
    with unit_of_work() as session:
        ex.remove_exclusion(session, row["exclusion_id"], removed_by="analyst-2")
    assert _ids("hide") == _ids("include")
    after = {
        f.finding_id: (f.severity, f.status) for f in DatabaseService().get_findings()
    }
    assert after == before


# --- the store ----------------------------------------------------------------


def test_create_normalizes_and_records_who_and_why():
    row = _exclude(ip=" 2001:DB8::0:1 ", origin="finding", origin_ref="ipx-list")
    assert row["ip"] == "2001:db8::1"
    assert row["created_by"] == "analyst-1"
    assert row["reason"] == "known scanner"
    assert (row["origin"], row["origin_ref"]) == ("finding", "ipx-list")
    assert row["active"] is True


def test_one_active_exclusion_per_address():
    _exclude()
    with pytest.raises(ex.ExclusionConflict):
        _exclude(ip=f" {SCANNER}")


@pytest.mark.parametrize(
    "ip, reason",
    [("10.0.0.0/8", "range"), ("nope", "bad"), (SCANNER, "  "), (SCANNER, None)],
)
def test_create_refuses_ranges_junk_and_missing_reasons(ip, reason):
    with pytest.raises(ex.ExclusionError):
        _exclude(ip=ip, reason=reason)


def test_removal_is_recorded_and_the_address_can_be_excluded_again():
    first = _exclude()
    with unit_of_work() as session:
        removed = ex.remove_exclusion(
            session,
            first["exclusion_id"],
            removed_by="analyst-2",
            reason="blocked upstream",
        )
        again = ex.remove_exclusion(session, first["exclusion_id"], removed_by="x")
    assert removed["active"] is False
    assert removed["removed_by"] == "analyst-2"
    assert again["removed_by"] == "analyst-2"  # the first removal's record stands

    second = _exclude(reason="back again")
    with unit_of_work() as session:
        history = ex.list_exclusions(session, include_removed=True)
        current = ex.list_exclusions(session)
    assert [r["exclusion_id"] for r in history] == [
        second["exclusion_id"],
        first["exclusion_id"],
    ]
    assert [r["exclusion_id"] for r in current] == [second["exclusion_id"]]


def test_list_counts_the_findings_each_exclusion_hides():
    _standard_findings()
    _exclude()
    _exclude(ip="198.51.100.1", reason="unused")
    _exclude(ip=HOST, reason="overlaps the scanner on ipx-list")
    with unit_of_work() as session:
        counts = {r["ip"]: r["hidden_findings"] for r in ex.list_exclusions(session)}
        total = ex.hidden_findings_total(session)
    # ipx-list names both the scanner and HOST, so the rows overlap by one.
    assert counts == {SCANNER: 2, "198.51.100.1": 0, HOST: 2}
    assert total == 3


def test_the_cache_sees_a_write_once_it_commits():
    assert ex.cached_active_ips() == frozenset()
    row = _exclude()
    assert ex.cached_active_ips() == {SCANNER}
    with unit_of_work() as session:
        ex.remove_exclusion(session, row["exclusion_id"], removed_by="analyst-2")
    assert ex.cached_active_ips() == frozenset()


# --- ingest keeps processing excluded addresses ---------------------------------


def test_loglm_ingest_still_stores_findings_for_an_excluded_address():
    from core.ingestion.ingestion_service import IngestionService

    _exclude()
    service = IngestionService()
    finding = service._parquet_row_to_finding(
        {
            "sequence_id": "ipx-loglm-seq",
            "event_start_time": 1753000000000,
            "focal_ip": SCANNER,
            "engaged_ip": HOST,
            "confidence_score": 0.97,
            "mitre_pred": 10,
        },
        data_source="loglm",
    )
    finding["finding_id"] = "ipx-loglm-1"
    assert service.ingest_finding(finding) is True

    stored = DatabaseService().get_finding("ipx-loglm-1")
    assert stored is not None
    assert stored.anomaly_score == pytest.approx(0.97)
    assert "ipx-loglm-1" in _ids("only")
    assert "ipx-loglm-1" not in _ids("hide")


# --- HTTP ----------------------------------------------------------------------


@pytest.fixture
def client(authenticate_app):
    from core.api.v1.findings_router import router as findings_router
    from core.findings.exclusions_router import router as exclusions_router

    app = FastAPI()
    app.include_router(exclusions_router, prefix="/api/exclusions")
    app.include_router(findings_router, prefix="/api/findings")
    authenticate_app(app)
    return TestClient(app)


def test_api_round_trip(client):
    _standard_findings()

    made = client.post(
        "/api/exclusions",
        json={
            "ip": SCANNER,
            "reason": "known scanner",
            "origin": "finding",
            "origin_ref": "ipx-scalar",
        },
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["created_by"] == "test-admin"

    assert (
        client.post("/api/exclusions", json={"ip": SCANNER, "reason": "x"}).status_code
        == 409
    )
    assert (
        client.post(
            "/api/exclusions", json={"ip": "10.0.0.0/24", "reason": "x"}
        ).status_code
        == 400
    )
    assert client.post("/api/exclusions", json={"ip": HOST}).status_code == 422

    listed = client.get("/api/exclusions").json()
    assert listed["total"] == 1
    assert listed["exclusions"][0]["hidden_findings"] == 2
    assert listed["hidden_findings_total"] == 2

    hidden = client.get(
        "/api/findings", params={"exclusions": "hide", "limit": 1000}
    ).json()
    hidden_ids = {f["finding_id"] for f in hidden["findings"]}
    assert "ipx-scalar" not in hidden_ids and "ipx-other" in hidden_ids

    only = client.get(
        "/api/findings", params={"exclusions": "only", "limit": 1000}
    ).json()
    marked = {f["finding_id"]: f["excluded_ips"] for f in only["findings"]}
    assert marked == {"ipx-scalar": [SCANNER], "ipx-list": [SCANNER]}

    one = client.get("/api/findings/ipx-list").json()
    assert one["excluded_ips"] == [SCANNER]

    summary_all = client.get("/api/findings/stats/summary").json()
    summary_hidden = client.get(
        "/api/findings/stats/summary", params={"exclusions": "hide"}
    ).json()
    assert summary_all["total"] - summary_hidden["total"] == 2

    assert (
        client.get("/api/findings", params={"exclusions": "bogus"}).status_code == 422
    )

    removed = client.post(
        f"/api/exclusions/{body['exclusion_id']}/remove", json={"reason": "done"}
    )
    assert removed.status_code == 200
    assert removed.json()["active"] is False
    assert client.post("/api/exclusions/excl-missing/remove").status_code == 404
    restored = client.get(
        "/api/findings", params={"exclusions": "hide", "limit": 1000}
    ).json()
    assert "ipx-scalar" in {f["finding_id"] for f in restored["findings"]}


def test_writes_need_findings_write(client):
    """Exclusions are deployment-wide (Vigil has no tenants), so writing one is
    gated on the role that may change what every analyst's queue shows."""

    def allow(_user_id, permission, *_a, **_kw):
        return permission == "findings.read"

    with patch(
        "core.auth.auth_service.AuthService.check_permission", side_effect=allow
    ):
        assert client.get("/api/exclusions").status_code == 200
        denied = client.post("/api/exclusions", json={"ip": SCANNER, "reason": "x"})
        assert denied.status_code == 403
        assert client.post("/api/exclusions/excl-x/remove").status_code == 403
    with unit_of_work() as session:
        assert ex.list_exclusions(session) == []


def test_attack_tab_and_timeline_describe_the_queue():
    """The ATT&CK rollup, its drill-down and the dashboard timeline hide what
    the queue hides, so the Dashboard's tabs agree with its KPIs."""
    from core.storage.models import FindingMitrePrediction
    from core.threat_intel import attack_router

    _standard_findings()
    with unit_of_work() as session:
        for fid in ("ipx-scalar", "ipx-other"):
            session.add(
                FindingMitrePrediction(
                    finding_id=fid, technique_id="T1046", confidence=0.9
                )
            )
    service = DatabaseService()
    before = {r[0]: r[2] for r in service.get_technique_severity_counts()}
    assert before["T1046"] == 2

    _exclude()
    hidden = {
        r[0]: r[2] for r in service.get_technique_severity_counts(exclusions="hide")
    }
    assert hidden["T1046"] == 1
    drill = attack_router.get_findings_by_technique("T1046")
    assert [f["finding_id"] for f in drill["findings"]] == ["ipx-other"]


def test_dashboard_timeline_hides_excluded_findings():
    from services.api.routers.timeline import router as timeline_router

    app = FastAPI()
    app.include_router(timeline_router, prefix="/api/timeline")
    _standard_findings()
    _exclude()
    response = TestClient(app).get("/api/timeline/range", params={"limit": 5000})
    assert response.status_code == 200, response.text
    ids = {e.get("metadata", {}).get("finding_id") for e in response.json()["events"]}
    assert "ipx-other" in ids
    assert "ipx-scalar" not in ids and "ipx-list" not in ids
