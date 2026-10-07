"""Freezing the episodic tier for an eval run (#736).

An eval that reads live memory cannot tell you what it measured. The Distil
polls, so a run that ended Monday can be written Wednesday carrying Monday's
``concluded_at``: re-run the same hunts a week apart and the corpus underneath
them has grown, which moves the score without anyone touching the code. That is
also why this is a copy and not an as-of filter over the live tables -- rows
carrying old dates keep arriving, so a filter that looks frozen is not.

A snapshot is a Postgres schema holding a copy of the episodic tables. Nothing
here is wired into recall: the eval process selects a snapshot by putting it on
its ``search_path``, and every query in ``core/memory/recall.py`` then reads the
copy unchanged.

**How a process selects one.** Through ``PGOPTIONS`` in its environment, which
libpq reads for any parameter the connection string does not set::

    PGOPTIONS='-c search_path=episodic_snap_2026_09_01,public'

Not through the DSN: ``core/storage/connection.py`` allows four connection
parameters and ``options`` is not among them, so a snapshot named in the URL is
refused before libpq sees it. Not through ``SET search_path`` on a borrowed
connection either -- that leaks to whoever takes the connection next, including
the Distil's writes. ``PGOPTIONS`` applies at connect time, so every connection
in the pool comes up on the same schema and none of them can drift.

**The variable belongs on whichever process runs the query, which is not
always the eval.** It is read per connection, so a process holding it reads the
snapshot for its lifetime and a process without it reads live, with no shared
state between them and nothing to reset afterwards. For a Python caller of
``recall_entity`` that process is the eval itself and the story ends there.

For a hunt it does not. A worker reaches recall over ``/internal/tools/invoke``
(``core/agents/tool_registry.py``), so the query runs in the long-lived backend
and ``PGOPTIONS`` on the eval runner changes nothing. Setting it on that backend
would work and is the wrong thing to do: the Distil writes through the same
connections, so the backend would start distilling into the snapshot. What that
case needs is a second backend that reads a snapshot and runs no Distil, which
this does not build -- so a hunt-driven eval is not frozen by anything here yet.

**There is no snapshot catalog.** ``pg_namespace`` already lists schemas and
carries a comment per schema, so the creation stamp lives there and
:func:`list_snapshots` reads it. A table recording which schemas exist would be a
second answer to a question Postgres already answers, and the two would diverge.

**Every episodic table is created, even the ones left empty.** ``search_path``
falls through: a table absent from the snapshot resolves against ``public``
instead, silently, and the eval reads -- or writes -- live rows believing they
came from the copy. Discovering the tables by prefix rather than listing them
means a table added to the tier later is created empty here rather than becoming
a hole that points at production.

**This freezes the exact-join tier and nothing else.** Narrative search's
corpus lives outside Postgres, so a snapshot does not cover it: "we froze
memory" is true of this half only, and an eval that leans on narrative recall is
not frozen by anything here.

**Each snapshot owns its sequences.** ``CREATE TABLE ... (LIKE ... INCLUDING
ALL)`` copies a ``bigserial`` column's default as it stands, which still points
at the original sequence -- so an eval writing its read journal into a snapshot
would advance the live tier's counter. The defaults are repointed at sequences
inside the schema, which is what makes a snapshot read-only with respect to
live in fact and not just by intention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.storage.unit_of_work import unit_of_work

LIVE_SCHEMA = "public"
SNAPSHOT_PREFIX = "episodic_snap_"


def _like(prefix: str) -> str:
    """A LIKE pattern matching everything under a prefix.

    An unescaped underscore is a single-character wildcard, and both prefixes
    here are full of them.
    """
    return prefix.replace("_", r"\_") + "%"


# The tier's own prefix, which is how the tables are found.
_TABLE_LIKE = _like("episodic_")

# Whose rows are copied. The order is the insert order and it is load-bearing:
# verdict_sources carries a foreign key into verdicts, so verdicts is written
# first. Everything else the discovery finds -- the read log, the Distil's
# markers and failures -- is created empty, because a journal of reads and a
# record of what the Distil has processed are not things a hunt recalls.
CORPUS_TABLES: Tuple[str, ...] = (
    "episodic_sightings",
    "episodic_verdicts",
    "episodic_verdict_sources",
    "episodic_gaps",
)

# No hyphens and no leading punctuation, so the schema name needs no quoting
# anywhere -- including inside PGOPTIONS, where a quoted identifier would have
# to survive a shell as well. A name may start with a digit because the prefix
# does not: `episodic_snap_2026_09_01` is a bare identifier either way.
_NAME = re.compile(r"^[a-z0-9][a-z0-9_]*$")

# Postgres truncates an identifier at 63 bytes, which would silently collide two
# snapshots whose names differ only past the limit.
_MAX_NAME = 63 - len(SNAPSHOT_PREFIX)


def _schema_for(name: str) -> str:
    """The schema a snapshot name lives in. One definition, five callers."""
    return SNAPSHOT_PREFIX + name


class SnapshotError(RuntimeError):
    """A snapshot could not be created, found or dropped."""


class SnapshotExists(SnapshotError):
    """The name is taken.

    Snapshots are kept rather than overwritten: an old snapshot read by new code
    measures the code, and a new snapshot read by new code measures the system.
    Overwriting one silently retires the first of those.
    """


@dataclass(frozen=True)
class Snapshot:
    """One frozen copy of the tier, and what it holds."""

    name: str
    created_at: Optional[datetime]
    rows: Dict[str, int]

    @property
    def schema(self) -> str:
        """Derived, not stored: _row_counts' reasoning applies to names too."""
        return _schema_for(self.name)

    @property
    def is_empty(self) -> bool:
        """The control: the same hunts against a copy that remembers nothing."""
        return all(count == 0 for count in self.rows.values())

    @property
    def pgoptions(self) -> str:
        """What an eval process exports to read this snapshot."""
        return pgoptions_for(self.name)


