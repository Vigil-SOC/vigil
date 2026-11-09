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


def _repo(tmp_path):
    repo = tmp_path / "acme"
    (repo / "rules").mkdir(parents=True)
    (repo / "decoders").mkdir()
    (repo / ".git").mkdir()
    (repo / "rules" / "local_rules.xml").write_text(
        '<group name="local,acme,">'
        '<rule id="100001" level="3"><decoded_as>acmeapp</decoded_as>'
        "<description>a</description></rule>"
        '<rule id="100100" level="3"><match>old</match><description>b</description></rule>'
        "</group>"
    )
    (repo / "rules" / "unrelated.xml").write_text(
        '<group name="other,"><rule id="100050" level="3"><match>z</match>'
        "<description>z</description></rule></group>"
    )
    (repo / "decoders" / "local_decoder.xml").write_text(
        '<decoder name="acmeapp"><prematch>^ACME:</prematch></decoder>'
        '<decoder name="acmeapp-user"><parent>acmeapp</parent><regex>u=(\\S+)</regex>'
        "<order>user</order></decoder>"
    )
    (repo / ".git" / "ignored.xml").write_text("<rule id='1'/>")
    # Wazuh's stock 0910: a raw "\<" ElementTree (and the API) can't read.
    (repo / "rules" / "0910-proxylogon.xml").write_text(
        '<group name="x,"><rule id="100002" level="3"><regex>a\\<b</regex>'
        "<description>raw</description></rule></group>"
    )
    return repo


def test_the_repositories_are_indexed_and_a_bad_file_is_listed_not_fatal(tmp_path):
    live = _Copy("100200", _rule("<match>live</match>", rid="100200"))
    repo = wazuh_sandbox.read_repos([_repo(tmp_path)], [live])
    assert repo.rule_file["100001"] == "acme/rules/local_rules.xml"
    assert repo.group_rules["acme"] == [
        ("100001", "acme/rules/local_rules.xml"),
        ("100100", "acme/rules/local_rules.xml"),
    ]
    assert repo.decoder_file["acmeapp-user"] == "acme/decoders/local_decoder.xml"
    assert repo.needs["acme/rules/local_rules.xml"]["decoded_as"] == ["acmeapp"]
    assert repo.rule_file["100200"] == "overlay/100200.xml"
    assert [u.split(":")[0] for u in repo.unreadable] == [
        "acme/rules/0910-proxylogon.xml"
    ]
    assert not any("ignored" in name for name in repo.rules)


def test_a_bare_rule_copy_is_wrapped_in_a_group():
    bare = '<rule id="100100" level="5"><match>x</match><description>d</description></rule>'
    assert wazuh_sandbox.wrapped(bare).startswith(
        '<group name="vigil,"><rule id="100100"'
    )
    assert wazuh_sandbox.wrapped(MATCH) == MATCH


@pytest.mark.asyncio
async def test_only_the_files_the_copies_need_are_planned(tmp_path, monkeypatch):
    repo = wazuh_sandbox.read_repos([_repo(tmp_path)])
    box = _sandbox(lambda r: _ok({}))
    stock = {("rule", "5710")}

    async def has(kind, name):
        return (kind, name) in stock

    monkeypatch.setattr(box, "_has", has)
    child = _Copy(
        "100300", _rule("<if_sid>100001</if_sid><match>x</match>", rid="100300")
    )
    rules, decoders = await box.plan([child], repo)
    # The parent's file, and the decoder its rule is decoded_as: not unrelated.xml.
    assert rules == ["acme/rules/local_rules.xml"]
    assert decoders == ["acme/decoders/local_decoder.xml"]
    stock_child = _Copy(
        "100301", _rule("<if_sid>5710</if_sid><match>x</match>", rid="100301")
    )
    assert await box.plan([stock_child], repo) == ([], [])


