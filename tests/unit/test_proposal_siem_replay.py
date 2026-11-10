"""A proposal's Sigma replayed on the SIEM's own events (#4157): hits on the Case's
attack events, false positives on events no Case claims (with samples), and a
rule proven only when it hit, stayed quiet on normal events that hold its fields,
and every read was whole."""

import asyncio
from datetime import datetime, timedelta, timezone

from core.detections.engineering import siem_replay as sr

NOW = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=30)
AT = (NOW - timedelta(hours=2)).isoformat()

CLEAN = """title: Excel spawns rundll32
detection:
  selection:
    EventID: 1
    ParentImage|endswith: '\\EXCEL.EXE'
    Image|endswith: '\\rundll32.exe'
  condition: selection
"""
BROAD = """title: Process started by a non-shell parent
detection:
  selection:
    EventID: 1
  filter_shell:
    ParentImage|endswith:
      - '\\explorer.exe'
      - '\\cmd.exe'
  condition: selection and not filter_shell
"""
TUNED = BROAD.replace(
    "      - '\\cmd.exe'\n",
    "      - '\\cmd.exe'\n      - '\\splunkd.exe'\n      - '\\services.exe'\n",
)


def _winlog(doc_id, parent, image, host="win-host-5.attackrange.local", at=AT):
    """A Sysmon process start as winlogbeat indexes it."""
    return {
        "_id": doc_id,
        "_source": {
            "@timestamp": at,
            "host": {"name": host},
            "winlog": {
                "computer_name": host,
                "event_id": 1,
                "event_data": {"ParentImage": parent, "Image": image, "EventID": 1},
            },
        },
    }


EXCEL = "C:\\Program Files\\Microsoft Office\\root\\Office16\\EXCEL.EXE"
RUNDLL = "C:\\Windows\\System32\\rundll32.exe"
ATTACK = [_winlog("a1", EXCEL, RUNDLL), _winlog("a2", EXCEL, RUNDLL)]
NORMAL = [
    _winlog(
        f"n{i}",
        "C:\\Program Files\\Splunk\\bin\\splunkd.exe",
        f"C:\\splunk-{i}.exe",
        host="win-dc-982",
    )
    for i in range(3)
] + [
    _winlog("n9", "C:\\Windows\\explorer.exe", EXCEL, host="win-dc-982"),
    _winlog(
        "n8", "C:\\Windows\\System32\\services.exe", "C:\\sppsvc.exe", host="win-dc-982"
    ),
]


class _Fake(sr.ElasticSource):
    """A SIEM holding ``attack`` and ``normal`` events; reads recorded."""

    def __init__(
        self, attack=(), normal=(), total=None, budget=True, fails=None, read=None
    ):
        super().__init__(
            service=None, index="scout-icedid", per_hour=30, integration="elastic-siem"
        )
        self._attack, self._normal = list(attack), list(normal)
        self.total, self.budget, self.fails = total, budget, fails
        self.read = read  # how many attack events the attack read returns
        self.reads = []

    def spend(self, what):
        self.reads.append(what)
        return self.budget

    async def attack(self, claim, ids, fields, start, end):
        if self.fails:
            raise RuntimeError(self.fails)
        docs = [d for d in self._attack if not ids or d["_id"] in ids]
        return sr.Read(total=len(docs), docs=docs[: self.read])

    async def benign(self, fields, start, end):
        docs = self._attack + self._normal
        return sr.Read(total=self.total or len(docs), docs=docs)


CASE = sr.claim_of(
    "case:case-icedid",
    {"hostnames": ["win-host-5"], "users": [], "src_ips": []},
    datetime.fromisoformat(AT) - timedelta(minutes=5),
    datetime.fromisoformat(AT) + timedelta(minutes=5),
)


def _run(sigma, sources, claims=(CASE,), attack_claim=CASE, named=None):
    return asyncio.run(
        sr.replay(
            sigma,
            sources=sources,
            attack_claim=attack_claim,
            named=named,
            claims=list(claims),
            start=START,
            end=NOW,
        )
    )


# --- the verdicts the issue names -------------------------------------------------


def test_a_clean_rule_is_proven():
    out = _run(CLEAN, [_Fake(ATTACK, NORMAL)])
    assert out["proven"] is True and out["unproven_because"] is None
    assert (out["hits"], out["false_positives"]) == (2, 0)
    assert out["attack_basis"] == "linked to case:case-icedid"
    assert out["benign_holding_fields"] == 5  # the attack events are not normal ones


