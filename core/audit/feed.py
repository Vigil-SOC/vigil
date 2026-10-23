"""The audit leader and the syslog feed (#2767, decisions 7, 10 and 15).

One backend replica leads: it holds a session advisory lock on a connection of
its own for as long as it lives. Each tick it records held entries late,
closes collapse windows, writes the 15-minute checkpoint, imports upgrade runs
from ``$VIGIL_UPGRADE_DIR/history.jsonl``, and, when an admin has turned the
feed on, sends the trail as CEF in RFC 5425 frames over TLS.

The feed's cursor is ``audit_feed_state`` (revision ``0007_audit_feed``),
outside the chain. The cursor moves only after a batch is written to the
socket; a failed batch is sent again from the same sequence. There is no
queue: the trail is the backlog, so nothing on a request path waits on this.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit import trail
from core.config import AUDIT_FEED_INTEGRATION_ID
from core.version import __version__

logger = logging.getLogger(__name__)

# The IntegrationConfig row that holds host, port and enabled. Off by default.
INTEGRATION_ID = AUDIT_FEED_INTEGRATION_ID
INTEGRATION_NAME = "Audit trail syslog feed"
# TLS material, in the secrets store.
SECRET_CA = "AUDIT_FEED_CA_PEM"
SECRET_CLIENT_CERT = "AUDIT_FEED_CLIENT_CERT_PEM"
SECRET_CLIENT_KEY = "AUDIT_FEED_CLIENT_KEY_PEM"

ACTION_FAILED = "audit.feed.failed"
ACTION_RECOVERED = "audit.feed.recovered"
ACTION_TEST = "audit.feed.test"
ACTION_UPGRADE = "upgrade.run"
# Payload key of an imported history line: the sha256 of the line as written.
UPGRADE_LINE = "history_line"
HISTORY = "history.jsonl"

_LEADER_LOCK = 0x7669_6769_6C46_4544  # "vigilFED"
TICK_SECONDS = 5.0
CHECKPOINT_INTERVAL = timedelta(minutes=15)
BATCH = 100
# Batches one tick may send before the next round of housekeeping.
BATCHES_PER_TICK = 20
BACKOFF_CAP_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 10.0
# Syslog facility 13 (log audit), severity 6 (informational).
_PRI = 13 * 8 + 6


@dataclass(frozen=True)
class Destination:
    host: str
    port: int
    ca_pem: Optional[str] = None
    client_cert_pem: Optional[str] = None
    client_key_pem: Optional[str] = None


# --- the wire ------------------------------------------------------------------


def _header(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _ext(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def cef(entry: dict[str, Any]) -> str:
    """One trail entry as CEF: the hashed payload, its seq, time and hash."""
    payload = entry["payload"]
    action = payload.get(trail.ACTION) or "audit.entry"
    outcome = payload.get(trail.OUTCOME)
    severity = 7 if action == ACTION_FAILED else 5 if outcome == "denied" else 3
    occurred = datetime.fromisoformat(entry["occurred_at"])
    ext = {
        "rt": int(occurred.timestamp() * 1000),
        "externalId": entry["seq"],
        "act": action,
        "suser": payload.get(trail.ACTOR),
        "outcome": outcome,
        "cs1Label": "entryHash",
        "cs1": entry["entry_hash"],
        "cs2Label": "payload",
        "cs2": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        "cn1Label": "late",
        "cn1": int(bool(entry["late"])),
    }
    body = " ".join(f"{k}={_ext(v)}" for k, v in ext.items() if v is not None)
    head = "|".join(
        _header(v) for v in ("DeepTempo", "Vigil", __version__, action, action)
    )
    return f"CEF:0|{head}|{severity}|{body}"


def _hostname() -> str:
    name = "".join(c for c in socket.gethostname() if 33 <= ord(c) <= 126)
    return name[:255] or "-"


def frame(entry: dict[str, Any]) -> bytes:
    """An RFC 5424 message carrying the CEF, octet-counted as RFC 5425 sends it."""
    occurred = datetime.fromisoformat(entry["occurred_at"]).astimezone(timezone.utc)
    stamp = occurred.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    message = f"<{_PRI}>1 {stamp} {_hostname()} vigil - audit - {cef(entry)}"
    data = message.encode("utf-8")
    return f"{len(data)} ".encode("ascii") + data


def tls_context(dest: Destination) -> ssl.SSLContext:
    """Verifies the server against the stored CA, or the system's when none is."""
    context = ssl.create_default_context(cadata=dest.ca_pem or None)
    # RFC 9662 (syslog over TLS) requires TLS 1.2 or later.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if dest.client_cert_pem:
        # load_cert_chain reads files only: a private directory, removed at once.
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            cert.write_text(dest.client_cert_pem, encoding="utf-8")
            key = None
            if dest.client_key_pem:
                key = Path(tmp) / "key.pem"
                key.write_text(dest.client_key_pem, encoding="utf-8")
                os.chmod(key, 0o600)
            context.load_cert_chain(str(cert), str(key) if key else None)
    return context