@pytest.mark.asyncio
async def test_if_group_loads_only_the_groups_rules_the_stock_ruleset_lacks(
    tmp_path, monkeypatch
):
    root = tmp_path / "corp"
    root.mkdir()
    # The repository carries a stock file (its rules are the image's own) and
    # one custom rule in the same group.
    (root / "0095-sshd_rules.xml").write_text(
        '<group name="syslog,sshd,"><rule id="5710" level="5"><match>illegal</match>'
        "<group>authentication_failed,</group><description>s</description></rule>"
        '<rule id="5711" level="0"><match>x</match>'
        "<group>authentication_failed,</group><description>t</description></rule></group>"
    )
    (root / "local_rules.xml").write_text(
        '<group name="local,authentication_failed,"><rule id="100060" level="5">'
        "<match>corp-vpn</match><description>c</description></rule></group>"
    )
    repo = wazuh_sandbox.read_repos([root])
    box = _sandbox(lambda r: _ok({}))
    asked = []

    async def loaded(ids):
        asked.append(sorted(ids))
        return {"5710", "5711"} & set(ids)

    async def has(kind, name):
        return False

    monkeypatch.setattr(box, "_loaded", loaded)
    monkeypatch.setattr(box, "_has", has)
    copy = _Copy(
        "100300", _rule("<if_group>authentication_failed</if_group><match>m</match>")
    )
    assert await box.plan([copy], repo) == (["corp/local_rules.xml"], [])
    assert asked == [["100060", "5710", "5711"]]  # one listing for the group


def _overwriting_repo(tmp_path):
    root = tmp_path / "corp"
    (root / "rules").mkdir(parents=True)
    # A copy of Wazuh's stock file: its rules are the image's own, never loaded.
    (root / "rules" / "0095-sshd_rules.xml").write_text(
        '<group name="syslog,sshd,"><rule id="5710" level="5"><if_sid>5700</if_sid>'
        "<match>illegal user|invalid user</match>"
        "<group>invalid_login,</group><description>s</description></rule></group>"
    )
    # The customer's own 5710, which their manager runs in place of Wazuh's;
    # 100070 hangs off a repository rule the stock ruleset lacks.
    (root / "rules" / "local_rules.xml").write_text(
        '<group name="local,sshd,"><rule id="5710" level="10" overwrite="yes">'
        "<if_sid>5700</if_sid><match>illegal user|invalid user|unknown login</match>"
        "<group>invalid_login,</group><description>c</description></rule>"
        '<rule id="100070" level="5"><if_sid>100060</if_sid><match>v</match>'
        "<description>v</description></rule></group>"
    )
    (root / "rules" / "corp_vpn.xml").write_text(
        '<group name="corp,"><rule id="100060" level="3"><match>corp-vpn</match>'
        "<description>c</description></rule>"
        # Overwrites a rule the stock ruleset lacks: loaded only when needed.
        '<rule id="100061" level="3" overwrite="yes"><match>corp-x</match>'
        "<description>x</description></rule></group>"
    )
    return wazuh_sandbox.read_repos([root])


def test_a_rule_that_overwrites_is_indexed(tmp_path):
    repo = _overwriting_repo(tmp_path)
    assert repo.overwrites == {
        "5710": "corp/rules/local_rules.xml",
        "100061": "corp/rules/corp_vpn.xml",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "<if_sid>5710</if_sid><match>zzmarker</match>",
        "<if_group>invalid_login</if_group><match>zzmarker</match>",
        "<if_sid>5700</if_sid><match>zzmarker</match>",  # beside the overwrite
    ],
)
async def test_a_customers_overwrite_of_a_stock_rule_is_loaded(
    tmp_path, monkeypatch, body
):
    repo = _overwriting_repo(tmp_path)
    box = _sandbox(lambda r: _ok({}))
    stock = {"5700", "5710"}

    async def loaded(ids):
        return stock & set(ids)

    async def has(kind, name):
        return kind == "rule" and name in stock

    monkeypatch.setattr(box, "_loaded", loaded)
    monkeypatch.setattr(box, "_has", has)
    copy = _Copy("100300", _rule(body, rid="100300"))
    rules, _ = await box.plan([copy], repo)
    # The overwrite's file, and the file its other rule needs (100060); never
    # the stock copy, nor corp_vpn.xml's overwrite for its own sake.
    assert rules == ["corp/rules/local_rules.xml", "corp/rules/corp_vpn.xml"]
    assert await box.plan([], repo) == ([], [])  # a load that only clears


