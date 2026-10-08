"""Part-2 behaviour the vectors don't pin on their own: restarts in the middle of
flapping, suppression, upgrade windows and retirement; the routing pass; the
upgrade detector; the group cap's tie-break."""

from __future__ import annotations

import pytest

from services.medic.contracts.vector_expand import load
from services.medic.engine import Engine, suppress
from services.medic.engine.upgrade import UpgradeWatch
from services.medic.tests.engine.runner import CONTRACTS, INSTANCE, group_key, run

VECTORS = CONTRACTS / "vectors"


@pytest.mark.parametrize(
    "name, at, refeed",
    [
        ("v13-incident-flapping", "+30m", True),  # between the 2nd and 3rd opening
        ("v13-incident-flapping", "+50m", True),  # flapping, resolving
        ("v16-group-overflow", "+7m", True),  # overflow member, not yet true
        ("v18-suppression-routing-hold", "+52m", True),  # child held, parent pending
        ("v19-suppression-child-outlives-parent", "+68m", True),  # freed, waiting
        ("v20-upgrade-window", "+25m", True),  # inside the window
        ("v23-group-retired", "+23h", False),  # coverage run must be in the state
        ("v25-retire-resolving-and-return", "+20h", False),
    ],
)
def test_part2_state_survives_a_restart(name: str, at: str, refeed: bool) -> None:
    vector = load(VECTORS / f"{name}.yaml")
    want = run(vector).incidents()
    vector["engine_restarts"] = [at]
    assert run(vector, refeed_history=refeed).incidents() == want


def test_cap_ties_go_by_label_values() -> None:
    """Three new groups on one tick with a cap of 2: the two lowest values get
    their own groups, the third joins the overflow group (§3)."""
    vector = load(VECTORS / "v16-group-overflow.yaml")
    vector["series"][2].pop("from")
    vector["series"][2]["steps"] = [["+0s", 0], ["+10m", 5]]
    for s, source in zip(vector["series"], ("c", "b", "a"), strict=True):
        s["labels"] = {"source": source}
    result = run(vector)
    rows = {k[2] for k in result.status if k[:2] == (600, "ingest.source-erroring")}
    assert rows == {
        group_key({"source": "a"}),
        group_key({"source": "b"}),
        group_key({"source": "__overflow__"}),
    }


def test_an_overflow_member_retires_but_the_overflow_group_stays() -> None:
    """With its last member retired, the overflow group is false (any of nothing):
    its incident clears on the next tick, and the group stays for a newcomer."""
    vector = load(VECTORS / "v16-group-overflow.yaml")
    vector["end"] = "+24h15m"
    vector["series"][2]["to"] = "+10m"  # c, the only member, goes quiet
    vector["expect"].append({"at": "+24h15m"})  # the runner keeps status there
    result = run(vector)
    over = group_key({"source": "__overflow__"})
    late = result.status[(24 * 3600 + 15 * 60, "ingest.source-erroring", over)]
    assert (late["eval"], late["state"]) == ("false", "inactive")
    ended = [i for i in result.incidents().values() if i["resolved_at"]]
    assert [(i["group"], i["resolved_at"], i["reason"]) for i in ended] == [
        ({"source": "__overflow__"}, 24 * 3600 + 10 * 60 + 15, "cleared")
    ]


def _open(rule: str, fault: dict | None, m: dict, value=True, **group) -> suppress.Open:
    base = {"state": "firing", "opened_at": 0, "hold_end": 300, "routed_at": None}
    return suppress.Open(rule, fault, group, base | m, value)


LLM = {"class": "llm", "mode": "L-5", "causes": ["L-5.a"]}
P4 = {"class": "pipeline", "mode": "P-4", "causes": ["P-4.a"]}
ENTRY = {"parent": {"class": "llm"}, "children": {"mode": "P-4"}}


def test_match_labels_must_agree_for_suppression() -> None:
    entry = ENTRY | {"match": ["source"]}
    parent = _open("llm.x", LLM, {"incident_id": "inc_parent00"}, source="a")
    child = _open("pipe.y", P4, {"incident_id": "inc_child000"}, source="b")
    out = suppress.route(400, [parent, child], [entry], False, "blind")
    assert [f["change"] for _, f in out] == ["routed", "routed"]
    assert not child.m.get("suppressed_by")


def test_a_routed_child_is_not_suppressed_later() -> None:
    child = _open("pipe.y", P4, {"incident_id": "inc_child000", "routed_at": 300})
    parent = _open("llm.x", LLM, {"incident_id": "inc_parent00", "opened_at": 400})
    assert suppress.route(400, [parent, child], [ENTRY], False, "blind") == []


def test_a_freed_child_waits_while_unknown_and_routes_once_true() -> None:
    m = {"incident_id": "inc_child000", "suppressed_by": "inc_parent00"}
    child = _open("pipe.y", P4, m | {"freed_at": 1000}, value=None)
    assert suppress.route(1600, [child], [ENTRY], False, "blind") == []
    assert not child.m.get("quiet")
    child.value = True
    out = suppress.route(1615, [child], [ENTRY], False, "blind")
    assert [f["change"] for _, f in out] == ["unsuppressed", "routed"]