def describe(error: BaseException) -> str:
    """A fixed phrase for a send failure: it reaches the API, the trail and the
    SIEM, so it never carries exception text. The detail goes to the log."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return "the receiver's certificate is not trusted or does not match the host"
    if isinstance(error, ssl.SSLError):
        return "the TLS handshake failed"
    if isinstance(error, socket.gaierror):
        return "the host name does not resolve"
    if isinstance(error, ConnectionRefusedError):
        return "the connection was refused"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "the connection timed out"
    if isinstance(error, (ConnectionResetError, BrokenPipeError)):
        return "the receiver closed the connection"
    return "the receiver could not be reached"


def open_tls(dest: Destination) -> ssl.SSLSocket:
    raw = socket.create_connection(
        (dest.host, dest.port), timeout=CONNECT_TIMEOUT_SECONDS
    )
    try:
        return tls_context(dest).wrap_socket(raw, server_hostname=dest.host)
    except BaseException:
        raw.close()
        raise


Connect = Callable[[Destination], Any]


def close(sock) -> None:
    """close_notify first: a bare close with unread session tickets is a reset,
    and a reset can drop what the receiver has not yet read."""
    try:
        sock.settimeout(2)
        sock.unwrap()
    except Exception:  # noqa: BLE001 - the peer may close first; we close anyway
        pass
    finally:
        sock.close()


def send_test(dest: Destination, connect: Connect = open_tls) -> None:
    """One test message to ``dest``; raises what the connection raised. Not an entry."""
    entry = {
        "seq": 0,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "late": False,
        "entry_hash": "",
        "payload": {trail.ACTION: ACTION_TEST, trail.ACTOR: trail.SYSTEM},
    }
    sock = connect(dest)
    try:
        sock.sendall(frame(entry))
    finally:
        close(sock)


# --- settings and state ----------------------------------------------------------


def settings(session) -> tuple[bool, Optional[str], Optional[int]]:
    """Enabled, host and port from the integration row; off when there is none."""
    row = session.execute(
        text(
            "SELECT enabled, config FROM integration_configs"
            " WHERE integration_id = :id"
        ),
        {"id": INTEGRATION_ID},
    ).first()
    if row is None:
        return False, None, None
    config = row.config or {}
    return bool(row.enabled), config.get("host"), config.get("port")


def destination(session, *, when_off: bool = False) -> Optional[Destination]:
    """Where to send, or None while the feed is off (unless ``when_off``, for a
    test) or has no destination."""
    from core.secrets import get_secret

    enabled, host, port = settings(session)
    if not ((enabled or when_off) and host and port):
        return None
    return Destination(
        host,
        int(port),
        get_secret(SECRET_CA) or None,
        get_secret(SECRET_CLIENT_CERT) or None,
        get_secret(SECRET_CLIENT_KEY) or None,
    )


def _state(session, lock: bool = False):
    sql = "SELECT * FROM public.audit_feed_state WHERE id = 1"
    return session.execute(text(sql + (" FOR UPDATE" if lock else ""))).mappings().one()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


def health(session) -> dict[str, Any]:
    """Last delivered, and how far behind the tip in entries. #2766 reads this too."""
    state = _state(session)
    tip = trail.current_tip(session)
    return {
        "delivered_seq": state["delivered_seq"],
        "delivered_at": _iso(state["delivered_at"]),
        "tip_seq": tip.seq,
        "behind": max(0, tip.seq - state["delivered_seq"]),
        "failing": state["failing"],
        "last_error": state["last_error"],
        "last_error_at": _iso(state["last_error_at"]),
    }


# --- the leader's housekeeping ------------------------------------------------------


