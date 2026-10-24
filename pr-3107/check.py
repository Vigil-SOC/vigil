import asyncio, contextlib, json, os, uuid
from unittest import mock
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from core.storage.models import FederationSource

base = f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}"
db = "fed3085_" + uuid.uuid4().hex[:8]
admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
with admin.connect() as c: c.execute(text(f"CREATE DATABASE {db}"))
eng = create_engine(base + "/" + db)
FederationSource.__table__.create(eng)
S = sessionmaker(eng)

class Mgr:
    @contextlib.contextmanager
    def session_scope(self):
        s = S()
        try:
            yield s; s.commit()
        except Exception:
            s.rollback(); raise
        finally:
            s.close()

class A:
    def __init__(s, name, interval, configured): s.name, s._i, s._c = name, interval, configured
    def is_configured(s):
        if s._c is None: raise RuntimeError("bad config")
        return s._c
    def default_interval(s): return s._i

ADS = {"conf": A("conf", 60, True), "unconf": A("unconf", 900, False), "broken": A("broken", 120, None)}
builds = {"n": 0}
def get_adapter(n):
    builds["n"] += 1
    return ADS.get(n)

def dbrows():
    with eng.connect() as c:
        return {r.source_id: dict(enabled=r.enabled, interval_seconds=r.interval_seconds, max_items=r.max_items, min_severity=r.min_severity, cursor=r.cursor, consecutive_errors=r.consecutive_errors, dropped_total=r.dropped_total) for r in c.execute(text("select * from federation_sources order by source_id"))}

try:
    with mock.patch("core.storage.connection.get_db_manager", return_value=Mgr()), \
         mock.patch("core.federation.registry.list_adapters", return_value=list(ADS.values())), \
         mock.patch("core.federation.seed.list_adapters", return_value=list(ADS.values())), \
         mock.patch("core.federation.registry.get_adapter", side_effect=get_adapter), \
         mock.patch("services.api.routers.federation.source_collection_lag", return_value=[]):
        from core.federation.seed import seed_federation_sources
        from services.api.routers import federation as r
        from fastapi import HTTPException
        print("seed returns", seed_federation_sources())
        print("db after seed", json.dumps(dbrows()))
        out = asyncio.run(r.list_sources())
        for s in out["sources"]: print("GET", json.dumps(s, default=str, sort_keys=True))
        builds["n"] = 0
        print("PATCH unconf enabled", json.dumps(asyncio.run(r.patch_source("unconf", r.FederationSourcePatch(enabled=True))), default=str, sort_keys=True), "get_adapter calls:", builds["n"])
        print("PATCH broken max_items=5", json.dumps(asyncio.run(r.patch_source("broken", r.FederationSourcePatch(max_items=5))), default=str, sort_keys=True))
        print("PATCH conf interval=30 (existing row)", json.dumps(asyncio.run(r.patch_source("conf", r.FederationSourcePatch(interval_seconds=30))), default=str, sort_keys=True))
        try: asyncio.run(r.patch_source("nope", r.FederationSourcePatch(enabled=True)))
        except HTTPException as e: print("PATCH nope ->", e.status_code, e.detail)
        print("db final", json.dumps(dbrows()))
        d = dbrows()
        exp = dict(min_severity=None, cursor={}, consecutive_errors=0, dropped_total=0)
        assert d["unconf"] == dict(enabled=True, interval_seconds=900, max_items=100, **exp), d["unconf"]
        assert d["broken"] == dict(enabled=False, interval_seconds=120, max_items=5, **exp), d["broken"]
        assert d["conf"] == dict(enabled=False, interval_seconds=30, max_items=100, **exp), d["conf"]
        print("ALL ASSERTS PASSED")
finally:
    eng.dispose()
    with admin.connect() as c: c.execute(text(f"DROP DATABASE {db}"))