def pgoptions_for(name: str) -> str:
    """The ``PGOPTIONS`` value that points a process at this snapshot.

    ``public`` stays on the path behind it: pgvector's operators and every
    non-episodic table the process touches live there, and only the episodic
    tables are shadowed by the copy.
    """
    return f"-c search_path={_schema_for(_validated(name))},{LIVE_SCHEMA}"


def create_snapshot(name: Optional[str] = None, *, empty: bool = False) -> Snapshot:
    """Copy the live episodic tier into a schema of its own.

    ``empty`` builds the same schema with the same tables and copies no rows.
    That is the control the whole exercise rests on -- the same hunts against a
    copy that remembers nothing, compared with a copy that does.

    Takes no caller session, unlike the reads below. The copy is four statements
    and its correctness is that they see one instant: under the default READ
    COMMITTED each takes its own snapshot, and the Distil is a delete-then-
    insert that polls, so a re-derive landing between the Verdicts copy and the
    Verdict Sources copy yields Verdicts whose sources are missing -- a quiet
    wrong answer, not a foreign key error. REPEATABLE READ is therefore part of
    what this function is, and it can only be set on a transaction that has not
    run a query yet, which is not something a caller's session can promise.
    """
    snapshot = _validated(name or _today())
    schema = _schema_for(snapshot)

    with unit_of_work(None) as db:
        # First statement in the transaction, which is the only place Postgres
        # accepts it.
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

        if _schema_exists(db, schema):
            raise SnapshotExists(f"snapshot {snapshot!r} already exists")

        tables = _episodic_tables(db)
        if not tables:
            raise SnapshotError(
                f"no episodic tables in {LIVE_SCHEMA}: nothing to snapshot"
            )
        missing = [table for table in CORPUS_TABLES if table not in tables]
        if missing:
            raise SnapshotError(
                "corpus tables are missing from the live tier: "
                + ", ".join(sorted(missing))
            )

        created = datetime.now(timezone.utc)
        db.execute(text(f"CREATE SCHEMA {schema}"))
        db.execute(
            text(f"COMMENT ON SCHEMA {schema} IS :stamp"),
            {"stamp": created.isoformat()},
        )

        for table in tables:
            db.execute(
                text(
                    f"CREATE TABLE {schema}.{table} "
                    f"(LIKE {LIVE_SCHEMA}.{table} INCLUDING ALL)"
                )
            )
            _own_sequences(db, schema, table)

        _copy_foreign_keys(db, schema)

        if not empty:
            for table in CORPUS_TABLES:
                db.execute(
                    text(
                        f"INSERT INTO {schema}.{table} "
                        f"SELECT * FROM {LIVE_SCHEMA}.{table}"
                    )
                )
            for table in tables:
                _advance_sequences(db, schema, table)

        counts = _row_counts(db, schema)

    return Snapshot(name=snapshot, created_at=created, rows=counts)