@pytest.mark.asyncio
async def test_a_copy_of_the_overwritten_rule_is_not_doubled(tmp_path, monkeypatch):
    repo = _overwriting_repo(tmp_path)
    box = _sandbox(lambda r: _ok({}))
    asked = []

    async def loaded(ids):
        asked.append(sorted(ids))
        return {"5710"} & set(ids)

    async def has(kind, name):
        return name == "5700"

    monkeypatch.setattr(box, "_loaded", loaded)
    monkeypatch.setattr(box, "_has", has)
    # The overlay's copy of 5710 is the newer text: it is the copy, not a parent.
    copy = _Copy("5710", _rule("<if_sid>5700</if_sid><match>m</match>", rid="5710"))
    assert await box.plan([copy], repo) == ([], [])
    assert asked == [["100061"]]


@pytest.mark.asyncio
async def test_a_busy_managers_timeout_is_asked_again(monkeypatch):
    slept, answers = [], [500, 200]

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(wazuh_sandbox.asyncio, "sleep", sleep)

    def handler(request):
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        if answers.pop(0) == 500:
            return httpx.Response(500, json={"title": "Timeout executing API request"})
        return _ok({"hostname": "wazuh-sandbox"})

    box = _sandbox(handler)
    box.retry_waits = (7.0,)
    assert (await box._request("GET", "/"))["hostname"] == "wazuh-sandbox"
    assert 7.0 in slept