def test_a_broad_rule_shows_its_false_positives_with_samples():
    out = _run(BROAD, [_Fake(ATTACK, NORMAL)])
    assert out["proven"] is False and out["hits"] == 2
    assert out["false_positives"] == 4  # splunkd's 3 children and sppsvc
    (siem,) = out["siems"]
    shown = {s["id"] for s in siem["false_positive_samples"]}
    assert shown == {"n0", "n1", "n2", "n8"}
    sample = siem["false_positive_samples"][0]
    assert sample["host"] == "win-dc-982" and "ParentImage" in sample["fields"]
    assert out["unproven_because"].startswith(
        "It matched 4 events no Case claims (4 in Elastic)"
    )


def test_tuning_it_and_replaying_again_proves_it():
    assert _run(TUNED, [_Fake(ATTACK, NORMAL)])["proven"] is True


# --- what makes a replay unproven -------------------------------------------------


def test_a_read_cut_short_proves_nothing():
    out = _run(CLEAN, [_Fake(ATTACK, NORMAL, total=9000)])
    assert out["proven"] is False
    assert "normal events was cut short in Elastic" in out["unproven_because"]


def test_a_cut_short_attack_read_with_a_hit_still_proves():
    class _Busy(_Fake):
        async def attack(self, claim, ids, fields, start, end):
            read = await super().attack(claim, ids, fields, start, end)
            return sr.Read(total=9000, docs=read.docs)

    out = _run(CLEAN, [_Busy(ATTACK, NORMAL)])
    assert out["proven"] is True and out["siems"][0]["attack_cut_short"] is True
    missed = _run(CLEAN, [_Busy([_winlog("a9", EXCEL, "C:\\x.exe")], NORMAL)])
    assert (
        "only the newest of the Case's events were read in Elastic"
        in missed["unproven_because"]
    )


def test_normal_events_without_the_rules_fields_prove_nothing():
    other = [
        {
            "_id": "x",
            "_source": {
                "@timestamp": AT,
                "message": "dns query",
                "host": {"name": "dc"},
            },
        }
    ]
    out = _run(CLEAN, [_Fake(ATTACK, other)])
    assert out["benign_holding_fields"] == 0 and out["proven"] is False
    assert (
        "No normal event in the window holds a field the rule reads"
        in out["unproven_because"]
    )


def test_no_attack_event_read_says_how_to_get_one():
    out = _run(CLEAN, [_Fake([], NORMAL)], attack_claim=None)
    assert out["attack_basis"] == "none" and out["proven"] is False
    assert "No attack event was read" in out["unproven_because"]


def test_a_spent_budget_or_a_failed_read_is_named_and_proves_nothing():
    spent = _run(CLEAN, [_Fake(ATTACK, NORMAL, budget=False)])
    assert spent["evaluated"] is False  # nothing read, nothing judged
    assert (
        "Elastic was not read: its search budget for this hour is spent."
        in spent["unproven_because"]
    )
    failed = _run(CLEAN, [_Fake(ATTACK, NORMAL, fails="index_not_found_exception")])
    assert (
        "Elastic could not be read: index_not_found_exception"
        in failed["unproven_because"]
    )
    assert failed["proven"] is False


def test_an_unreplayable_rule_says_which_construct():
    agg = CLEAN.replace("condition: selection", "condition: selection | count() > 5")
    out = _run(agg, [_Fake(ATTACK, NORMAL)])
    assert out["evaluated"] is False and out["proven"] is False
    assert out["unproven_because"].startswith("The rule cannot be replayed")


# --- claims -------------------------------------------------------------------------


def test_the_cases_own_event_outside_the_attack_read_is_never_a_false_positive():
    out = _run(CLEAN, [_Fake(ATTACK, NORMAL, read=1)])
    assert out["false_positives"] == 0 and out["hits"] == 1
    assert out["siems"][0]["claimed_left_out"] == 1


def test_events_another_case_claims_are_not_counted_as_false_positives():
    dc_case = sr.claim_of(
        "case:case-dc",
        {"hostnames": ["WIN-DC-982.attackrange.local"]},
        datetime.fromisoformat(AT) - timedelta(minutes=5),
        datetime.fromisoformat(AT) + timedelta(minutes=5),
    )
    out = _run(BROAD, [_Fake(ATTACK, NORMAL)], claims=(CASE, dc_case))
    assert out["false_positives"] == 0 and out["siems"][0]["claimed_left_out"] == 5


def test_a_run_ruled_out_still_counts_as_normal_activity():
    ruled_out = sr.claim_of(
        "case:case-fp", {"hostnames": ["win-dc-982"]}, START, NOW, ruled_out=True
    )
    out = _run(BROAD, [_Fake(ATTACK, NORMAL)], claims=(CASE, ruled_out))
    assert out["false_positives"] == 4