def list_snapshots(*, session: Optional[Session] = None) -> List[Snapshot]:
    """Every snapshot in the database, oldest name first."""
    with unit_of_work(session) as db:
        rows = (
            db.execute(
                text(
                    "SELECT n.nspname AS schema, "
                    "       obj_description(n.oid, 'pg_namespace') AS created_at "
                    "FROM pg_namespace n "
                    "WHERE n.nspname LIKE :prefix "
                    "ORDER BY n.nspname"
                ),
                {"prefix": _like(SNAPSHOT_PREFIX)},
            )
            .mappings()
            .all()
        )

        # A name this module would refuse to create is not a snapshot, whatever
        # its schema is called, and listing it as one would hand back a Snapshot
        # whose `pgoptions` cannot be built -- an accessor that raises on a row
        # the listing itself chose to return. Tolerance below that line stands:
        # a missing stamp and missing tables are states a real snapshot can be
        # in, an unusable name is not.
        named = [
            row for row in rows if _NAME.match(row["schema"][len(SNAPSHOT_PREFIX) :])
        ]
        return [
            Snapshot(
                name=row["schema"][len(SNAPSHOT_PREFIX) :],
                created_at=_stamp(row["created_at"]),
                rows=_row_counts(db, row["schema"]),
            )
            for row in named
        ]


def get_snapshot(name: str, *, session: Optional[Session] = None) -> Snapshot:
    """One snapshot by name, or :class:`SnapshotError` if there is no such thing."""
    snapshot = _validated(name)
    schema = _schema_for(snapshot)
    with unit_of_work(session) as db:
        if not _schema_exists(db, schema):
            raise SnapshotError(f"no snapshot named {snapshot!r}")
        created = db.execute(
            text(
                "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace "
                "WHERE nspname = :schema"
            ),
            {"schema": schema},
        ).scalar()
        return Snapshot(
            name=snapshot, created_at=_stamp(created), rows=_row_counts(db, schema)
        )


def drop_snapshot(
    name: str,
    *,
    lock_timeout: Optional[str] = None,
    session: Optional[Session] = None,
) -> None:
    """Delete a snapshot and everything in it.

    ``lock_timeout`` bounds the wait for the schema's locks. Anything still
    reading the snapshot holds them until its transaction ends, and
    ``DROP ... CASCADE`` waits rather than failing -- which is a hang and not an
    error, so a caller that cannot afford to wait forever says how long.
    """
    snapshot = _validated(name)
    schema = _schema_for(snapshot)
    with unit_of_work(session) as db:
        if not _schema_exists(db, schema):
            raise SnapshotError(f"no snapshot named {snapshot!r}")
        if lock_timeout is not None:
            db.execute(text("SET LOCAL lock_timeout = :wait"), {"wait": lock_timeout})
        # Only ever a name that passed _validated and carries the prefix, which
        # is what keeps this statement off `public`.
        db.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def _validated(name: str) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise SnapshotError(
            f"snapshot name {name!r} must be lowercase letters, digits and "
            "underscores, starting with a letter or digit"
        )
    if len(name) > _MAX_NAME:
        raise SnapshotError(
            f"snapshot name {name!r} is longer than {_MAX_NAME} characters"
        )
    return name


def _today() -> str:
    """The default name. Dated, because what a snapshot is is a date."""
    return datetime.now(timezone.utc).strftime("%Y_%m_%d")


def _stamp(raw: Optional[str]) -> Optional[datetime]:
    """The creation stamp off the schema comment.

    Absent or unreadable is not an error: a schema someone created by hand is
    still a snapshot, and refusing to list it would hide it.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _schema_exists(db: Session, schema: str) -> bool:
    return (
        db.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
            {"schema": schema},
        ).scalar()
        is not None
    )


def _episodic_tables(db: Session) -> Sequence[str]:
    return (
        db.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = :live AND tablename LIKE :like "
                "ORDER BY tablename"
            ),
            {"live": LIVE_SCHEMA, "like": _TABLE_LIKE},
        )
        .scalars()
        .all()
    )


def _serial_columns(db: Session, schema: str, table: str) -> Sequence[str]:
    return (
        db.execute(
            text(
                "SELECT a.attname FROM pg_attribute a "
                "WHERE a.attrelid = CAST(:relation AS regclass) "
                "  AND a.attnum > 0 AND NOT a.attisdropped "
                "  AND pg_get_serial_sequence(:relation, a.attname) IS NOT NULL "
                "ORDER BY a.attnum"
            ),
            {"relation": f"{schema}.{table}"},
        )
        .scalars()
        .all()
    )


def _sequence_name(schema: str, table: str, column: str) -> str:
    """Named in one place because two callers have to agree.

    _own_sequences creates it and _advance_sequences moves it past the copied
    rows; a disagreement between them is a setval against a sequence nobody
    reads, which fails as a duplicate key on the snapshot's first insert and
    not before.
    """
    return f"{schema}.{table}_{column}_seq"


def _own_sequences(db: Session, schema: str, table: str) -> None:
    """Repoint the copy's serial defaults at sequences inside the schema."""
    for column in _serial_columns(db, LIVE_SCHEMA, table):
        sequence = _sequence_name(schema, table, column)
        db.execute(text(f"CREATE SEQUENCE {sequence}"))
        db.execute(
            text(
                f"ALTER TABLE {schema}.{table} ALTER COLUMN {column} "
                f"SET DEFAULT nextval('{sequence}'::regclass)"
            )
        )
        db.execute(
            text(f"ALTER SEQUENCE {sequence} OWNED BY {schema}.{table}.{column}")
        )


