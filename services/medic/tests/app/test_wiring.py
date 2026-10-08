"""S4 wiring: observations → engine (15 s tick) → router R1 → store, end to end.

E3 vectors run through the app path, not only the engine: what lands in the
store must be the incidents the vector expects, routed and chained.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.medic.contracts.vector_expand import expand, load, secs
from services.medic.store import verify_store
from services.medic.tests.app.replay import replay
from services.medic.tests.engine.runner import CONTRACTS, Run, group_key, rules_of
from services.medic.tests.store.chains import stored

INSTANCE = "mi_0123456789abcdef"
VECTORS = CONTRACTS / "vectors"


def _start(vector: dict) -> datetime:
    return datetime.strptime(vector["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _replay_vector(name: str, data_dir: Path) -> tuple[dict, list[dict]]:
    vector = load(next(VECTORS.glob(f"{name}-*.yaml")))
    replay(
        expand(vector),
        rules_of(vector),
        data_dir,
        instance_id=INSTANCE,
        start=_start(vector),
        seconds=secs(vector["end"]),
    )
    return vector, stored(data_dir)


@pytest.mark.parametrize("name", ["v01", "v02"])
def test_vector_through_the_app_path(tmp_path: Path, name: str) -> None:
    vector, records = _replay_vector(name, tmp_path / "store")
    engine_only = [{k: r[k] for k in ("type", "at", "body")} for r in records]
    got = Run(engine_only, {}, _start(vector)).incidents()

    want = vector["incidents"]
    assert len(got) == len(want)
    for label, inc in want.items():
        match = [
            g
            for g in got.values()
            if g["rule"] == inc["rule"]
            and g["active_since"] == secs(inc["active_since"])
            and group_key(g.get("group", {})) == group_key(inc.get("group", {}))
        ]
        assert len(match) == 1, label
        for field, value in inc.items():
            if isinstance(value, str) and value.startswith("+"):
                value = secs(value)
            assert match[0].get(field) == value, (label, field)
    assert verify_store(tmp_path / "store").ok


def test_v01_incident_is_routed_on_its_rules_lane(tmp_path: Path) -> None:
    vector, records = _replay_vector("v01", tmp_path / "store")
    (opened,) = [r for r in records if r["type"] == "incident_opened"]
    (rule,) = rules_of(vector)
    assert opened["body"]["lane"] == {"value": rule.rule["lane"], "reason": "rule"}
    assert opened["body"]["route"] == "routed"
    assert [r["type"] for r in records] == [
        "incident_opened",
        "incident_updated",
        "incident_resolved",
    ]
    # A replay store is engine records only, starting at seq 0 (S0-2 (a)).
    assert [r["seq"] for r in records] == [0, 1, 2]


def test_v02_blip_writes_nothing(tmp_path: Path) -> None:
    _, records = _replay_vector("v02", tmp_path / "store")
    assert records == []


@pytest.mark.parametrize("name", ["v01", "v10", "v15"])
def test_replay_twice_gives_byte_identical_stores(tmp_path: Path, name: str) -> None:
    # S0-2 (a): "byte-identical" means the whole replay store.
    first, second = tmp_path / "a", tmp_path / "b"
    _replay_vector(name, first)
    _replay_vector(name, second)
    assert stored(first), "the vector should write something"
    assert sorted(p.name for p in first.iterdir()) == sorted(
        p.name for p in second.iterdir()
    )
    for path in first.iterdir():
        if path.is_file() and path.name != "medic.lock":
            assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_a_store_that_refuses_doesnt_stop_the_engine(tmp_path: Path, caplog) -> None:
    # C5: the loop keeps going whatever state the store is in; a lost record is
    # counted and logged, and the next one is still offered.
    from services.medic.app.wiring import TICK_S, Medic
    from services.medic.store import StoreBusy, open_writer
    from services.medic.tests.app.replay import seed_instance
    from services.medic.tests.fakes import FakeClock

    vector = load(next(VECTORS.glob("v01-*.yaml")))
    start, observations = _start(vector), expand(vector)
    seed_instance(tmp_path, INSTANCE)
    clock = FakeClock(wall=start.timestamp())
    with open_writer(tmp_path) as writer:
        medic = Medic(
            writer=writer,
            rules=rules_of(vector),
            sensors=[],
            clock=clock,
            shape="compose",
        )
        real_append, calls = writer.append, []

        def busy_once(record: dict) -> dict:
            calls.append(record["type"])
            if len(calls) == 1:
                raise StoreBusy("another connection holds the write lock")
            return real_append(record)

        writer.append = busy_once
        fed = 0
        for t in range(0, secs(vector["end"]) + 1, TICK_S):
            clock.advance(start.timestamp() + t - clock.wall())
            while fed < len(observations) and (
                datetime.fromisoformat(observations[fed]["observed_at"]).timestamp()
                <= clock.wall()
            ):
                medic.sink.write(observations[fed])
                fed += 1
            medic.evaluate()
    assert calls == ["incident_opened", "incident_updated", "incident_resolved"]
    assert medic.unstored == 1
    assert "not stored" in caplog.text
    assert [r["type"] for r in stored(tmp_path)] == [
        "incident_updated",
        "incident_resolved",
    ]