def test_a_claim_holds_only_inside_its_window():
    late = _winlog("late", EXCEL, RUNDLL, at=(NOW - timedelta(days=3)).isoformat())
    assert not CASE.holds(sr.event_of("elastic", late))
    assert CASE.holds(sr.event_of("elastic", ATTACK[0]))


def test_named_events_are_the_attack_set():
    out = _run(CLEAN, [_Fake(ATTACK, NORMAL)], named={"elastic": ["a1"]})
    assert out["attack_basis"] == "named by a person" and out["hits"] == 1


# --- reading an event as a rule does ------------------------------------------------


def test_a_rule_field_finds_the_events_own_path_in_any_case():
    winlog = sr.event_of("elastic", ATTACK[0]).view
    assert winlog["Image"] == RUNDLL
    wazuh = sr.View(sr.flatten({"data": {"win": {"eventdata": {"image": RUNDLL}}}}))
    assert "Image" in wazuh and wazuh.get("Image") == RUNDLL


def test_sigmas_event_id_finds_winlogbeats_and_wazuhs():
    winlog = sr.View(sr.flatten({"winlog": {"event_id": 1}}))
    wazuh = sr.View(sr.flatten({"data": {"win": {"system": {"eventID": "1"}}}}))
    assert winlog["EventID"] == 1 and wazuh["EventID"] == "1"


def test_a_field_two_paths_end_in_is_not_guessed():
    view = sr.View(sr.flatten({"host": {"name": "a"}, "user": {"name": "b"}}))
    assert "name" not in view and view["host.name"] == "a"
    same = sr.View(
        sr.flatten({"winlog": {"event_id": 1, "event_data": {"EventID": "1"}}})
    )
    assert same["EventID"] == 1  # two paths, one value


def test_an_events_host_user_and_address_are_read():
    event = sr.event_of(
        "wazuh",
        {
            "_id": "w1",
            "_source": {
                "@timestamp": AT,
                "agent": {"name": "WIN-HOST-5"},
                "data": {
                    "srcip": "10.0.0.5",
                    "win": {"eventdata": {"user": "CORP\\Alice"}},
                },
            },
        },
    )
    assert {"win-host-5"} <= event.hosts and event.users == {"alice"}
    assert event.ips == {"10.0.0.5"}


def test_the_fields_a_rule_reads():
    assert sr.rule_fields(BROAD) == ["EventID", "ParentImage"]
    assert sr.rule_fields("not: [yaml") == []


# --- what the SIEMs are asked ------------------------------------------------------


class _Cluster:
    """dsl_search over ``docs`` (oldest first): in ``sort`` order when asked, and
    cut at ``size``, as Elasticsearch does."""

    def __init__(self, docs):
        self.docs, self.asked = list(docs), []

    async def dsl_search(self, index, query, window, size, sort=None):
        self.asked.append({"query": query, "sort": sort})
        docs = self.docs
        if sort:
            docs = sorted(docs, key=lambda d: d["_source"]["@timestamp"], reverse=True)
        return len(docs), docs[:size]


def test_a_busy_case_host_still_reads_its_newest_events_first():
    at = datetime.fromisoformat(AT)
    older = [
        _winlog(
            f"o{i}",
            "C:\\Windows\\explorer.exe",
            f"C:\\tool-{i}.exe",
            at=(at - timedelta(seconds=2, milliseconds=i)).isoformat(),
        )
        for i in range(sr.MAX_ATTACK + 5)
    ][::-1]
    cluster = _Cluster(older + ATTACK)
    source = sr.ElasticSource(cluster, "logs-*", 30, "elastic-siem")
    source.spend = lambda what: True

    out = _run(CLEAN, [source])

    assert out["hits"] == 2 and out["siems"][0]["attack_cut_short"] is True
    assert all(a["sort"] == sr.NEWEST_FIRST for a in cluster.asked)


def test_a_rule_reading_event_id_finds_winlogbeats_event_id_field():
    paths = {
        clause["exists"]["field"]
        for clause in sr._holding(["EventID"])["bool"]["should"]
    }
    assert {"*.event_id", "*.event-id", "*.eventID", "*.EventID"} <= paths


class _Splunk:
    def __init__(self):
        self.spl = []

    def search(self, spl, **_):
        self.spl.append(spl)
        return type("Found", (), {"results": []})()


def test_splunk_reads_the_events_a_person_named():
    service = _Splunk()
    source = sr.SplunkSource(service, "main", 30)
    asyncio.run(source.attack(None, ["main~3~G:7:42", "9:1"], ["Image"], START, NOW))
    (spl,) = service.spl
    assert spl.startswith("search index=main | eval _named=")
    assert '_named="main~3~G:7:42"' in spl and '_cd="9:1"' in spl
