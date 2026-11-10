"""A rule saved with named attack events is proven on the SIEM's own events, and
a shadow copy never shown to fire is not promoted (#4212).

The repro is the froth.ly C2 rule from BOTSv3: an agent wrote its own events with
``c-useragent`` where Splunk's ``stream:http`` holds ``http_user_agent``, so a
rule on that name matched them, saved, and would have gone live matching nothing.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

from core.detections import overlay
from core.detections import save_proof as sp
from core.detections.engineering import siem_replay as sr
from core.detections.inventory import rollout

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
AT = NOW - timedelta(hours=3)
BKT = "botsv3~43841~B3BA533E"


def _row(cd, agent, port, path, at=AT):
    """A stream:http event as Splunk returns it, shaped by rollout._splunk_doc."""
    return {
        "_id": f"{BKT}:{cd}",
        "_index": "botsv3",
        "_source": {
            "_bkt": BKT,
            "_cd": cd,
            "_time": at.isoformat(),
            "sourcetype": "stream:http",
            "http_user_agent": agent,
            "dest_port": str(port),
            "uri_path": path,
        },
    }


PS = "Mozilla/5.0 (Windows NT; Windows NT 10.0; en-US) WindowsPowerShell/5.1.17134.112"
C2 = _row("43841:1881433", PS, 3333, "/images/logos.png")
BROWSER = [
    _row(f"43841:9{i}", "Mozilla/5.0 (Windows NT 10.0) Chrome/68.0", 80, f"/p{i}")
    for i in range(4)
]

SIGMA_INVENTED = """title: PowerShell HTTP C2
logsource: {category: proxy}
detection:
  selection:
    c-useragent: ['*WindowsPowerShell*']
  condition: selection
