#!/usr/bin/env python3
"""Seed reference data (roles, SLA policies, case templates, …) after the
schema exists.

``database/init/*.sql`` is mounted at the Postgres initdb step, which runs
*before* the backend's ``create_all`` builds the tables the data-only files
target — so initdb aborts partway (ON_ERROR_STOP) and the seed rows never land.
Re-applying the same files here, after ``init_schema.py``, gets the roles the
first-run bootstrap needs plus the rest of the reference data. No default admin
is seeded (that row was removed from ``06_auth_tables.sql``); the operator
creates it via ``/api/auth/bootstrap``.

Every file is idempotent (``IF NOT EXISTS`` / ``ON CONFLICT DO NOTHING``), so
this is safe to re-run, and failures are tolerated per statement the way the
Helm db-init Job does — a file whose target a given release doesn't define must
not stop the rest.
"""

import logging
import re
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text  # noqa: E402
from database.connection import init_database, get_db_manager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("seed_reference_data")

# psql-style client directives the SQL files may carry; the driver rejects them.
_SKIP_PREFIXES = ("\\", "\\connect", "\\c ")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_0-9]*\$")


def _statements(sql: str):
    """Split on top-level ';', treating one inside a single-quoted string or a
    dollar-quoted ($tag$) body as literal so PL/pgSQL bodies stay whole."""
    buf: list[str] = []
    i, n = 0, len(sql)
    in_squote = False
    dollar_tag = None
    while i < n:
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_squote:
            if sql[i] == "'":
                in_squote = False
        elif sql[i] == "'":
            in_squote = True
        elif sql[i] == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        elif sql[i] == ";":
            yield from _emit("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(sql[i])
        i += 1
    yield from _emit("".join(buf))


def _emit(stmt: str):
    s = stmt.strip()
    if s and not s.startswith(_SKIP_PREFIXES):
        yield s


def main() -> int:
    init_dir = project_root / "database" / "init"
    files = sorted(init_dir.glob("*.sql"))
    if not files:
        logger.error("no SQL files in %s", init_dir)
        return 1

    # Bring up the engine in this process (create_all already ran separately).
    init_database(echo=False, create_tables=False)
    engine = get_db_manager().engine
    if engine is None:
        logger.error("database engine unavailable")
        return 1
    for f in files:
        applied = 0
        for stmt in _statements(f.read_text()):
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                applied += 1
            except Exception:
                # Expected for statements whose target a release doesn't define.
                pass
        logger.info("seeded %s (%d statements)", f.name, applied)

    logger.info("reference data seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
