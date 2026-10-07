"""Default rows for the tables that only create_all builds.

``05_case_management_extended.sql`` holds the default SLA policies and case
templates, and no SQL file creates their tables. compose's db-seed and
``scripts/seed_reference_data.py`` apply it after create_all. The Helm db-init
Job cannot: it runs before the backend can log in to create the tables, and
then records the file as applied, so a Helm install never got either. The
backend therefore seeds each table the file writes when it finds it empty.
"""

import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_FILE = "05_case_management_extended.sql"
# A checkout keeps the SQL under infra/; the backend and daemon images copy it
# to /app/database/init (infra/docker/Dockerfile.backend).
SEED_DIRS = (Path("infra") / "database" / "init", Path("database") / "init")

# psql-style client directives the SQL files may carry; the driver rejects them.
_SKIP_PREFIXES = ("\\", "\\connect", "\\c ")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_0-9]*\$")
# The target table, schema-qualified or quoted or neither, then the column list
# when there is one.
INSERT_TARGET = re.compile(
    r"INSERT\s+INTO\s+([\w.\"]+)\s*(?:\(([^)]*)\))?", re.IGNORECASE
)


def split_statements(sql: str) -> Iterator[str]:
    """Split on top-level ';', treating one inside a single-quoted string or a
    dollar-quoted ($tag$) body as literal so PL/pgSQL bodies stay whole.
    Comments are dropped: an apostrophe in one ("don't") would otherwise open
    a string and merge the statements after it."""
    buf: list[str] = []
    i, n = 0, len(sql)
    in_squote = False
    dollar_tag = None
    while i < n:
        if dollar_tag is None and not in_squote:
            if sql.startswith("--", i):
                end = sql.find("\n", i)
                i = n if end == -1 else end
                continue
            if sql.startswith("/*", i):
                # Postgres block comments nest.
                depth, i = 1, i + 2
                while i < n and depth:
                    if sql.startswith("/*", i):
                        depth, i = depth + 1, i + 2
                    elif sql.startswith("*/", i):
                        depth, i = depth - 1, i + 2
                    else:
                        i += 1
                buf.append(" ")
                continue
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


def find_seed_file(root: Path = REPO_ROOT) -> Optional[Path]:
    for directory in SEED_DIRS:
        path = root / directory / SEED_FILE
        if path.is_file():
            return path
    return None


def target_table(match: "re.Match[str]") -> str:
    """The table an ``INSERT_TARGET`` match names, without schema or quotes."""
    return match.group(1).replace('"', "").split(".")[-1]


def inserts_by_table(sql: str) -> Dict[str, List[str]]:
    """The statements that are INSERTs, grouped by target table in file order."""
    tables: Dict[str, List[str]] = {}
    for statement in split_statements(sql):
        match = INSERT_TARGET.match(statement)
        if match:
            tables.setdefault(target_table(match), []).append(statement)
    return tables


def seed_empty_tables(
    conn: Connection, sql: Optional[str] = None
) -> Tuple[Dict[str, int], Dict[str, str]]:
    """Run the seed's INSERTs into each of its tables that has no rows yet.

    A table with any row is left alone, so a default an operator deleted stays
    deleted. The file's ON CONFLICT DO NOTHING covers two processes seeding one
    empty table at once. Each table has its own savepoint, so a table that
    fails leaves the others seeded.

    Returns the rows inserted per seeded table, and the error per failed one.
    """
    if sql is None:
        path = find_seed_file()
        if path is None:
            raise FileNotFoundError(
                f"{SEED_FILE} is not under {REPO_ROOT} in any of "
                + ", ".join(str(d) for d in SEED_DIRS)
            )
        sql = path.read_text(encoding="utf-8")
    quote = conn.dialect.identifier_preparer.quote
    inserted: Dict[str, int] = {}
    failed: Dict[str, str] = {}
    for table, statements in inserts_by_table(sql).items():
        savepoint = conn.begin_nested()
        try:
            has_rows = conn.execute(
                text(f"SELECT EXISTS (SELECT 1 FROM {quote(table)})")
            ).scalar()
            if not has_rows:
                inserted[table] = sum(
                    conn.execute(text(statement)).rowcount for statement in statements
                )
            savepoint.commit()
        except SQLAlchemyError as e:
            savepoint.rollback()
            inserted.pop(table, None)
            message = str(getattr(e, "orig", e)).splitlines()
            failed[table] = message[0] if message else type(e).__name__
    return inserted, failed