"""
SIGMA = SIGMA_INVENTED.replace("c-useragent", "http_user_agent")
SPLUNK_INVENTED = {"search": 'sourcetype=stream:http c-useragent="*WindowsPowerShell*"'}
SPLUNK = {"search": 'sourcetype=stream:http http_user_agent="*WindowsPowerShell*"'}


class _Splunk(sr.Source):
    """A Splunk holding ``events``; reads by id as SplunkSource reads them."""

    siem = "splunk"

    def __init__(self, events):
        super().__init__(service=None, index="*", per_hour=30)
        self.events = list(events)

    def spend(self, what):
        return True

    async def attack(self, claim, ids, fields, start, end):
        docs = [d for d in self.events if any(sp._same(i, d["_id"]) for i in ids)]
        return sr.Read(total=len(docs), docs=docs)

    async def benign(self, fields, start, end):
        return sr.Read(total=len(self.events), docs=self.events)


@pytest.fixture
def siem(monkeypatch):
    """The SIEM the proof reads, and what a copy's run returns: the events its
    search would match, by the field names it uses."""
    state = {"events": [C2, *BROWSER], "runs": []}

    def sources(*_a, **_k):
        return [_Splunk(state["events"])], []

    async def run_copy(language, rule, start, end):
        state["runs"].append((language, start, end))
        field = "http_user_agent" if "http_user_agent" in rule["search"] else None
        docs = [
            d
            for d in state["events"]
            if field and "WindowsPowerShell" in d["_source"].get(field, "")
        ]
        return len(docs), docs, None

    monkeypatch.setattr(sr, "configured_sources", sources)
    monkeypatch.setattr(sr, "ledger_claims", lambda days: [])
    monkeypatch.setattr(sp, "_run_copy", run_copy)
    return state


def _prove(sigma, splunk, ids=("43841:1881433",)):
    named = {"splunk": list(ids)}
    start, end = NOW - timedelta(days=30), NOW
    return asyncio.run(
        sp._prove(
            [("sigma", sigma, _Rec("sig")), ("splunk", "", _Rec("spl"))],
            {"spl": splunk},
            sigma,
            named,
            start,
            end,
            (),
        )
    )


class _Rec:
    def __init__(self, key):
        self.native_key = key


# --- the save ----------------------------------------------------------------------


def test_the_repro_is_refused_its_sigma_matches_none_of_the_real_events(siem):
    with pytest.raises(sp.Refused) as refused:
        _prove(SIGMA_INVENTED, SPLUNK_INVENTED)
    assert "matched none of the 1 named events" in str(refused.value)
    assert refused.value.detail["replay"]["attack_rows"] == 1


def test_a_right_sigma_with_a_wrong_splunk_copy_is_refused_on_the_copy(siem):
    with pytest.raises(sp.Refused) as refused:
        _prove(SIGMA, SPLUNK_INVENTED)
    assert str(refused.value).startswith("splunk copy returned 0 event(s)")
    assert refused.value.detail["replay"]["result"] == "fail"


def test_the_corrected_rule_proves_each_copy_on_the_siems_events(siem):
    out = _prove(SIGMA, SPLUNK)
    assert out["sig"]["result"] == "pass" and out["sig"]["source"] == "siem"
    assert out["sig"]["hits"] == 1 and out["sig"]["false_positives"] == 0
    assert out["spl"]["result"] == "pass" and out["spl"]["source"] == "siem"
    assert out["spl"]["matched"] == ["43841:1881433"]
    # The copy runs over the hits' own window, not the 30 days the ids were read in.
    _, start, end = siem["runs"][0]
    assert (start, end) == (AT - sp.PAD, AT + sp.PAD)


def test_an_id_the_siem_does_not_hold_proves_nothing(siem):
    with pytest.raises(sp.Refused) as refused:
        _prove(SIGMA, SPLUNK, ids=("invented:1",))
    assert "none of the 1 named events could be read" in str(refused.value)


def test_a_rule_matching_events_no_case_claims_is_refused(siem):
    broad = SIGMA.replace("'*WindowsPowerShell*'", "'*Windows*'")
    with pytest.raises(sp.Refused) as refused:
        _prove(broad, SPLUNK)
    assert "matched 4 event(s) in the window that no Case or hunt claims" in str(
        refused.value
    )


def test_an_exception_keeps_expected_activity_out_of_the_false_positives(siem):
    broad = SIGMA.replace("'*WindowsPowerShell*'", "'*Windows*'")
    out = asyncio.run(
        sp._prove(
            [("sigma", broad, _Rec("sig"))],
            {},
            broad,
            {"splunk": ["43841:1881433"]},
            NOW - timedelta(days=30),
            NOW,
            [{"dest_port": "80"}],
        )
    )
    assert out["sig"]["result"] == "pass"
    assert out["sig"]["siems"][0]["excepted_left_out"] == 4


def test_a_transforming_search_saves_unverified(siem, monkeypatch):
    async def stats(language, rule, start, end):
        return 1, [{"_id": "digest", "_source": {"count": "3"}}], None

    monkeypatch.setattr(sp, "_run_copy", stats)
    out = _prove(SIGMA, {"search": "... | stats count"})
    assert out["spl"]["result"] == "unverifiable"
    assert "transforming search" in out["spl"]["reason"]


def test_a_copy_no_event_was_named_for_is_not_run(siem):
    out = asyncio.run(
        sp._prove(
            [("sigma", SIGMA, _Rec("sig")), ("elastic", "", _Rec("el"))],
            {"el": {"query": "x"}},
            SIGMA,
            {"splunk": ["43841:1881433"]},
            NOW - timedelta(days=30),
            NOW,
            (),
        )
    )
    assert out["el"]["result"] == "not_run"


@pytest.mark.parametrize(
    "given, says",
    [
        (["x"], "must be an object keyed by SIEM"),
        ({"qradar": ["x"]}, "names 'qradar'"),
        ({"splunk": "x"}, "must be a list of event ids"),
        ({"splunk": []}, "names no event"),
    ],
)
def test_malformed_ids_are_refused(given, says):
    with pytest.raises(sp.Refused, match=says):
        sp.named_ids(given)


def test_the_window_defaults_to_the_last_30_days_and_is_bounded():
    assert sp.window(None, None, now=NOW) == (NOW - timedelta(days=30), NOW)
    start, end = sp.window("2018-08-20T10:00:00Z", "2018-08-20T12:00:00Z")
    assert (start.year, end.hour) == (2018, 12)
    with pytest.raises(sp.Refused, match="before"):
        sp.window("2018-08-21T00:00:00Z", "2018-08-20T00:00:00Z")
    with pytest.raises(sp.Refused, match="365 days"):
        sp.window("2016-01-01T00:00:00Z", "2018-01-01T00:00:00Z")


# --- shadow to live ----------------------------------------------------------------


def _copy(language="splunk", hits=0, **checks):
    week = rollout._week(NOW)
    return rollout.Copy(
        rule_id=1,
        version=1,
        name="PowerShell HTTP C2",
        language=language,
        stage="shadow",
        stage_at=NOW - timedelta(days=8),
        checks={
            "runs": {"days": {d: {"pass": 4, "fail": 0} for d in week}},
            "shadow": {"days": {d: hits for d in week}},
            "sees": {"result": "pass"},
            "lint": {"result": "pass", "findings": []},
            **checks,
        },
        rule={},
    )


def test_a_quiet_week_alone_no_longer_promotes():
    assert rollout._due(None, _copy(), NOW, 0) is None


def test_a_replay_on_the_callers_own_events_is_not_proof():
    caller = {"result": "pass", "matched": True}
    assert rollout._due(None, _copy(replay=caller), NOW, 0) is None


def test_a_save_time_siem_proof_promotes_and_says_so():
    stage, reason, proof = rollout._due(
        None, _copy(replay={"result": "pass", "source": "siem"}), NOW, 0
    )
    assert stage == "live"
    assert reason.endswith("and it caught the named attack events in its SIEM at save")
    assert proof["fired"] == "caught the named attack events in its SIEM at save"


def test_an_unverifiable_copy_waits_in_shadow():
    waiting = {"result": "unverifiable", "source": "siem"}
    assert rollout._due(None, _copy(replay=waiting), NOW, 0) is None


def test_a_version_caught_alerting_on_a_proven_attack_promotes():
    assert rollout._due(None, _copy(caught={"result": "pass"}), NOW, 0)[0] == "live"


def test_a_wazuh_copy_is_shown_to_fire_by_its_own_archive_hits_alone():
    assert rollout._due(None, _copy("wazuh", hits=0), NOW, 0) is None
    assert rollout._due(None, _copy("wazuh", hits=1), NOW, 0)[0] == "live"
    # Archive hits are not proof for a copy that can be run at save.
    assert rollout._due(None, _copy("splunk", hits=1), NOW, 0) is None


# --- through save_rule, on real Postgres (VIGIL_TEST_PG_DSN) ----------------------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
SPLUNK_YAML = """name: PowerShell HTTP C2
search: sourcetype=stream:http http_user_agent="*WindowsPowerShell*"
tags:
  mitre_attack_id: [T1071.001]