def checkpoint(session, now: Optional[datetime] = None) -> Optional[trail.Entry]:
    """The timed checkpoint, the 1000th-append shape, when entries came since the last."""
    state = _state(session, lock=True)
    now = now or session.execute(text("SELECT now()")).scalar_one()
    due = state["checkpoint_at"] is None or (
        now - state["checkpoint_at"] >= CHECKPOINT_INTERVAL
    )
    tip = trail.current_tip(session)
    if not due or tip.seq <= state["checkpoint_seq"]:
        return None
    entry = trail.append(
        session,
        {
            trail.ACTION: trail.ACTION_CHECKPOINT,
            trail.ACTOR: trail.SYSTEM,
            "checkpoint": tip.public(),
        },
    )
    session.execute(
        text(
            "UPDATE public.audit_feed_state"
            " SET checkpoint_seq = :seq, checkpoint_at = :now WHERE id = 1"
        ),
        {"seq": entry.seq, "now": now},
    )
    return entry


def _import_line(session, raw: bytes) -> bool:
    key = hashlib.sha256(raw).hexdigest()
    seen = session.execute(
        text(
            "SELECT 1 FROM public.audit_trail"
            f" WHERE payload->>'{trail.ACTION}' = :action"
            f" AND payload->>'{UPGRADE_LINE}' = :key LIMIT 1"
        ),
        {"action": ACTION_UPGRADE, "key": key},
    ).first()
    if seen is not None:
        return False
    try:
        line = json.loads(raw)
        occurred_at = datetime.fromisoformat(line["time"])
    except (ValueError, KeyError, TypeError):
        logger.warning("upgrade history line %s is not a run; skipped", key[:12])
        return False
    trail.append(
        session,
        {
            trail.ACTION: ACTION_UPGRADE,
            trail.ACTOR: trail.SYSTEM,
            "from": line.get("from"),
            "to": line.get("to"),
            "result": line.get("outcome"),
            "restore_point": line.get("restore_point"),
            UPGRADE_LINE: key,
        },
        occurred_at=occurred_at,
        late=True,
        # What the operator gave (an acknowledgement's reason) is never the actor.
        operator_text=line.get("reason") or None,
    )
    return True


def import_upgrades(session, upgrade_dir: Optional[Path]) -> int:
    """New ``history.jsonl`` lines as system entries; each line is recorded once."""
    if upgrade_dir is None:
        return 0
    path = Path(upgrade_dir) / HISTORY
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    offset = _state(session, lock=True)["upgrade_offset"]
    if size < offset:  # a replaced file: read it again; the line key dedupes
        offset = 0
    if size == offset:
        return 0
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(size - offset)
    end = data.rfind(b"\n") + 1  # a line still being written waits for its newline
    imported = 0
    for raw in data[:end].splitlines():
        if not raw.strip():
            continue
        try:
            with session.begin_nested():
                imported += _import_line(session, raw)
        except Exception:  # noqa: BLE001 - one bad line must not block the rest
            logger.exception("upgrade history line could not be recorded; skipped")
    session.execute(
        text("UPDATE public.audit_feed_state SET upgrade_offset = :o WHERE id = 1"),
        {"o": offset + end},
    )
    return imported


# --- the leader -------------------------------------------------------------------


