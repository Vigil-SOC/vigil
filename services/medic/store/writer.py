"""The decision store: one SQLite file, one writer, one hash chain (C4 §6, G2).

Every write goes through one DecisionWriter, which holds `medic.lock` for the
life of the process and appends inside `BEGIN IMMEDIATE`, so nothing can slip
a record in between reading the head and writing the next one. Sealing and
verifying are the contract's own code (`contracts/decision_chain.py`), not a
copy, so the store's hashes are the contract's by construction.

Not here yet (G3): retention and purge, anchors, the size-cap incident, the
reserve row, the free-space guard, the 16 KB record cap and the corruption reset.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from services.medic.contracts import decision_chain
from services.medic.store.files import (
    DB_NAME,
    LOCK_NAME,
    StoreError,
    StoreRefused,
    create_private_file,
    load_or_create_instance_id,
    prepare_root,
)

log = logging.getLogger("services.medic")

SCHEMA_PATH = Path(decision_chain.__file__).with_name("decision-record.schema.json")
SCHEMA_VERSION = 1  # of the SQLite layout below, not of the record (that's `v`)

MIB = 2**20
GIB = 2**30
RESERVE_BYTES = 64 * MIB  # C4 §6.4; held as a filler row from G3 on
NEAR_CAP_PERCENT = 80  # C4 §6.4 step 1
JOURNAL_SIZE_LIMIT = 16 * MIB

ENVELOPE = ("seq", "prev", "hash")

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY,
    hash TEXT NOT NULL UNIQUE,
    record TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS records_append_only BEFORE UPDATE ON records
BEGIN SELECT RAISE(ABORT, 'records are append-only'); END;
"""


class StoreLocked(StoreRefused):
    """Another writer holds the store (C4 §6.7: one process per store)."""


class StoreBusy(StoreError):
    """Something outside the writer holds SQLite's write lock; nothing was written."""


class RecordRefused(StoreError):
    """The record wasn't written. `code` is stable; `where` is a JSON path or field."""

    def __init__(self, code: str, where: str, detail: str) -> None:
        super().__init__(f"{code} at {where or '$'}: {detail}")
        self.code = code
        self.where = where


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    count: int
    head_seq: int | None
    head_hash: str
    code: str | None = None
    first_bad_seq: int | None = None
    detail: str = ""

    def __str__(self) -> str:
        if self.ok:
            return f"ok: {self.count} records, head seq {self.head_seq} hash {self.head_hash}"
        return f"BROKEN: {self.code} at seq {self.first_bad_seq}: {self.detail}"


@dataclass(frozen=True)
class StoreUsage:
    """A report only: nothing is enforced or purged yet (G3)."""

    used_bytes: int
    cap_bytes: int
    reserve_bytes: int
    free_bytes: int
    state: str  # ok | near_cap | over_cap

    @property
    def free_covers_reserve(self) -> bool:
        return self.free_bytes >= self.reserve_bytes