"""
IDS = {"splunk": ["43841:1881433"]}


@pytest.fixture
def db(monkeypatch, tmp_path):
    if not DSN:
        pytest.skip("VIGIL_TEST_PG_DSN not set")
    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
        "VIGIL_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        session.execute(text("SET LOCAL lock_timeout = '10s'"))
        session.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


def _replays(db):
    with db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT r.language, v.checks->'replay' FROM detection_rules r "
                "JOIN detection_rule_versions v ON v.rule_id = r.id "
                "AND v.version = r.latest_version"
            )
        ).all()
    return dict(rows)


def test_a_save_the_siem_refuses_writes_nothing(db, siem):
    result = overlay.save_rule(
        splunk_yaml=SPLUNK_YAML.replace("http_user_agent", "c-useragent"),
        sigma_yaml=SIGMA,
        proven_event_ids=IDS,
    )
    assert result["saved"] == [] and result["error"].startswith("splunk copy returned")
    assert result["replay"]["result"] == "fail"
    assert _replays(db) == {}


def test_a_proven_save_keeps_each_copys_own_proof_on_its_version(db, siem):
    result = overlay.save_rule(
        splunk_yaml=SPLUNK_YAML, sigma_yaml=SIGMA, proven_event_ids=IDS
    )
    assert not result.get("error"), result
    replays = _replays(db)
    assert replays["splunk"]["result"] == "pass"
    assert replays["splunk"]["matched"] == ["43841:1881433"]
    assert replays["sigma"]["result"] == "pass" and replays["sigma"]["hits"] == 1
    assert {r["source"] for r in replays.values()} == {"siem"}
    stage = {s["source"]: s["stage"] for s in result["saved"]}
    assert stage == {"splunk": "shadow", "sigma": None}


def test_a_save_without_ids_records_no_proof_on_the_splunk_copy(db, siem):
    result = overlay.save_rule(splunk_yaml=SPLUNK_YAML, sigma_yaml=SIGMA)
    assert not result.get("error"), result
    assert _replays(db)["splunk"] is None