class Leader:
    """One per backend process. ``tick`` does nothing unless this one holds the lock."""

    def __init__(
        self,
        engine,
        *,
        upgrade_dir: Optional[Path] = None,
        connect: Connect = open_tls,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.engine = engine
        self.upgrade_dir = upgrade_dir
        self.connect = connect
        self.clock = clock
        self._conn = None
        self._failures = 0
        self._retry_at = 0.0
        self._busy = threading.Lock()
        self._stopped = False

    @property
    def leading(self) -> bool:
        return self._conn is not None

    def tick(self) -> None:
        with self._busy:
            if self._stopped or not self._lead():
                return
            for step in (
                lambda s: trail.drain_late(s),
                lambda s: trail.close_windows(s),
                checkpoint,
                lambda s: import_upgrades(s, self.upgrade_dir),
            ):
                try:
                    with Session(self.engine) as session, session.begin():
                        step(session)
                except Exception:  # noqa: BLE001 - the next step and tick still run
                    logger.exception("audit leader step failed")
            try:
                self._send()
            finally:
                if self._stopped:
                    self._release()

    def stop(self) -> None:
        """Give the lock up now, or at the end of a tick that is running."""
        self._stopped = True
        if self._busy.acquire(blocking=False):
            try:
                self._release()
            finally:
                self._busy.release()

    def _ready(self) -> bool:
        with Session(self.engine) as session:
            for table in ("audit_trail", "audit_feed_state"):
                if (
                    session.execute(
                        text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
                    ).scalar()
                    is None
                ):
                    return False
            return trail.trail_ready(session)

    def _lead(self) -> bool:
        if self._conn is not None and self._alive():
            return True
        conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _LEADER_LOCK}
            ).scalar_one()
        except BaseException:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self._conn = conn
        try:
            if not self._ready():
                self._release()
                return False
            # A window the last leader left open closes now, not in 60 seconds.
            with Session(self.engine) as session, session.begin():
                trail.close_windows(session, everything=True)
        except BaseException:
            self._release()
            raise
        logger.info("audit leader: this replica leads")
        return True

    def _alive(self) -> bool:
        """Whether this replica still holds the lock's session."""
        try:
            self._conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - the session, and its lock, are gone
            logger.warning("audit leader lost its connection; taking over again")
            self._release()
            return False

    def _release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LEADER_LOCK})
        except Exception:  # noqa: BLE001 - closing drops a session lock anyway
            pass
        conn.close()

    def _send(self) -> None:
        if self.clock() < self._retry_at:
            return
        with Session(self.engine) as session:
            dest = destination(session)
        if dest is None:
            self._failures, self._retry_at = 0, 0.0
            return
        sock = None
        try:
            for _ in range(BATCHES_PER_TICK):
                if not self._alive():  # another replica may lead by now
                    return
                with Session(self.engine) as session:
                    after = _state(session)["delivered_seq"]
                    rows = trail.read(session, trail.TrailQuery(after=after), BATCH)
                if not rows:
                    return
                try:
                    if sock is None:
                        sock = self.connect(dest)
                    sock.sendall(b"".join(frame(r) for r in rows))
                except (OSError, ValueError) as e:
                    self._failed(e)
                    return
                self._delivered(rows[-1]["seq"])
                if len(rows) < BATCH:
                    return
        finally:
            if sock is not None:
                close(sock)

    def _failed(self, error: Exception) -> None:
        self._failures += 1
        delay = min(BACKOFF_CAP_SECONDS, 2.0 ** (self._failures - 1))
        self._retry_at = self.clock() + delay
        message = describe(error)
        logger.warning(
            "audit feed send failed (retry in %ss): %s: %s",
            delay,
            type(error).__name__,
            error,
        )
        with Session(self.engine) as session, session.begin():
            state = _state(session, lock=True)
            if not state["failing"]:
                # Once per outage, not per retry; the feed carries it once it is back.
                trail.append(
                    session,
                    {
                        trail.ACTION: ACTION_FAILED,
                        trail.ACTOR: trail.SYSTEM,
                        "error": message,
                        "delivered_seq": state["delivered_seq"],
                    },
                )
            session.execute(
                text(
                    "UPDATE public.audit_feed_state SET failing = true,"
                    " last_error = :e, last_error_at = now() WHERE id = 1"
                ),
                {"e": message},
            )

    def _delivered(self, seq: int) -> None:
        self._failures, self._retry_at = 0, 0.0
        with Session(self.engine) as session, session.begin():
            state = _state(session, lock=True)
            if state["failing"]:
                trail.append(
                    session,
                    {
                        trail.ACTION: ACTION_RECOVERED,
                        trail.ACTOR: trail.SYSTEM,
                        "delivered_seq": seq,
                    },
                )
            session.execute(
                text(
                    "UPDATE public.audit_feed_state SET delivered_seq ="
                    " greatest(delivered_seq, :s), delivered_at = now(),"
                    " failing = false WHERE id = 1"
                ),
                {"s": seq},
            )


async def run(leader: Leader, interval: float = TICK_SECONDS) -> None:
    try:
        while True:
            try:
                await asyncio.to_thread(leader.tick)
            except Exception:  # noqa: BLE001 - a bad tick must not end the leader
                logger.exception("audit leader tick failed")
            await asyncio.sleep(interval)
    finally:
        leader.stop()


def start(upgrade_dir: Optional[str] = None) -> Optional[asyncio.Task]:
    """Start the leader loop in this backend process; None without Postgres."""
    from core.storage.connection import get_db_manager

    engine = get_db_manager().engine
    if engine is None or engine.dialect.name != "postgresql":
        return None
    leader = Leader(engine, upgrade_dir=Path(upgrade_dir) if upgrade_dir else None)
    return asyncio.get_running_loop().create_task(run(leader))
