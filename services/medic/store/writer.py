"""The decision store: one SQLite file, one writer, one hash chain (C4 §6, G2).

Every write goes through one DecisionWriter, which holds `medic.lock` for the
life of the process and appends inside `BEGIN IMMEDIATE`, so nothing can slip
a record in between reading the head and writing the next one. Sealing and
verifying are the contract's own code (`contracts/decision_chain.py`), not a
copy, so the store's hashes are the contract's by construction.

Bounded (G3b): a 16 KB record cap, a size cap (SQLite `max_page_count`) with a
reserve held as a filler row, and a free-space guard. At a bound the writer
stops recording: `append` raises RecordHeld, except for `reserve=True` records
(the recording-stopped incident), which are written into the freed reserve.

Not here yet (G3): retention and purge, anchors and the corruption reset.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
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
RESERVE_BYTES = 64 * MIB  # C4 §6.4; held as a filler row inside the file
# A cap too small for 64 MiB gets a reserve of a sixteenth of it (the default
# 1 GiB cap gets exactly 64 MiB).
RESERVE_FRACTION = 16
NEAR_CAP_PERCENT = 80  # C4 §6.4 step 1
RECORD_CAP = 16 * 1024  # bytes of a stored record's canonical JSON (G3b)
# Recording resumes only with this share of the cap to spare, so it can't flap.
RESUME_MARGIN_PERCENT = 5
# Shared disks (Compose, host-native): stop below the larger of 10 % and 2 GiB free.
DISK_FLOOR_PERCENT = 10
DISK_FLOOR_MIN = 2 * GIB
JOURNAL_SIZE_LIMIT = 16 * MIB
# A reader waits this long for the file, then fails. Shorter than the writer's
# 5 s: an open reader holds back WAL checkpoints (C4 §6.7).
READER_BUSY_MS = 1000

ENVELOPE = ("seq", "prev", "hash")

# Types append() refuses: each has one writer path. A forged anchor would make
# deleting the rows it "covers" verify clean.
WRITER_ONLY_TYPES = {
    "store_reset": "store_reset is written only by reset()",
    "anchor": "anchors are written only by the purge (G3)",
}


def _first_float(node: Any, path: str) -> str | None:
    """JSON path of the first float in `node`, if any."""
    if isinstance(node, float):
        return path
    if isinstance(node, dict):
        items = ((f"{path}.{k}", v) for k, v in node.items())
    elif isinstance(node, list):
        items = ((f"{path}[{i}]", v) for i, v in enumerate(node))
    else:
        return None
    for sub_path, value in items:
        found = _first_float(value, sub_path)
        if found:
            return found
    return None


_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- The reserve: one filler row, deleted to make room when the cap is hit.
CREATE TABLE IF NOT EXISTS reserve (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    filler BLOB NOT NULL
);
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


class RecordHeld(StoreError):
    """Recording is stopped (cap or free space): the record was not written."""


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
    used_bytes: int  # file + wal + shm
    cap_bytes: int  # the effective cap: SQLite won't go below the file's size
    reserve_bytes: int
    reserve_left_bytes: int
    free_bytes: int
    states: tuple[str, ...]  # StoreHealth.states (medic-api.openapi.yaml)


def _is_full(err: BaseException) -> bool:
    return isinstance(err, sqlite3.OperationalError) and "full" in str(err)


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

    Limits of an unkeyed chain, caught only by the logged head (C4 §6.5):
    deleting the newest rows leaves a valid chain, and an edited record with a
    forged hash is named at the *next* seq (E-LINK).
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
        *,
        guard: bool = True,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.root = root
        self.instance_id = instance_id
        self._conn: sqlite3.Connection | None = conn
        self._lock_fd = lock_fd
        self._cap = cap_bytes
        self._validator = validator
        self._mutex = threading.Lock()
        self._guard = guard  # the free-space floor; off on Helm (the cap is clamped)
        self._disk_usage = disk_usage  # the one seam to the filesystem's free space
        self._reserve = min(RESERVE_BYTES, cap_bytes // RESERVE_FRACTION)
        # SQLite won't set the limit below the file's size: read back what it took.
        pages = max(1, cap_bytes // self._page_size(conn))
        self._max_page_count = conn.execute(
            f"PRAGMA max_page_count = {pages}"
        ).fetchone()[0]
        self._filler = conn.execute("SELECT count(*) FROM reserve").fetchone()[0] > 0
        self._stop: str | None = None  # why recording is stopped, if it is
        self.excerpts_dropped = 0
        if self._max_page_count * self._page_size(conn) > cap_bytes:
            log.warning(
                "Store cap %d bytes is below the store's size; the effective cap is %d",
                cap_bytes,
                self._max_page_count * self._page_size(conn),
            )
        self._assess(conn)

    # -- writes ---------------------------------------------------------------------

    def append(
        self, record: dict[str, Any], *, reserve: bool = False
    ) -> dict[str, Any]:
        """Seal `record` onto the chain and commit it. Returns the stored record.

        The caller supplies `v`, `at`, `type` and `body`; the writer owns `seq`,
        `prev` and `hash`, so a caller can't fork or rewind the chain. While
        recording is stopped only `reserve=True` (the recording-stopped incident)
        is written, into the freed reserve; anything else raises RecordHeld.
        A record over RECORD_CAP loses excerpts, last evidence item first.
        """
        if not isinstance(record, dict):
            raise RecordRefused("E-SCHEMA", "$", "a record is a JSON object")
        supplied = [k for k in ENVELOPE if k in record]
        if supplied:
            raise RecordRefused(
                "E-ENVELOPE", supplied[0], "the writer assigns seq, prev and hash"
            )
        if record.get("type") in WRITER_ONLY_TYPES:
            raise RecordRefused("E-TYPE", "type", WRITER_ONLY_TYPES[record.get("type")])
        # Our own copy: the caller can't change the record between hash and insert.
        try:
            own = json.loads(json.dumps(record, allow_nan=False))
        except (ValueError, TypeError) as err:  # NaN, a set, ...
            raise RecordRefused("E-SCHEMA", "$", f"not JSON: {err}") from err
        where = _first_float(own, "$")
        if where:
            # jsonschema calls 2.0 an integer, but Python hashes it as "2.0" and a
            # canonical-JSON verifier elsewhere as "2" (G2: integers only).
            raise RecordRefused("E-SCHEMA", where, "numbers are integers only")

        def seal(last: dict[str, Any] | None) -> dict[str, Any]:
            sealed = decision_chain.seal(own, last)
            while (size := len(decision_chain.canonical(sealed))) > RECORD_CAP:
                if not self._drop_excerpt(own):
                    raise RecordRefused(
                        "E-SIZE",
                        "$",
                        f"{size} bytes, over the {RECORD_CAP} cap, with no excerpt to drop",
                    )
                sealed = decision_chain.seal(own, last)
            return sealed

        return self._write(seal, reserve=reserve)

    def _drop_excerpt(self, record: dict[str, Any]) -> bool:
        """Drop the excerpt of the last evidence item that has one; count it."""
        evidence = record.get("body", {}).get("evidence")
        for i in range(len(evidence) - 1, -1, -1) if isinstance(evidence, list) else ():
            if isinstance(evidence[i], dict) and evidence[i].pop("excerpt", None):
                self.excerpts_dropped += 1
                log.warning(
                    "Record over %d bytes: dropped the excerpt of evidence item %d "
                    "(%d dropped so far)",
                    RECORD_CAP,
                    i,
                    self.excerpts_dropped,
                )
                return True
        return False

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

    def _write(self, seal, *, reserve: bool = False) -> dict[str, Any]:
        with self._mutex:
            conn = self._open_conn()
            self._assess(conn)
            if self._stop and not reserve:
                raise RecordHeld(
                    f"recording is stopped ({self._stop}); nothing was written"
                )
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as err:
                if "locked" not in str(err) and "busy" not in str(err):
                    raise
                raise StoreBusy(
                    f"{self.root / DB_NAME}: another connection holds the write lock "
                    f"({err}); nothing was written"
                ) from err
            released = False
            try:
                row = conn.execute(
                    "SELECT record FROM records ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                sealed = seal(json.loads(row[0]) if row else None)
                text = self._check(sealed)
                if self._stop and self._filler:
                    # Free the reserve; the insert below reuses its pages.
                    conn.execute("DELETE FROM reserve")
                    released = True
                conn.execute(
                    "INSERT INTO records (seq, hash, record) VALUES (?, ?, ?)",
                    (sealed["seq"], sealed["hash"], text),
                )
                conn.execute("COMMIT")
            except BaseException as err:
                # A failed COMMIT (SQLITE_FULL, I/O) may already have rolled back;
                # an unconditional ROLLBACK would then hide the real error.
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if _is_full(err) and not self._stop:
                    # The hard cap (or a full disk) beat the soft check.
                    self._trip("cap")
                    raise RecordHeld("the store is full; nothing was written") from err
                raise
            if released:
                self._filler = False
                log.warning("Recording stopped: the reserve is released for incidents")
            if self._stop:
                # Fold the wal back, so the reserve's writes don't sit outside the cap.
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return sealed

    def _check(self, sealed: dict[str, Any]) -> str:
        """Schema, gap order, then instance; returns the canonical text that is stored."""
        error = best_match(self._validator.iter_errors(sealed))
        if error is not None:
            raise RecordRefused("E-SCHEMA", error.json_path, error.message)
        if sealed["type"] == "gap" and not decision_chain.gap_ok(sealed["body"]):
            # verify() would fail on it, and a broken chain won't reopen (S2-4).
            raise RecordRefused("E-GAP", "$.body", "the gap ends before it starts")
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

    @property
    def stopped(self) -> bool:
        """True while recording is stopped (as of the last write or `refresh`)."""
        return self._stop is not None

    def refresh(self) -> bool:
        """Re-check the bounds (and resume if room came back); returns `stopped`."""
        with self._mutex:
            try:
                self._assess(self._open_conn())
            except (StoreError, sqlite3.Error) as exc:
                log.error("Store bounds not checked: %s", type(exc).__name__)
        return self.stopped

    def usage(self) -> StoreUsage:
        """Bytes used against the effective cap, the reserve, and free disk."""
        with self._mutex:
            conn = self._open_conn()
            used = self._disk_bytes()
            live = self._live_bytes(conn)
            cap = self._max_page_count * self._page_size(conn)
        left = self._reserve if self._filler else min(self._reserve, max(0, cap - live))
        if self._stop:
            states = (
                ("recording_stopped",)
                if self._filler
                else ("on_reserve", "recording_stopped")
            )
        elif live * 100 >= self._cap * NEAR_CAP_PERCENT:
            states = ("near_cap",)
        else:
            states = ("ok",)
        return StoreUsage(
            used_bytes=used,
            cap_bytes=cap,
            reserve_bytes=self._reserve,
            reserve_left_bytes=left,
            free_bytes=self._disk_usage(self.root).free,
            states=states,
        )

    # -- bounds ---------------------------------------------------------------------

    def _disk_bytes(self) -> int:
        used = 0
        for name in (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm"):
            try:
                used += (self.root / name).stat().st_size
            except FileNotFoundError:
                pass
        return used

    @staticmethod
    def _page_size(conn: sqlite3.Connection) -> int:
        return conn.execute("PRAGMA page_size").fetchone()[0]

    def _live_bytes(self, conn: sqlite3.Connection, *, truncate: bool = False) -> int:
        """What the store holds: pages in use (the freed reserve isn't) plus wal and
        shm, which `max_page_count` doesn't bound. `truncate` first folds the wal
        back into the file, as it's only ever shrunk by a checkpoint."""
        if truncate:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pages, free = (
            conn.execute(f"PRAGMA {n}").fetchone()[0]
            for n in ("page_count", "freelist_count")
        )
        extra = sum(
            (self.root / (DB_NAME + sfx)).stat().st_size
            for sfx in ("-wal", "-shm")
            if (self.root / (DB_NAME + sfx)).exists()
        )
        return (pages - free) * self._page_size(conn) + extra

    def _floor(self, usage: Any) -> int | None:
        if not self._guard:
            return None
        return max(usage.total * DISK_FLOOR_PERCENT // 100, DISK_FLOOR_MIN)

    def _trip(self, reason: str) -> None:
        self._stop = reason
        log.error("Recording stopped (%s): Medic keeps detecting", reason)

    def _arm(self, conn: sqlite3.Connection) -> bool:
        """Fill the reserve. False if it doesn't fit (the cap's hard limit)."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO reserve (id, filler) VALUES (1, zeroblob(?))",
                (self._reserve,),
            )
            conn.execute("COMMIT")
        except sqlite3.OperationalError as err:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if _is_full(err):
                return False
            raise
        self._filler = True
        return True

    def _assess(self, conn: sqlite3.Connection) -> None:
        """Decide recording / stopped before a write: free space first, then size.
        Stopped resumes only with room to spare, re-arming the reserve."""
        du = self._disk_usage(self.root)
        floor = self._floor(du)
        live = self._live_bytes(conn)
        if live >= self._cap:
            live = self._live_bytes(conn, truncate=True)
        if self._stop is None:
            if floor is not None and du.free < floor:
                self._trip("disk")
            elif live >= self._cap or (
                not self._filler
                and (live + self._reserve >= self._cap or not self._arm(conn))
            ):
                self._trip("cap")
            return
        # Stopped: room means the data, a reserve and a margin under the cap.
        need = live + (0 if self._filler else self._reserve)
        if need + self._cap * RESUME_MARGIN_PERCENT // 100 > self._cap:
            return
        if floor is not None and du.free < floor + self._reserve:
            return
        if not self._filler and not self._arm(conn):
            return
        self._stop = None
        log.warning("Recording resumed")

    def pragmas(self) -> dict[str, Any]:
        names = ("journal_mode", "synchronous", "auto_vacuum", "fullfsync")
        with self._mutex:
            conn = self._open_conn()
            return {n: conn.execute(f"PRAGMA {n}").fetchone()[0] for n in names}

    # -- lifecycle ------------------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RecordRefused("E-CLOSED", "", "the writer is closed")
        return self._conn

    def close(self) -> None:
        with self._mutex:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                try:
                    conn.close()
                finally:
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
    root: Path,
    *,
    cap_bytes: int = GIB,
    busy_timeout_ms: int = 5000,
    shape: str = "compose",
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> DecisionWriter:
    """Open (or create) `<root>/medic.db` as its one writer.

    `shape` picks the disk rule: on Helm the volume is Medic's own, so the cap is
    clamped to half of it; elsewhere the disk is shared and recording stops below
    the free-space floor. `disk_usage` is the seam to the filesystem.

    Refuses (StoreRefused) a root or file that isn't private (files.mode_problem),
    a second writer (StoreLocked), a bad or missing instance id, a corrupt file,
    and a chain that doesn't verify: appending would extend a tampered head.
    """
    root = Path(root)
    prepare_root(root)
    helm = shape == "helm"
    if helm:
        cap_bytes = min(cap_bytes, disk_usage(root).total // 2)
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
    try:
        return DecisionWriter(
            root,
            conn,
            lock_fd,
            instance_id,
            cap_bytes,
            validator,
            guard=not helm,
            disk_usage=disk_usage,
        )
    except BaseException:
        conn.close()
        os.close(lock_fd)
        raise


@contextmanager
def open_reader(root: Path) -> Iterator[sqlite3.Connection]:
    """A separate read-only connection (C4 §6.7). One connection, closed by the
    caller when the read returns. `query_only` plus a short `busy_timeout`: a
    reader that can't get the file fails instead of sitting on the WAL."""
    db = Path(root) / DB_NAME
    if not db.exists():
        raise StoreRefused(f"{db}: no store here")
    conn = sqlite3.connect(
        db.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
    )
    try:
        conn.execute(f"PRAGMA busy_timeout = {READER_BUSY_MS}")
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def verify_store(root: Path) -> VerifyReport:
    """Verify the whole chain through a reader; needs no lock, so anyone can run it."""
    with open_reader(root) as conn:
        return verify_rows(conn.execute(_SELECT_ALL), _validator())
