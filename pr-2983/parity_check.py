import pytest
pytestmark = [pytest.mark.unit, pytest.mark.database, pytest.mark.external_service]
import copy
from datetime import datetime, timezone
from sqlalchemy import select
from core.memory import learning_episodes as le
from core.storage.models import EpisodicVerdictSource
from tests.unit.memory.test_learning_episodes import verdict

T = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def _old_attach(db, verdicts_by_id):
    if not verdicts_by_id:
        return
    sources = EpisodicVerdictSource.__table__
    stmt = (
        select(sources)
        .where(sources.c.verdict_id.in_(list(verdicts_by_id)))
        .order_by(sources.c.verdict_id, sources.c.source_system)
    )
    for row in db.execute(stmt).mappings():
        verdicts_by_id[row["verdict_id"]]["sources"].append(
            {k: row[k] for k in ("source_system", "stance", "source_tier")}
        )


def test_parity(episodic_session):
    db = episodic_session
    verdict(db, "hunt", "a", ["ip:1.2.3.4"], concluded=T,
            sources=[("zeek", "weakens"), ("crowdstrike", "supports"), ("Zeta_Upper", "neither"), ("splunk", "neither")])
    verdict(db, "hunt", "c", ["ip:1.2.3.5"], concluded=T)
    verdict(db, "case", "b", ["host:x"], concluded=T,
            sources=[(f"s{i:03d}", "supports") for i in range(300)])
    db.flush()
    env = {(k, i): {"payload": {"verdicts": [], "gaps": []}} for k, i in [("hunt", "a"), ("case", "b"), ("hunt", "c"), ("hunt", "none")]}
    new = copy.deepcopy(env)
    le._payloads(db, new)
    old = copy.deepcopy(env)
    old_by_id = {}
    real = le.attach_sources
    le.attach_sources = lambda s, by_id: (old_by_id.update(by_id), _old_attach(s, by_id))
    try:
        le._payloads(db, old)
    finally:
        le.attach_sources = real
    for k in env:
        print(k, [[s["source_system"] for s in v["sources"]][:5] + [len(v["sources"])] for v in new[k]["payload"]["verdicts"]])
    assert new == old
    assert sum(len(v["sources"]) for e in new.values() for v in e["payload"]["verdicts"]) == 304
    empty = {}
    le._payloads(db, empty)
    assert empty == {}
    print("PARITY OK")