def usage_state(used: int, cap: int) -> str:
    if used > cap:
        return "over_cap"
    if used * 100 >= cap * NEAR_CAP_PERCENT:
        return "near_cap"
    return "ok"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def _check_row(
    seq: int, hash_col: str, text: str, validator: Draft202012Validator
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    """One stored row, in the order that names tampering first: parse, own hash
    (an edit), row key and hash column (a moved row), then the schema."""
    try:
        rec = json.loads(text)
        own_hash = decision_chain.record_hash(rec)
    except (ValueError, TypeError, AttributeError):
        return {}, ("E-PARSE", "row is not a canonical JSON record")
    if not isinstance(rec, dict) or own_hash != rec.get("hash"):
        return rec, ("E-HASH", "content does not match its hash")
    if rec.get("seq") != seq:
        return rec, ("E-SEQ", f"row {seq} holds the record for seq {rec.get('seq')}")
    if rec["hash"] != hash_col:
        return rec, ("E-HASH", "the record's hash differs from its row's")
    error = best_match(validator.iter_errors(rec))
    if error is not None:
        return rec, ("E-SCHEMA", f"{error.json_path}: {error.message}")
    return rec, None


def verify_rows(
    rows: Iterable[tuple[int, str, str]], validator: Draft202012Validator
) -> VerifyReport:
    """Walk (seq, hash, record) rows in seq order; name the first bad seq.

    The row checks stop at the first bad row; the contract's verify() then
    runs on the rows before it, so whichever problem comes first wins.
    """
    records: list[dict[str, Any]] = []
    row_problem: tuple[str, int, str] | None = None
    for seq, hash_col, text in rows:
        rec, problem = _check_row(seq, hash_col, text, validator)
        if problem:
            row_problem = (problem[0], seq, problem[1])
            break
        records.append(rec)
    try:
        head = decision_chain.verify(records)
    except decision_chain.ChainError as err:
        return VerifyReport(False, len(records), None, "", err.code, err.seq, str(err))
    if row_problem:
        code, seq, detail = row_problem
        return VerifyReport(False, len(records), None, "", code, seq, detail)
    last = records[-1]["seq"] if records else None
    return VerifyReport(True, len(records), last, head)


_SELECT_ALL = "SELECT seq, hash, record FROM records ORDER BY seq"


class DecisionWriter:
    """The one object that writes the store. Thread-safe; one per process."""

    def __init__(
        self,
        root: Path,
        conn: sqlite3.Connection,
        lock_fd: int,
        instance_id: str,
        cap_bytes: int,
        validator: Draft202012Validator,
    ) -> None:
        self.root = root
        self.instance_id = instance_id
        self._conn: sqlite3.Connection | None = conn
        self._lock_fd = lock_fd
        self._cap = cap_bytes
        self._validator = validator
        self._mutex = threading.Lock()

    # -- writes ---------------------------------------------------------------------

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Seal `record` onto the chain and commit it. Returns the stored record.

        The caller supplies `v`, `at`, `type` and `body`; the writer owns `seq`,
        `prev` and `hash`, so a caller can't fork or rewind the chain.
        """
        if not isinstance(record, dict):
            raise RecordRefused("E-SCHEMA", "$", "a record is a JSON object")
        supplied = [k for k in ENVELOPE if k in record]
        if supplied:
            raise RecordRefused(
                "E-ENVELOPE", supplied[0], "the writer assigns seq, prev and hash"
            )
        if record.get("type") == "store_reset":
            raise RecordRefused(
                "E-TYPE", "type", "store_reset is written only by reset()"
            )
        return self._write(lambda last: decision_chain.seal(record, last))

    def reset(
        self,
        *,
        previous_head: dict[str, Any] | None,
        moved_aside: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Write the first record of a fresh store after corruption (C4 §6.3).

        Its seq and prev continue from `previous_head` (the moved-aside store's
        head, or None if unknown). The corruption flow itself is G3's.
        """
        body: dict[str, Any] = {
            "reason": "corruption",
            "previous_head": previous_head,
            "instance_id": self.instance_id,
        }
        if moved_aside is not None:
            body["moved_aside"] = moved_aside
        draft = {"v": 1, "at": at or utc_now(), "type": "store_reset", "body": body}

        def seal(last: dict[str, Any] | None) -> dict[str, Any]:
            if last is not None:
                raise RecordRefused(
                    "E-TYPE", "type", "store_reset only starts a fresh, empty store"
                )
            return decision_chain.seal(draft, previous_head)

        return self._write(seal)

    def _write(self, seal) -> dict[str, Any]:
        with self._mutex:
            conn = self._open_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as err:
                raise StoreBusy(
                    f"{self.root / DB_NAME}: another connection holds the write lock "
                    f"({err}); nothing was written"
                ) from err
            try:
                row = conn.execute(
                    "SELECT record FROM records ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                try:
                    sealed = seal(json.loads(row[0]) if row else None)
                except (ValueError, TypeError) as err:  # NaN, a set, ...
                    raise RecordRefused(
                        "E-SCHEMA", "$", f"not canonical JSON: {err}"
                    ) from err
                text = self._check(sealed)
                conn.execute(
                    "INSERT INTO records (seq, hash, record) VALUES (?, ?, ?)",
                    (sealed["seq"], sealed["hash"], text),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return sealed

    def _check(self, sealed: dict[str, Any]) -> str:
        """Schema, then instance; returns the canonical text that is stored."""
        error = best_match(self._validator.iter_errors(sealed))
        if error is not None:
            raise RecordRefused("E-SCHEMA", error.json_path, error.message)
        named = sealed["body"].get("instance_id")
        if named is not None and named != self.instance_id:
            raise RecordRefused(
                "E-INSTANCE",
                "$.body.instance_id",
                f"this store is {self.instance_id}; one store has one instance id",
            )
        return decision_chain.canonical(sealed).decode()

    # -- reads ----------------------------------------------------------------------

    def verify(self) -> VerifyReport:
        with self._mutex:
            return verify_rows(self._open_conn().execute(_SELECT_ALL), self._validator)

    def usage(self) -> StoreUsage:
        """Bytes used against the cap, and free disk against the reserve (stub)."""
        used = sum(
            (self.root / name).stat().st_size
            for name in (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm")
            if (self.root / name).exists()
        )
        return StoreUsage(
            used_bytes=used,
            cap_bytes=self._cap,
            reserve_bytes=RESERVE_BYTES,
            free_bytes=shutil.disk_usage(self.root).free,
            state=usage_state(used, self._cap),
        )

    def pragmas(self) -> dict[str, Any]:
        conn = self._open_conn()
        names = ("journal_mode", "synchronous", "auto_vacuum", "fullfsync")
        return {n: conn.execute(f"PRAGMA {n}").fetchone()[0] for n in names}

    # -- lifecycle ------------------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RecordRefused("E-CLOSED", "", "the writer is closed")
        return self._conn

    def close(self) -> None:
        with self._mutex:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                os.close(self._lock_fd)  # releases the flock

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _take_lock(root: Path) -> int:
    path = root / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
        os.close(fd)
        raise StoreLocked(
            f"{path} is held by another Medic writer (pid {holder}); one store has "
            "one writer (C4 §6.7)"
        ) from None
    os.fchmod(fd, 0o600)
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def _connect(db: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    """C4 §6.3 durability settings on the writer connection."""
    conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if conn.execute("PRAGMA page_count").fetchone()[0] == 0:
            # Must precede the first table; turning it on later needs a full VACUUM.
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode != "wal":
            raise StoreRefused(f"{db}: SQLite refused WAL mode (got {mode!r})")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA fullfsync = ON")  # macOS; a no-op elsewhere
        conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
        check = conn.execute("PRAGMA quick_check").fetchall()
        if check != [("ok",)]:
            raise StoreRefused(f"{db}: quick_check failed: {check[:3]}")
        conn.executescript(
            f"BEGIN IMMEDIATE; {_DDL} INSERT OR IGNORE INTO meta (key, value) "
            f"VALUES ('schema_version', '{SCHEMA_VERSION}'); COMMIT;"
        )
        (found,) = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if int(found) > SCHEMA_VERSION:
            raise StoreRefused(
                f"{db}: store layout v{found} is newer than this Medic (v{SCHEMA_VERSION})"
            )
    except BaseException:
        conn.close()
        raise
    return conn


def open_writer(
    root: Path, *, cap_bytes: int = GIB, busy_timeout_ms: int = 5000
) -> DecisionWriter:
    """Open (or create) `<root>/medic.db` as its one writer.

    Refuses (StoreRefused) a root or file that isn't private (files.mode_problem),
    a second writer (StoreLocked), a bad or missing instance id, a corrupt file,
    and a chain that doesn't verify: appending would extend a tampered head.
    """
    root = Path(root)
    prepare_root(root)
    lock_fd = _take_lock(root)
    conn: sqlite3.Connection | None = None
    try:
        db = root / DB_NAME
        if not os.path.lexists(db):
            create_private_file(db)
        try:
            conn = _connect(db, busy_timeout_ms)
        except sqlite3.DatabaseError as err:
            raise StoreRefused(f"{db}: not a readable SQLite store ({err})") from err
        (count,) = conn.execute("SELECT count(*) FROM records").fetchone()
        instance_id = load_or_create_instance_id(root, store_is_empty=count == 0)
        validator = _validator()
        report = verify_rows(conn.execute(_SELECT_ALL), validator)
        if not report.ok:
            raise StoreRefused(
                f"{db}: the decision chain is broken ({report.code} at seq "
                f"{report.first_bad_seq}: {report.detail}); Medic won't append to it"
            )
    except BaseException:
        if conn is not None:
            conn.close()
        os.close(lock_fd)
        raise
    # C4 §6.5's outside witness: the head goes to the log, outside the store.
    log.info(
        "medic store open: %d records, head seq %s hash %s, instance %s",
        report.count,
        report.head_seq,
        report.head_hash,
        instance_id,
    )
    return DecisionWriter(root, conn, lock_fd, instance_id, cap_bytes, validator)


@contextmanager
def open_reader(root: Path) -> Iterator[sqlite3.Connection]:
    """A separate read-only connection (C4 §6.7). Keep it short: an open reader
    holds back WAL checkpoints."""
    db = Path(root) / DB_NAME
    if not db.exists():
        raise StoreRefused(f"{db}: no store here")
    conn = sqlite3.connect(
        db.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
    )
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def verify_store(root: Path) -> VerifyReport:
    """Verify the whole chain through a reader; needs no lock, so anyone can run it."""
    with open_reader(root) as conn:
        return verify_rows(conn.execute(_SELECT_ALL), _validator())