def _advance_sequences(db: Session, schema: str, table: str) -> None:
    """Move each sequence past the rows just copied, so a later insert is unique."""
    for column in _serial_columns(db, schema, table):
        db.execute(
            text(
                f"SELECT setval('{_sequence_name(schema, table, column)}', "
                f"COALESCE((SELECT max({column}) FROM {schema}.{table}), 0) + 1, false)"
            )
        )


def _copy_foreign_keys(db: Session, schema: str) -> None:
    """Re-create the tier's foreign keys inside the copy.

    ``LIKE ... INCLUDING ALL`` carries checks, indexes and defaults but not
    foreign keys. They are read back as text and replayed with the snapshot on
    the ``search_path``, so an unqualified ``REFERENCES episodic_verdicts``
    binds to the copy.

    Both halves are pinned rather than inherited, because
    ``pg_get_constraintdef`` renders against whatever path it finds. Taking a
    snapshot from a process that already has one selected -- which is exactly
    what ``PGOPTIONS`` does, and the reason a backend running an eval would --
    renders ``REFERENCES public.episodic_verdicts`` instead, and replaying that
    gives the new snapshot a foreign key into the live tier. Silently: the
    constraint is valid, it simply points at the wrong table.

    The prior path is captured and put back, so this leaves the transaction as
    it found it whatever the caller had selected.
    """
    prior = db.execute(text("SELECT current_setting('search_path', true)")).scalar()
    _set_search_path(db, LIVE_SCHEMA)
    constraints = (
        db.execute(
            text(
                "SELECT t.relname AS table_name, c.conname AS name, "
                "       pg_get_constraintdef(c.oid) AS definition "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE c.contype = 'f' AND n.nspname = :live AND t.relname LIKE :like "
                "ORDER BY c.conname"
            ),
            {"live": LIVE_SCHEMA, "like": _TABLE_LIKE},
        )
        .mappings()
        .all()
    )

    try:
        if not constraints:
            return
        _set_search_path(db, schema)
        for row in constraints:
            db.execute(
                text(
                    f"ALTER TABLE {schema}.{row['table_name']} "
                    f"ADD CONSTRAINT {row['name']} {row['definition']}"
                )
            )
    finally:
        _set_search_path(db, prior or LIVE_SCHEMA)


def _set_search_path(db: Session, path: str) -> None:
    """Transaction-local, and through set_config so the value can be bound.

    ``SET LOCAL search_path = ...`` takes no parameter, so restoring a captured
    path would mean interpolating it back; ``set_config`` takes it as a string.
    """
    db.execute(text("SELECT set_config('search_path', :path, true)"), {"path": path})


def _row_counts(db: Session, schema: str) -> Dict[str, int]:
    """What the snapshot holds, counted rather than recorded.

    Counting on read keeps the answer true. A stored count is a second copy of
    the same fact, and it is wrong the moment anyone touches the schema.
    """
    counts: Dict[str, int] = {}
    for table in CORPUS_TABLES:
        # A schema carrying the prefix is not proof somebody built it here, and
        # a listing that raises on one hand-made schema hides every real one --
        # the same reason _stamp tolerates a missing comment. Absent counts as
        # zero rows, because that is what a reader can act on.
        #
        # This guard is also the only thing standing between an unvalidated
        # schema name and the f-string below: to_regclass takes the name as a
        # parameter and returns NULL rather than raising, so a name that could
        # not be a relation never reaches the count. Anything that weakens it
        # has to put the validation back.
        present = db.execute(
            text("SELECT to_regclass(:relation)"), {"relation": f"{schema}.{table}"}
        ).scalar()
        counts[table] = (
            int(
                db.execute(text(f"SELECT count(*) FROM {schema}.{table}")).scalar() or 0
            )
            if present is not None
            else 0
        )
    return counts