def test_a_child_resolving_when_freed_closes_quietly_even_if_it_refires() -> None:
    """§6.3: the decision is made 10 min after the last parent resolved."""
    m = {"incident_id": "inc_child000", "suppressed_by": "inc_parent00"}
    child = _open("pipe.y", P4, m | {"freed_at": 1000, "state": "resolving"}, False)
    assert suppress.route(1600, [child], [ENTRY], False, "blind") == []
    child.m["state"], child.value = "firing", True  # refires later
    assert suppress.route(1700, [child], [ENTRY], False, "blind") == []


def test_nothing_routes_inside_an_upgrade_window() -> None:
    plain = _open("llm.x", LLM, {"incident_id": "inc_parent00", "opened_at": 100})
    assert suppress.route(400, [plain], [], True, "blind") == []
    assert suppress.route(415, [plain], [], False, "blind") != []


def test_sensor_blind_is_never_a_child() -> None:
    blind = _open("blind", None, {"incident_id": "inc_blind000"})
    parent = _open("llm.x", LLM, {"incident_id": "inc_parent00"})
    entry = {"parent": {"class": "llm"}, "children": {"rule": "blind"}}
    suppress.route(10, [parent, blind], [entry], False, "blind")
    assert not blind.m.get("suppressed_by")


@pytest.mark.parametrize(
    "entry",
    [
        {"parent": {"class": "llm"}},
        {"parent": {"class": "llm", "mode": "L-5"}, "children": {"mode": "P-4"}},
        {"parent": {"tier": "x"}, "children": {"mode": "P-4"}},
        ENTRY | {"match": "source"},
    ],
)
def test_a_malformed_suppression_entry_is_refused(entry: dict) -> None:
    with pytest.raises(ValueError):
        Engine([], instance_id=INSTANCE, install_shape="helm", suppression=[entry])


def _sample(service: str, t: int, *, epoch=None, signal="s", values=()) -> dict:
    obs = {"kind": "sample", "outcome": "ok", "signal": signal, "values": list(values)}
    obs["target"] = {"service": service}
    return obs | ({"epoch": epoch} if epoch else {})


def _value(key: str, value) -> dict:
    return {"key": key, "state": "present", "value": value}


def test_restarts_of_two_services_within_5_min_open_a_window() -> None:
    w = UpgradeWatch(None)
    w.observe(_sample("backend", 0, epoch="e1", signal="a"), 0)
    w.observe(_sample("soc-daemon", 0, epoch="e1", signal="b"), 0)
    w.observe(_sample("backend", 0, epoch="e2", signal="a"), 100)
    assert not w.active(100)  # one service restarting is not an upgrade
    w.observe(_sample("soc-daemon", 0, epoch="e2", signal="b"), 350)
    assert w.active(350) and w.until == 350 + 900


def test_restart_markers_that_dont_open_a_window() -> None:
    w = UpgradeWatch(None)
    for t, up in ((0, 50), (60, 110)):  # uptime rising: no restart
        w.observe(_sample("backend", t, values=[_value("uptime_seconds", up)]), t)
    w.observe(_sample("medic", 0, epoch="e1"), 0)
    w.observe(_sample("medic", 0, epoch="e2"), 70)  # Medic's own restart: not Vigil's
    w.observe(_sample("soc-daemon", 0, values=[_value("x.started_at", "1")]), 0)
    w.observe(_sample("soc-daemon", 0, values=[_value("x.started_at", "2")]), 80)
    assert not w.active(80)
    w.observe(_sample("backend", 90, values=[_value("uptime_seconds", 5)]), 90)
    assert w.active(90)  # backend's uptime dropped within 5 min of soc-daemon's


def test_a_late_version_read_doesnt_flip_the_version() -> None:
    w = UpgradeWatch(None)
    w.observe(_sample("backend", 0, signal="version", values=[_value("v", "1")]), 0)
    w.observe(_sample("backend", 0, signal="version", values=[_value("v", "1")]), 60)
    w.observe(_sample("backend", 0, signal="version", values=[_value("v", "0")]), 30)
    assert not w.active(60)


def test_a_blind_period_restarts_the_24_hours() -> None:
    """v26's own incident, checked while v26 waits on ⚑ S4b2-1 (it omits the
    sensor-blind incident of the blind period, which §2 requires)."""
    result = run(load(VECTORS / "v26-blind-restarts-retirement.yaml"))
    got = sorted(
        (i["rule"], i["opened_at"], i["resolved_at"], i["reason"])
        for i in result.incidents().values()
    )
    assert got == [
        ("ingest.source-erroring", 0, 36 * 3600 + 30 * 60, "group_retired"),
        ("watcher.sensor-blind", 12 * 3600 + 3 * 60, 12 * 3600 + 30 * 60, "cleared"),
    ]
