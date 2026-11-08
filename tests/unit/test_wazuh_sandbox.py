"""Vigil's Wazuh sandbox (#4099): a shadow Wazuh copy the archive query can't
reproduce is counted by replaying a sample of the archive through wazuh-logtest
on Vigil's own manager, loaded with the customer's repository rules and decoders
and the overlay's copies. What a sample can't count, or what the sandbox lacks,
is not validated, with its reason."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.detections.inventory import rollout, wazuh_sandbox
from core.detections.inventory.wazuh_sandbox import Sandbox

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _rule(body, rid="100100", attrs=""):
    return (
        f'<group name="vigil,"><rule id="{rid}" level="5"{attrs}>{body}'
        "<description>d</description></rule></group>"
    )


MATCH = _rule("<if_sid>5710</if_sid><match>zzmarker</match>")


# --- what the sandbox can count ----------------------------------------------


@pytest.mark.parametrize(
    "xml, fragment",
    [
        (
            _rule("<match>x</match>", attrs=' frequency="8" timeframe="120"'),
            "frequency",
        ),
        (_rule("<match>x</match>", attrs=' ignore="60"'), "ignore"),
        (_rule("<if_matched_sid>5710</if_matched_sid>"), "<if_matched_sid>"),
        (_rule("<if_sid>5710</if_sid><same_source_ip />"), "<same_source_ip>"),
        (_rule('<list field="user" lookup="match_key">etc/lists/x</list>'), "<list>"),
    ],
    ids=["frequency", "ignore", "if_matched", "same_", "list"],
)
def test_what_a_sample_cannot_count_is_refused(xml, fragment):
    why = wazuh_sandbox.refusal(xml)
    assert why.startswith("can't validate: ") and fragment in why


@pytest.mark.parametrize(
    "body",
    [
        "<if_sid>5710</if_sid><match>zzmarker</match>",
        "<decoded_as>sshd</decoded_as><regex>^Failed \\S+ for</regex>",
        '<pcre2>(?i)invalid user</pcre2><hostname negate="yes">dc01</hostname>',
    ],
    ids=["match", "regex", "pcre2-negate"],
)
def test_a_rule_on_the_raw_line_is_the_sandboxs_to_count(body):
    assert wazuh_sandbox.refusal(_rule(body)) is None


def test_a_rules_parents_are_what_it_names():
    xml = _rule(
        "<if_sid>5710, 5711 5712</if_sid><if_group>authentication_failed</if_group>"
        "<decoded_as>sshd</decoded_as>"
    )
    assert wazuh_sandbox.parents(xml) == {
        "if_sid": ["5710", "5711", "5712"],
        "if_group": ["authentication_failed"],
        "decoded_as": ["sshd"],
    }


# --- what it loads -------------------------------------------------------------


class _Copy:
    def __init__(self, key, toml):
        self.key, self.toml = key, toml


def test_it_loads_the_repositories_rules_and_decoders_and_the_copies(tmp_path):
    repo = tmp_path / "repo"
    (repo / "rules").mkdir(parents=True)
    (repo / "decoders").mkdir()
    (repo / ".git").mkdir()
    (repo / "rules" / "local_rules.xml").write_text(
        '<group name="local,">'
        '<rule id="100001" level="3"><match>a</match><description>a</description></rule>'
        '<rule id="100100" level="3"><match>old</match><description>b</description></rule>'
        "</group>"
    )
    (repo / "decoders" / "local_decoder.xml").write_text(
        '<decoder name="acme"><prematch>^ACME:</prematch></decoder>'
    )
    (repo / ".git" / "ignored.xml").write_text("<rule id='1'/>")
    (repo / "rules" / "proxylogon.xml").write_text(
        '<group name="x,"><rule id="100002" level="3"><regex>\\<a</regex>'
        "<description>raw</description></rule></group>"
    )
    rs = wazuh_sandbox.ruleset([_Copy("100100", MATCH)], [repo])
    assert all(n.startswith("vigil_sandbox_") for n in [*rs.rules, *rs.decoders])
    texts = list(rs.rules.values())
    # The copy replaces the repository's rule with its id; the rest stays.
    assert 'id="100001"' in texts[0] and "old" not in texts[0]
    # A file ElementTree can't read (Wazuh's raw "\<") goes as written.
    assert "\\<a" in texts[1]
    assert texts[-1] == MATCH
    assert list(rs.decoders.values()) == [
        '<decoder name="acme"><prematch>^ACME:</prematch></decoder>'
    ]
    assert not any("ignored" in t for t in texts)


# --- the sandbox's API ----------------------------------------------------------


def _sandbox(handler):
    box = Sandbox("https://wazuh-sandbox:55000", "wazuh", "wazuh", verify_ssl=False)
    box.pace = 0
    box._client = httpx.AsyncClient(
        base_url=box.api_url, transport=httpx.MockTransport(handler)
    )
    return box


def _ok(data):
    return httpx.Response(200, json={"data": data, "error": 0})


@pytest.mark.asyncio
async def test_load_uploads_its_files_and_drops_its_stale_ones():
    seen = []

    def handler(request):
        seen.append(
            (request.method, request.url.path, request.headers.get("content-type"))
        )
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        if request.method == "GET":
            kind = request.url.path.split("/")[1]
            return _ok(
                {
                    "affected_items": (
                        [{"filename": f"vigil_sandbox_{n}.xml"} for n in (0, 9)]
                        + [{"filename": "local_rules.xml"}]
                        if kind == "rules"
                        else []
                    )
                }
            )
        return _ok({})

    rs = wazuh_sandbox.Ruleset(rules={"vigil_sandbox_0.xml": MATCH})
    await _sandbox(handler).load(rs)
    assert (
        "PUT",
        "/rules/files/vigil_sandbox_0.xml",
        "application/octet-stream",
    ) in seen
    assert ("DELETE", "/rules/files/vigil_sandbox_9.xml", None) in seen
    # Only its own files: the sandbox image's are never touched.
    assert not any("local_rules" in path for m, path, _ in seen if m == "DELETE")


@pytest.mark.asyncio
async def test_replay_uses_one_session_and_closes_it():
    calls = []

    def handler(request):
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        if request.method == "PUT":
            body = json.loads(request.content)
            calls.append(body.get("token"))
            return _ok({"token": "s1", "output": {"rule": {"id": "100100"}}})
        calls.append(("DELETE", request.url.path))
        return _ok({})

    out = await _sandbox(handler).replay(
        [{"event": "a", "log_format": "syslog", "location": "x"}] * 2
    )
    assert [o["rule"]["id"] for o in out] == ["100100", "100100"]
    assert calls == [None, "s1", ("DELETE", "/logtest/sessions/s1")]


@pytest.mark.asyncio
async def test_missing_names_the_parents_the_sandbox_lacks():
    def handler(request):
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        q = request.url.params
        if request.url.path == "/rules" and q.get("rule_ids"):
            return _ok({"affected_items": [{"id": 5710}], "total_affected_items": 1})
        if request.url.path == "/rules":
            return _ok({"affected_items": [], "total_affected_items": 0})
        return _ok({"affected_items": [{}], "total_affected_items": 1})

    gone = await _sandbox(handler).missing(
        {"if_sid": ["5710", "123456"], "if_group": ["acme"], "decoded_as": ["sshd"]}
    )
    assert gone == ["if_sid 123456", "if_group acme"]


# --- counting --------------------------------------------------------------------


def _doc(line, decoder="sshd", at=NOW - timedelta(hours=1)):
    return {
        "_id": line,
        "_source": {
            "full_log": line,
            "location": "/var/log/auth.log",
            "decoder": {"name": decoder},
            "@timestamp": at.isoformat(),
        },
    }


def test_an_event_is_replayed_as_logged():
    assert wazuh_sandbox.event(_doc("x")) == {
        "event": "x",
        "log_format": "syslog",
        "location": "/var/log/auth.log",
    }
    assert wazuh_sandbox.event(_doc("{}", decoder="json"))["log_format"] == "json"
    assert wazuh_sandbox.event({"_source": {"decoder": {"name": "windows"}}}) is None


def test_a_hit_is_an_event_whose_final_rule_is_the_copy():
    docs = [
        _doc("a"),
        _doc("b"),
        _doc("c", decoder="acme"),  # the customer's manager decoded it otherwise
        _doc("d", at=NOW - timedelta(days=2)),  # before this copy's window
        _doc("e"),
    ]
    outputs = [
        {"rule": {"id": "100100"}, "decoder": {"name": "sshd"}},
        {"rule": {"id": "5710"}, "decoder": {"name": "sshd"}},
        {"rule": {"id": "100100"}, "decoder": {"name": "sshd"}},
        {"rule": {"id": "100100"}, "decoder": {"name": "sshd"}},
        None,  # logtest refused it
    ]
    tally = wazuh_sandbox.count(
        docs, outputs, {"100100": NOW - timedelta(hours=6)}, window_events=1200
    )
    assert [d["_id"] for d in tally.hits["100100"]] == ["a"]
    assert tally.sampled() == {
        "engine": wazuh_sandbox.ENGINE,
        "events": 3,
        "of": 1200,
        "decoded_differently": 1,
        "refused": 1,
    }


def test_a_run_waits_for_six_hours_of_window_unless_a_copy_never_ran():
    start = NOW - timedelta(hours=2)
    assert not wazuh_sandbox.due([(start, NOW, False)])
    assert wazuh_sandbox.due([(start, NOW, True)])
    assert wazuh_sandbox.due([(NOW - timedelta(hours=7), NOW, False)])
    assert not wazuh_sandbox.due([])


def test_a_sampled_run_records_its_sample_and_a_plain_one_clears_it():
    sampled = {
        "engine": "e",
        "events": 3,
        "of": 9,
        "decoded_differently": 0,
        "refused": 0,
    }
    checks = rollout._ran({}, "shadow", NOW, (2, [], None), NOW, sampled=sampled)
    assert checks["runs"]["sampled"] == sampled and checks["shadow"]["days"]
    again = rollout._ran(checks, "shadow", NOW, (0, [], None), NOW)
    assert "sampled" not in again["runs"]


# --- the rollout ------------------------------------------------------------------


class _Service:
    def __init__(self, docs=(), events=True):
        self.docs, self.events = list(docs), events
        self.searched, self.closed = [], False

    async def indices_matching(self, index):
        return ["wazuh-archives-4.x-2026.11.07"]

    async def any_event_since(self, index, field, days):
        return self.events

    async def dsl_search(self, index, query, window, size):
        self.searched.append((index, query, window, size))
        return 1200, self.docs[:size]

    async def close(self):
        self.closed = True


class _Box:
    """The sandbox, faked: what it lacks, and the final rule per event line."""

    def __init__(self, finals=None, lacks=(), fails=None):
        self.finals, self.lacks, self.fails = finals or {}, list(lacks), fails
        self.loaded, self.closed = None, False

    wrong = None

    async def guard(self, theirs):
        return self.wrong

    async def load(self, rs):
        if self.fails:
            raise wazuh_sandbox.WazuhError(self.fails)
        self.loaded = rs

    async def missing(self, needs):
        return [f"if_sid {s}" for s in needs.get("if_sid", ()) if s in self.lacks]

    async def replay(self, events):
        return [
            {
                "rule": {"id": self.finals.get(e["event"], "5710")},
                "decoder": {"name": "sshd"},
            }
            for e in events
        ]

    async def close(self):
        self.closed = True


def _copy(rule_id, xml, ran=False):
    return rollout.Copy(
        rule_id=rule_id,
        version=1,
        name=f"rule {rule_id}",
        language="wazuh",
        stage="shadow",
        stage_at=NOW - timedelta(days=8),
        checks=(
            {"runs": {"last_success": (NOW - timedelta(hours=1)).isoformat()}}
            if ran
            else {}
        ),
        rule={},
        key=str(100000 + rule_id),
        toml=xml,
    )


@pytest.fixture
def run(monkeypatch):
    wrote = {"runs": {}, "sampled": {}, "unrunnable": [], "sees": {}}

    def go(copies, service, box=None, configured=True):
        monkeypatch.setattr(
            rollout.wazuh_sees, "indexer", lambda: (None, {"max_searches_per_hour": 30})
        )
        monkeypatch.setattr(rollout.kibana, "service_of", lambda config: service)
        monkeypatch.setattr(rollout.search_budget, "spend", lambda *a, **k: True)
        monkeypatch.setattr(rollout.search_budget, "last_spent", lambda *a, **k: {})
        monkeypatch.setattr(rollout.exceptions, "active", lambda ids: {})
        monkeypatch.setattr(
            rollout.wazuh_sandbox,
            "configured",
            lambda: (
                {"url": "https://wazuh-sandbox:55000", "username": "w", "password": "p"}
                if configured
                else None
            ),
        )
        monkeypatch.setattr(rollout.wazuh_sandbox, "Sandbox", lambda *a, **k: box)
        monkeypatch.setattr(rollout.wazuh_sandbox, "repo_roots", lambda: [])
        monkeypatch.setattr(rollout.wazuh_sandbox, "customer", lambda: None)
        monkeypatch.setattr(
            rollout,
            "_write_unrunnable",
            lambda blocked, now: wrote["unrunnable"].extend(blocked) or len(blocked),
        )
        monkeypatch.setattr(
            rollout,
            "_write_sees",
            lambda rows, now: wrote["sees"].update(
                {c.rule_id: (r, w) for c, r, w in rows}
            )
            or len(rows),
        )

        def write_runs(cs, results, now, excepted=None, sampled=None):
            wrote["runs"].update(results)
            wrote["sampled"].update({rid: sampled for rid in results})
            return len(results)

        monkeypatch.setattr(rollout, "_write_runs", write_runs)
        report = rollout.RolloutReport()
        rollout._run_wazuh_copies(copies, NOW, report)
        return report

    go.wrote = wrote
    return go


def test_a_rule_on_the_raw_line_is_counted_by_the_sandbox(run):
    service = _Service([_doc("zzmarker 1"), _doc("other"), _doc("zzmarker 2")])
    box = _Box(finals={"zzmarker 1": "100001", "zzmarker 2": "100001"})
    report = run([_copy(1, MATCH)], service, box)
    total, docs, error = run.wrote["runs"][1]
    assert (total, [d["_id"] for d in docs], error) == (
        2,
        ["zzmarker 1", "zzmarker 2"],
        None,
    )
    assert (
        run.wrote["sampled"][1]["events"] == 3 and run.wrote["sampled"][1]["of"] == 1200
    )
    assert run.wrote["sees"][1][0] == "pass"
    # The archive's presence, then one sample for every sandboxed copy.
    assert report.searched == 2
    ((_, query, _, size),) = service.searched
    assert "function_score" in query and size == wazuh_sandbox.SAMPLE_EVENTS
    assert box.loaded.rules and box.closed and service.closed


def test_without_a_sandbox_the_rule_reads_cant_validate_and_why(run):
    run([_copy(1, MATCH)], _Service(), configured=False)
    ((copy, why),) = run.wrote["unrunnable"]
    assert "<match> on full_log isn't exact" in why
    assert wazuh_sandbox.NO_SANDBOX in why
    assert run.wrote["runs"] == {} and run.wrote["sees"][1][0] == "unknown"


def test_a_parent_only_on_the_customers_manager_reads_not_in_repo(run):
    xml = _rule("<if_sid>123456</if_sid><match>x</match>")
    run([_copy(1, xml)], _Service([_doc("x")]), _Box(lacks=["123456"]))
    ((copy, why),) = run.wrote["unrunnable"]
    assert why.startswith("can't validate: not in repo: if_sid 123456")
    assert run.wrote["runs"] == {}


def test_an_unreachable_sandbox_leaves_its_copies_unvalidated(run):
    run([_copy(1, MATCH)], _Service(), _Box(fails="Wazuh manager could not be reached"))
    ((copy, why),) = run.wrote["unrunnable"]
    assert why == "can't validate: Wazuh sandbox: Wazuh manager could not be reached"
    assert run.wrote["sees"][1][0] == "unknown"


def test_a_frequency_rule_is_not_sandboxed(run):
    xml = _rule(
        "<if_sid>5710</if_sid><match>x</match>", attrs=' frequency="8" timeframe="60"'
    )
    box = _Box()
    run([_copy(1, xml)], _Service(), box)
    assert "frequency needs every event before it" in run.wrote["unrunnable"][0][1]
    assert box.loaded is None


def test_a_sandbox_run_waits_until_its_window_is_due(run):
    service, box = _Service([_doc("x")]), _Box()
    run([_copy(1, MATCH, ran=True)], service, box)
    assert box.loaded is None and run.wrote["runs"] == {}
    assert run.wrote["unrunnable"] == []  # nothing written: the window keeps growing


def test_a_sandbox_that_is_the_customers_manager_is_never_written_to(run):
    box = _Box()
    box.wrong = "is the customer's Wazuh manager (the same host and port)"
    run([_copy(1, MATCH)], _Service([_doc("x")]), box)
    ((copy, why),) = run.wrote["unrunnable"]
    assert why == (
        "can't validate: the Wazuh sandbox is the customer's Wazuh manager "
        "(the same host and port)"
    )
    assert box.loaded is None and run.wrote["runs"] == {}


@pytest.mark.asyncio
async def test_guard_refuses_the_customers_manager_or_any_with_agents(monkeypatch):
    calls = []

    def handler(agents, hostname):
        def h(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/authenticate"):
                return _ok({"token": "t"})
            if request.url.path == "/agents":
                return _ok({"affected_items": [{"id": a} for a in agents]})
            return _ok({"hostname": hostname})

        return h

    async def theirs_host(config):
        return "wazuh-prod"

    monkeypatch.setattr(wazuh_sandbox, "customer_hostname", theirs_host)
    theirs = {
        "api_url": "https://Wazuh-Sandbox:55000/",
        "username": "u",
        "password": "p",
    }
    same = await _sandbox(handler(["000"], "x")).guard(theirs)
    assert "same host and port" in same and calls == []  # refused before any call
    other = {**theirs, "api_url": "https://wazuh.acme.corp:55000"}
    assert "has agents" in await _sandbox(handler(["000", "001"], "x")).guard(other)
    assert "same manager hostname" in await _sandbox(
        handler(["000"], "wazuh-prod")
    ).guard(other)
    assert await _sandbox(handler(["000"], "wazuh-sandbox")).guard(other) is None
    assert await _sandbox(handler(["000"], "wazuh-sandbox")).guard(None) is None


@pytest.mark.asyncio
async def test_every_sandbox_call_is_paced(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(wazuh_sandbox.asyncio, "sleep", sleep)

    def handler(request):
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        return _ok({"affected_items": []})

    box = _sandbox(handler)
    box.pace = 30.0
    await box.load(wazuh_sandbox.Ruleset(rules={"vigil_sandbox_0.xml": MATCH}))
    # Uploads and listings, not only logtest: one wait before each call but the first.
    assert len(slept) == 2 and all(0 < s <= 30.0 for s in slept)


def test_an_eventchannel_event_is_replayed_as_eventchannel():
    doc = _doc("{}", decoder="windows_eventchannel")
    doc["_source"]["location"] = "EventChannel"
    assert wazuh_sandbox.event(doc)["log_format"] == "eventchannel"