@pytest.mark.asyncio
async def test_a_refused_file_is_deleted_and_reported_and_the_rest_still_load(
    tmp_path, monkeypatch
):
    seen = []
    bad_copy = _Copy(
        "100400", _rule("<if_sid>5710</if_sid><match>bad</match>", rid="100400")
    )

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        if request.method == "GET":
            kind = request.url.path.split("/")[1]
            names = (
                ["vigil_sandbox_7.xml", "local_rules.xml"] if kind == "rules" else []
            )
            return _ok({"affected_items": [{"filename": n} for n in names]})
        if request.method == "PUT" and b"bad" in request.content:
            return httpx.Response(400, json={"title": "XML syntax error"})
        return _ok({})

    box = _sandbox(handler)

    async def has(kind, name):
        return name == "5710"

    monkeypatch.setattr(box, "_has", has)
    repo = wazuh_sandbox.read_repos([_repo(tmp_path)])
    good = _Copy(
        "100300", _rule("<if_sid>100001</if_sid><match>x</match>", rid="100300")
    )
    done = await box.load([good, bad_copy], repo)
    # Last run's file is cleared first; the image's own are never touched.
    assert seen.index(("DELETE", "/rules/files/vigil_sandbox_7.xml")) < seen.index(
        ("PUT", "/decoders/files/vigil_sandbox_0.xml")
    )
    assert not any("local_rules" in path for m, path in seen if m == "DELETE")
    # decoder, the parent's file, the good copy: loaded. The bad copy: refused,
    # then deleted again so it can't poison the next upload.
    assert done.files == 3
    assert "XML syntax error" in done.refused["100400"]
    assert ("DELETE", "/rules/files/vigil_sandbox_3.xml") in seen
    assert any(s.startswith("acme/rules/0910-proxylogon.xml") for s in done.skipped)
    assert box.copy_files == {"100300": "vigil_sandbox_2.xml"}  # only those loaded


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
        if request.url.path == "/rules" and q.get("rule_ids") == "123456":
            # The manager fails the call when no listed id exists.
            return httpx.Response(
                400,
                json={
                    "detail": "The rule does not exist or you do not have permission to see it"
                },
            )
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
    assert tally.sampled("100100") == {
        "engine": wazuh_sandbox.ENGINE,
        "events": 3,
        "of": 1200,
        "decoded_differently": 1,
        "refused": 1,
        "hits": 1,
        "estimated": 400,  # 1 hit in 3 replayed of 1,200: about 400 across the window
    }
    assert tally.estimate("5710") == 0


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

    def __init__(self, finals=None, lacks=(), fails=None, matches=None):
        self.finals, self.lacks, self.fails = finals or {}, list(lacks), fails
        self.loaded, self.closed = None, False
        # event line -> the copy keys whose rule matches it, in Wazuh's order:
        # the first one loaded ends the event, as siblings do on a manager.
        self.matches, self.loads = matches or {}, []

    wrong = None

    async def guard(self, theirs):
        return self.wrong

    refused = {}

    async def load(self, copies, repo):
        if self.fails:
            raise wazuh_sandbox.WazuhError(self.fails)
        self.loaded = list(copies)
        self.loads.append(
            ([c.key for c in copies], sorted(n for n in repo.rules if "overlay" in n))
        )
        return wazuh_sandbox.Loaded(refused=dict(self.refused), files=len(copies))

    async def missing(self, needs):
        return [f"if_sid {s}" for s in needs.get("if_sid", ()) if s in self.lacks]

    async def replay(self, events):
        loaded = {c.key for c in self.loaded}
        self.replayed = getattr(self, "replayed", []) + [[e["event"] for e in events]]
        return [
            {
                "rule": {
                    "id": next(
                        (k for k in self.matches.get(e["event"], ()) if k in loaded),
                        self.finals.get(e["event"], "5710"),
                    )
                },
                "decoder": {"name": "sshd"},
            }
            for e in events
        ]

    async def alone(self, copy, keep, events):
        self.loaded = [copy, *keep]
        self.loads.append(([c.key for c in self.loaded], "alone"))
        return await self.replay(events)

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

    def go(copies, service, box=None, configured=True, password="p"):
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
                {
                    "url": "https://wazuh-sandbox:55000",
                    "username": "w",
                    "password": password,
                }
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
            wrote["sampled"].update({rid: (sampled or {}).get(rid) for rid in results})
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
    # 2 hits in the 3 events replayed of 1,200 in the window: about 800, and
    # that estimate is what the daily hit limit sees.
    assert (total, [d["_id"] for d in docs], error) == (
        800,
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
    assert [c.rule_id for c in box.loaded] == [1] and box.closed and service.closed


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
    box.wrong = "the Wazuh sandbox is your Wazuh manager (the same host and port)"
    run([_copy(1, MATCH)], _Service([_doc("x")]), box)
    ((copy, why),) = run.wrote["unrunnable"]
    assert why == (
        "can't validate: the Wazuh sandbox is your Wazuh manager "
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
        return "wazuh-prod", None

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
    assert "same hostname" in await _sandbox(handler(["000"], "wazuh-prod")).guard(
        other
    )
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
    await box.load([_Copy("100100", MATCH)], wazuh_sandbox.Repo())
    # Listings and the upload, not only logtest: a wait before each call but the first.
    assert len(slept) == 2 and all(0 < s <= 30.0 for s in slept)


def test_an_eventchannel_event_is_replayed_as_eventchannel():
    doc = _doc("{}", decoder="windows_eventchannel")
    doc["_source"]["location"] = "EventChannel"
    assert wazuh_sandbox.event(doc)["log_format"] == "eventchannel"


@pytest.mark.asyncio
async def test_guard_fails_closed_when_the_customers_manager_cant_be_read(monkeypatch):
    async def unread(config):
        return None, "Wazuh manager could not be reached (ConnectError)"

    monkeypatch.setattr(wazuh_sandbox, "customer_hostname", unread)

    def handler(request):
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        if request.url.path == "/agents":
            return _ok({"affected_items": [{"id": "000"}]})
        return _ok({"hostname": "wazuh-sandbox"})

    theirs = {
        "api_url": "https://wazuh.acme.corp:55000",
        "username": "u",
        "password": "p",
    }
    why = await _sandbox(handler).guard(theirs)
    assert why.startswith("couldn't confirm the sandbox isn't your Wazuh manager")
    assert "could not be reached" in why


def test_a_copy_the_sandbox_refuses_reads_why(run):
    box = _Box()
    box.refused = {
        "100001": "Wazuh PUT /rules/files/x failed (HTTP 400): XML syntax error"
    }
    run([_copy(1, MATCH)], _Service([_doc("x")]), box)
    ((copy, why),) = run.wrote["unrunnable"]
    assert why.startswith("can't validate: the Wazuh sandbox refused it: ")
    assert "XML syntax error" in why and run.wrote["runs"] == {}


def test_a_sandbox_without_a_password_is_not_used(run):
    box = _Box()
    run([_copy(1, MATCH)], _Service([_doc("x")]), box, password="")
    ((copy, why),) = run.wrote["unrunnable"]
    assert why.endswith(wazuh_sandbox.NO_PASSWORD) and box.loaded is None


def test_a_loud_rule_counted_on_a_sample_cannot_promote():
    # 1 hit in 486 replayed of 12,840: ~27 a day, above the 20-hit ceiling.
    tally = wazuh_sandbox.Tally(window_events=12840, replayed=486)
    tally.hits["100100"] = [{}]
    assert tally.estimate("100100") == 27
    checks = rollout._ran(
        {},
        "shadow",
        NOW,
        (tally.estimate("100100"), [], None),
        NOW,
        sampled=tally.sampled("100100"),
    )
    assert (
        checks["shadow"]["days"]["2026-11-07"]
        == 27
        > rollout.DEFAULT_LIMITS.max_hits_per_day
    )
    assert checks["runs"]["sampled"]["hits"] == 1


# --- copies counted together (#4145) ----------------------------------------------


def test_events_another_copy_ended_are_the_ones_counted_again():
    outputs = [
        {"rule": {"id": "100001"}},
        {"rule": {"id": "5710"}},
        None,
        {"rule": {"id": "100002"}},
    ]
    assert wazuh_sandbox.won_by_another(outputs, ["100001", "100002", "100003"]) == {
        "100001": [3],
        "100002": [0],
        "100003": [0, 3],
    }
    assert wazuh_sandbox.won_by_another(outputs, ["100001"]) == {}


def test_counted_alone_a_copy_keeps_the_copies_it_hangs_off_by_if_sid():
    top = _Copy("100001", _rule("<if_sid>5710</if_sid><match>a</match>", "100001"))
    mid = _Copy("100002", _rule("<if_sid>100001</if_sid><match>b</match>", "100002"))
    low = _Copy("100003", _rule("<if_sid>100002, 5700</if_sid>", "100003"))
    grouped = _Copy("100004", _rule("<if_group>sshd</if_group>", "100004"))
    copies = [top, mid, low, grouped]
    assert wazuh_sandbox.chain(low, copies) == [mid, top]
    assert wazuh_sandbox.chain(top, copies) == []
    assert wazuh_sandbox.chain(grouped, copies) == []


def test_two_copies_under_one_parent_are_each_counted_as_if_alone(run):
    # Both copies match a, b and c; Wazuh ends each on the first, 100001.
    lines = ["a", "b", "c", "d"]
    group = _rule("<if_group>invalid_login</if_group><match>zz</match>", "100001")
    sid = _rule("<if_sid>5710</if_sid><match>zz</match>", "100002")
    box = _Box(matches={x: ["100001", "100002"] for x in "abc"})
    run(
        [_copy(1, group), _copy(2, sid)],
        _Service([_doc(x) for x in lines]),
        box,
    )
    # 3 hits each in the 4 replayed of 1,200: 900 each, as alone, not 900 and 0.
    assert run.wrote["runs"][1][0] == run.wrote["runs"][2][0] == 900
    assert run.wrote["sampled"][2]["hits"] == 3
    assert run.wrote["sampled"][2]["events"] == 4
    # One load for both, then 100002 alone on the 3 events 100001 ended; 100001
    # took no event of 100002's, so it is not loaded again.
    assert [keys for keys, _ in box.loads] == [["100001", "100002"], ["100002"]]
    assert box.replayed == [lines, ["a", "b", "c"]]


def test_copies_on_different_events_keep_their_own_counts(run):
    box = _Box(finals={"a": "100001", "b": "100002"})
    run(
        [_copy(1, MATCH), _copy(2, _rule("<match>b</match>", "100002"))],
        _Service([_doc("a"), _doc("b")]),
        box,
    )
    # Each ended an event the other is counted on again alone, where it still
    # isn't theirs: one load together, then one alone each.
    assert run.wrote["runs"][1][0] == run.wrote["runs"][2][0] == 600
    assert len(box.loads) == 3


def test_a_copy_counted_alone_keeps_its_parent_copy_loaded(run):
    parent = _rule("<if_sid>5710</if_sid><match>zz</match>", "100001")
    child = _rule("<if_sid>100001</if_sid><match>zz</match>", "100002")
    sibling = _rule("<if_sid>100001</if_sid><match>z</match>", "100003")
    box = _Box(matches={"zz": ["100003", "100002", "100001"]})
    run(
        [_copy(1, parent), _copy(2, child), _copy(3, sibling)],
        _Service([_doc("zz")]),
        box,
    )
    alone = {keys[0]: keys[1:] for keys, _ in box.loads[1:]}
    # The child alone keeps its parent copy; not its sibling, which won the
    # event and so isn't counted again.
    assert alone == {"100001": [], "100002": ["100001"]}
    assert [run.wrote["runs"][n][0] for n in (1, 2, 3)] == [1200, 1200, 1200]


@pytest.mark.asyncio
async def test_alone_swaps_copy_files_and_never_uploads_the_repository_again():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/authenticate"):
            return _ok({"token": "t"})
        if request.url.path == "/logtest":
            return _ok({"token": "s", "output": {"rule": {"id": "100002"}}})
        return _ok({})

    box = _sandbox(handler)
    box.copy_files = {
        "100001": "vigil_sandbox_3.xml",
        "100002": "vigil_sandbox_4.xml",
        "100003": "vigil_sandbox_5.xml",
    }
    me = _Copy("100002", _rule("<if_sid>100009</if_sid>", rid="100002"))
    parent = _Copy("100009", _rule("<if_sid>5710</if_sid>", rid="100009"))
    outs = await box.alone(me, [parent], [{"event": "x"}, {"event": "y"}])
    assert [wazuh_sandbox.final(o) for o in outs] == ["100002", "100002"]
    writes = [(m, p) for m, p in seen if m in ("PUT", "DELETE") and p != "/logtest"]
    # The siblings' files go, the missing parent copy comes up; nothing else.
    assert writes == [
        ("DELETE", "/rules/files/vigil_sandbox_3.xml"),
        ("DELETE", "/rules/files/vigil_sandbox_5.xml"),
        ("PUT", "/rules/files/vigil_sandbox_copy_100009.xml"),
        ("DELETE", "/logtest/sessions/s"),
    ]
    assert box.copy_files == {
        "100002": "vigil_sandbox_4.xml",
        "100009": "vigil_sandbox_copy_100009.xml",
    }
