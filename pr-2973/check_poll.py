"""Drive the real pending()/write_distil against a fresh DB built from init SQL."""
import sys, uuid
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.memory.distil import (DISTIL_MAPPING_VERSION, SUPPORTED_SCHEMA_VERSION,
                                Terminal, pending, write_distil)

repo = Path(sys.argv[1]); url = sys.argv[2]
admin = create_engine(url + "/deeptempo_test", isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.execute(text("DROP DATABASE IF EXISTS pr2892check WITH (FORCE)"))
    c.execute(text("CREATE DATABASE pr2892check"))
eng = create_engine(url + "/pr2892check")
with eng.connect() as c:
    for n in ("19_agent_ledger.sql", "31_agent_ledger_hash_chain.sql",
              "26_episodic_memory.sql", "29_episodic_distil_failures.sql"):
        c.execute(text((repo / "infra/database/init" / n).read_text()))
    c.commit()

def term(run, seq):
    with eng.begin() as c:
        c.execute(text("INSERT INTO agent_events (run_id, seq, run_kind, kind, payload, schema_version)"
                       " VALUES (:r, :s, 'hunt', 'terminal', '{}'::jsonb, 1)"), {"r": str(run), "s": seq})

def offered():
    with Session(eng) as s:
        return {(t.run_id, t.seq) for t in pending(s, limit=200)}

def distil(run, seq, hunt, at):
    with Session(eng) as s:
        out = write_distil(s, Terminal(run, seq), {"distil_schema_version": SUPPORTED_SCHEMA_VERSION,
                     "investigation_id": hunt, "concluded_at": at, "outcome": "aborted"})
        s.commit()
        return out

def marker(hunt):
    with eng.connect() as c:
        return tuple(c.execute(text("SELECT origin_run_id, origin_seq, distil_version FROM episodic_distil_markers"
                                    " WHERE investigation_id = :h"), {"h": hunt}).one())

def check(label, got, want):
    print(f"{'OK  ' if got == want else 'FAIL'} {label}: {got!r}")
    if got != want: sys.exit(1)

a, b = uuid.uuid4(), uuid.uuid4()
term(a, 10); term(b, 5)
check("both runs offered before any marker", offered(), {(a, 10), (b, 5)})
check("first write derives", distil(a, 10, "hunt-a", "2026-10-01T00:00:00Z")["derived"], 1)
check("marker names run a, seq 10", marker("hunt-a"), (a, 10, DISTIL_MAPPING_VERSION))
check("run a no longer offered, b still is", offered(), {(b, 5)})
check("rewrite of the same terminal derives again", distil(a, 10, "hunt-a", "2026-10-01T00:00:00Z")["derived"], 1)
check("still only b offered after rewrite", offered(), {(b, 5)})
term(a, 20)
check("resumed run a offered at its later terminal seq 20", offered(), {(a, 20), (b, 5)})
distil(a, 20, "hunt-a", "2026-10-02T00:00:00Z")
check("marker moved to seq 20", marker("hunt-a"), (a, 20, DISTIL_MAPPING_VERSION))
check("run a settled after resume", offered(), {(b, 5)})
with eng.begin() as c:
    c.execute(text("UPDATE episodic_distil_markers SET distil_version = distil_version - 1"))
check("version bump re-offers run a", offered(), {(a, 20), (b, 5)})
with eng.connect() as c:
    cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns"
                                         " WHERE table_name = 'episodic_distil_markers'"))}
check("init SQL has no origin_run_ids column", "origin_run_ids" in cols, False)
print("ALL OK")
