"""Unit tests for core.cases.sandbox_correlation_service normalisation helpers.

Most tests cover the pure normalisation helpers. ``TestAttachReportAtomicity``
covers the DB-writing path's transaction boundary with a mock session.
``TestAttachReportIdempotency`` runs the real inserts against in-memory SQLite.
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import ARRAY, JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import core.storage.unit_of_work as uow_module
from core.storage.models import CaseEvidence, CaseIOC
from core.cases.sandbox_correlation_service import (
    SandboxCorrelationService,
    _cape_verdict,
    _iter_iocs,
    _normalise_cape,
    _normalise_report,
    _score_to_confidence,
    _score_to_threat_level,
)


@pytest.mark.unit
class TestCapeNormalisation:
    def test_extracts_primary_hashes_and_score(self):
        report = {
            "data": {
                "target": {"file": {"md5": "a" * 32, "sha256": "b" * 64}},
                "info": {"score": 8.5},
                "network": {},
                "signatures": [],
            }
        }
        out = _normalise_cape(report)
        assert out["md5"] == "a" * 32
        assert out["sha256"] == "b" * 64
        assert out["score"] == 8.5
        assert out["verdict"] == "malicious"

    def test_extracts_network_and_dropped_iocs(self):
        report = {
            "data": {
                "target": {"file": {}},
                "info": {"score": 3},
                "network": {
                    "hosts": [{"ip": "1.2.3.4"}, "5.6.7.8"],
                    "dns": [{"request": "evil.example"}],
                    "http": [{"uri": "http://evil.example/a"}],
                },
                "dropped": [{"sha256": "c" * 64}],
                "behavior": {"summary": {"mutexes": ["Global\\Badness"]}},
                "signatures": [{"ttp": ["T1059.001"]}, {"ttp": ["T1055"]}],
            }
        }
        out = _normalise_cape(report)
        assert "1.2.3.4" in out["iocs"]["ip"]
        assert "5.6.7.8" in out["iocs"]["ip"]
        assert "evil.example" in out["iocs"]["domain"]
        assert "http://evil.example/a" in out["iocs"]["url"]
        assert "c" * 64 in out["iocs"]["hash"]
        assert "Global\\Badness" in out["iocs"]["mutex"]
        assert "T1059.001" in out["mitre_techniques"]
        assert "T1055" in out["mitre_techniques"]


@pytest.mark.unit
class TestVerdictAndScoreMapping:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (None, None),
            (0, "clean"),
            (2, "benign"),
            (5, "suspicious"),
            (8, "malicious"),
            (10, "malicious"),
        ],
    )
    def test_cape_verdict(self, score, expected):
        assert _cape_verdict(score) == expected

    def test_score_to_threat_level(self):
        assert _score_to_threat_level(None) is None
        assert _score_to_threat_level(1) == "low"
        assert _score_to_threat_level(5) == "medium"
        assert _score_to_threat_level(7) == "high"
        assert _score_to_threat_level(9) == "critical"

    def test_score_to_confidence_clamps(self):
        assert _score_to_confidence(None) is None
        assert _score_to_confidence(0) == 0.0
        assert _score_to_confidence(5) == 0.5
        assert _score_to_confidence(100) == 1.0


@pytest.mark.unit
class TestIocIteration:
    def test_dedupes_across_same_type(self):
        iocs = {"ip": ["1.1.1.1", "1.1.1.1", "2.2.2.2"], "domain": ["ex.com"]}
        pairs = list(_iter_iocs(iocs))
        assert ("ip", "1.1.1.1") in pairs
        assert ("ip", "2.2.2.2") in pairs
        assert ("domain", "ex.com") in pairs
        assert len(pairs) == 3

    def test_skips_empty_values(self):
        pairs = list(_iter_iocs({"ip": ["", None]}))
        assert pairs == []


@pytest.mark.unit
class TestDispatch:
    def test_unknown_sandbox_returns_empty(self):
        out = _normalise_report("made-up", {"anything": True})
        assert out["iocs"] == {}
        assert out["verdict"] is None

    def test_cape_routing(self):
        report = {
            "data": {
                "target": {"file": {"md5": "d" * 32}},
                "info": {"score": 9},
                "network": {},
                "signatures": [],
            }
        }
        out = _normalise_report("cape-sandbox", report)
        assert out["verdict"] == "malicious"


@pytest.mark.unit
class TestAttachReportAtomicity:
    """attach_report writes one evidence row plus N IOC rows, all or nothing."""

    REPORT = {
        "data": {
            "target": {"file": {"md5": "a" * 32, "sha256": "b" * 64}},
            "info": {"score": 9},
            "network": {"hosts": [{"ip": "1.2.3.4"}, "5.6.7.8"]},
            "signatures": [],
        }
    }

    @pytest.fixture
    def session(self, monkeypatch):
        mock_session = MagicMock()
        # No pre-existing IOC rows, so each one takes the insert path.
        mock_session.query.return_value.filter.return_value.first.return_value = None
        monkeypatch.setattr(uow_module, "get_db_session", lambda: mock_session)
        return mock_session

    def _attach(self):
        return SandboxCorrelationService().attach_report(
            case_id="case-1",
            sandbox_name="cape",
            task_id="task-1",
            report=self.REPORT,
        )

    def test_commits_once_when_every_step_succeeds(self, session):
        result = self._attach()

        assert result["iocs_added"] == 2
        session.commit.assert_called_once()
        session.rollback.assert_not_called()

    def test_failure_partway_rolls_back_the_evidence_row_too(
        self, session, monkeypatch
    ):
        # Let the first IOC through, then blow up on the second.
        calls = {"n": 0}

        def _flaky_upsert(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("constraint violation on the second IOC")
            return True

        monkeypatch.setattr(SandboxCorrelationService, "_upsert_ioc", _flaky_upsert)

        with pytest.raises(RuntimeError):
            self._attach()

        # The evidence row was added before the failure, but nothing commits.
        session.add.assert_called_once()
        session.commit.assert_not_called()
        session.rollback.assert_called_once()
        session.close.assert_called_once()


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.mark.unit
class TestAttachReportIdempotency:
    REPORT = TestAttachReportAtomicity.REPORT

    @pytest.fixture
    def db(self, monkeypatch):
        for table in (CaseEvidence.__table__, CaseIOC.__table__):
            for col in table.c:
                if isinstance(col.type, ARRAY):
                    monkeypatch.setattr(col, "type", JSON())
        engine = create_engine("sqlite://")
        CaseEvidence.__table__.create(engine)
        CaseIOC.__table__.create(engine)
        Session = sessionmaker(bind=engine)
        monkeypatch.setattr(uow_module, "get_db_session", lambda: Session())
        return Session

    def _evidence(self, db):
        return db().query(CaseEvidence).order_by(CaseEvidence.evidence_id).all()

    def test_same_task_twice_adds_one_evidence_row(self, db):
        svc = SandboxCorrelationService()
        first = svc.attach_report("case-1", "cape", "t1", self.REPORT)
        second = svc.attach_report("case-1", "cape", "t1", self.REPORT)

        assert [e.evidence_id for e in self._evidence(db)] == [first["evidence_id"]]
        assert second["evidence_id"] == first["evidence_id"]
        assert first["iocs_added"] == 2 and second["iocs_added"] == 0

    def test_other_task_sandbox_or_case_adds_its_own_row(self, db):
        svc = SandboxCorrelationService()
        svc.attach_report("case-1", "cape", "t1", self.REPORT)
        svc.attach_report("case-1", "cape", "t2", self.REPORT)
        svc.attach_report("case-1", "joe", "t1", {})
        svc.attach_report("case-2", "cape", "t1", self.REPORT)

        keys = [
            (e.case_id, e.source, e.analysis_results["task_id"])
            for e in self._evidence(db)
        ]
        assert keys == [
            ("case-1", "cape", "t1"),
            ("case-1", "cape", "t2"),
            ("case-1", "joe", "t1"),
            ("case-2", "cape", "t1"),
        ]


@pytest.mark.unit
class TestVendorScoreScale:
    REPORTS = {
        "cape": {"data": {"info": {"score": 1}, "network": {"hosts": ["9.9.9.9"]}}},
        "hybrid_analysis": {"threat_score": 10, "hosts": ["9.9.9.9"]},
    }

    @pytest.mark.parametrize("sandbox", list(REPORTS))
    def test_low_vendor_score_yields_low_ioc(self, sandbox, monkeypatch):
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        monkeypatch.setattr(uow_module, "get_db_session", lambda: session)

        result = SandboxCorrelationService().attach_report(
            "case-1", sandbox, "task-1", self.REPORTS[sandbox]
        )

        assert "error" not in result
        (ioc,) = [
            c.args[0]
            for c in session.add.call_args_list
            if isinstance(c.args[0], CaseIOC)
        ]
        assert (ioc.threat_level, ioc.confidence, ioc.reputation_score) == (
            "low",
            0.1,
            1.0,
        )

    @pytest.mark.parametrize("threat_score", [None, "n/a"])
    def test_unusable_hybrid_score_is_none(self, threat_score):
        assert (
            _normalise_report("hybrid_analysis", {"threat_score": threat_score})[
                "score"
            ]
            is None
        )
