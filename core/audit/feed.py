"""The syslog feed and the one replica that sends it (#2767).

One backend task holds a session advisory lock and, each tick, drains late
entries, closes collapse windows, writes the 15-minute checkpoint, imports
upgrade history, and sends the trail as CEF over TLS (RFC 5425) when an admin
has turned the feed on. The cursor is ``audit_feed_state``, outside the chain.
The trail is the backlog: a failed batch is resent from the same sequence.
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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import text

from core.audit import trail
from core.config import get_settings
from core.secrets_manager import delete_secret, get_secret, set_secret
from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager, get_db_session
from core.version import __version__

logger = logging.getLogger(__name__)

INTEGRATION_ID = "audit_syslog"
CA_SECRET = "audit_syslog_ca"
CERT_SECRET = "audit_syslog_cert"
KEY_SECRET = "audit_syslog_key"
ACTION_FAILED = "audit.feed.failed"
ACTION_RECOVERED = "audit.feed.recovered"
ACTION_UPGRADE = "upgrade.run"

# "vigilFED". Session lock, held for the life of the leader. Not the trail's
# transaction lock and not the Medic poller's.
LOCK_KEY = 0x7669_6769_6C46_4544
BATCH = 100
TICK_SECONDS = 5.0
BACKOFF_CAP = 60.0
CHECKPOINT_INTERVAL = timedelta(minutes=15)
_CONNECT_TIMEOUT = 10.0
_PEM_MAX = 64_000
_MSG_MAX = 4000

_SAVE_MAINT = text("""
    UPDATE public.audit_feed_state
    SET last_checkpoint_at = :checkpoint_at,
        checkpoint_through = :checkpoint_through,
        history_offset = :history_offset
    WHERE id = 1
""")
_MARK_FAIL = text("""
    UPDATE public.audit_feed_state
    SET failing = true, last_error = :error
    WHERE id = 1
""")
_MARK_OK = text("""
    UPDATE public.audit_feed_state
    SET failing = false, last_error = NULL,
        last_delivered_seq = :seq, last_delivered_at = now()
    WHERE id = 1
""")


class FeedError(ValueError):
    """The settings are not usable. The router answers 400."""


class FeedUnavailable(RuntimeError):
    """The revision has not been applied."""


@dataclass
class SendClock:
    """When the next send may run. Tests set ``now``; the loop uses monotonic."""

    backoff: float = 1.0
    not_before: float = 0.0
    now: float = 0.0

    def due(self) -> bool:
        return self.now >= self.not_before

    def failed(self) -> None:
        self.not_before = self.now + self.backoff
        self.backoff = min(self.backoff * 2.0, BACKOFF_CAP)

    def ok(self) -> None:
        self.backoff = 1.0
        self.not_before = 0.0


@dataclass(frozen=True)
class Destination:
    enabled: bool
    host: str
    port: Optional[int]
    ca_pem: str
    cert_pem: Optional[str]
    key_pem: Optional[str]

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.host and self.port and self.ca_pem)


@dataclass
class State:
    last_delivered_seq: Optional[int]
    failing: bool
    last_checkpoint_at: datetime
    checkpoint_through: int
    history_offset: int


def _esc(value: str, header: bool = False) -> str:
    value = (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("|", "\\|")
    )
    if not header:
        value = value.replace("=", "\\=")
    return value


def _syslog_host() -> str:
    raw = "".join(c if c.isalnum() or c in "._-" else "_" for c in socket.gethostname())
    return raw[:255] or "-"


def _when(value) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def cef(row: dict) -> bytes:
    """One RFC 5424 syslog message whose MSG is a CEF record of ``row``."""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    action = str(payload.get(trail.ACTION) or "audit")
    when = _when(row.get("occurred_at"))
    severity = "0" if action == trail.ACTION_CHECKPOINT else "1"
    if payload.get(trail.OUTCOME) == trail.OUTCOME_DENIED:
        severity = "6"
    head = (
        f"<134>1 {when.strftime('%Y-%m-%dT%H:%M:%S.%fZ')} {_syslog_host()} "
        f"Vigil - audit - - CEF:0|Vigil|SOC|{_esc(__version__, True)}|"
        f"{_esc(action, True)}|{_esc(action, True)}|{severity}|"
    )
    outcome = payload.get(trail.OUTCOME)
    if row.get("late"):
        outcome = trail.OUTCOME_RECORDED_LATE
    parts = [
        f"externalId={row['seq']}",
        f"end={int(when.timestamp() * 1000)}",
        f"suser={_esc(str(payload.get(trail.ACTOR) or ''))}",
        f"cs1={_esc(str(row.get('entry_hash') or ''))}",
        "cs1Label=entryHash",
    ]
    if outcome:
        parts.append(f"outcome={_esc(str(outcome))}")
    if row.get("source_ip"):
        parts.append(f"src={_esc(str(row['source_ip']))}")
    if row.get("operator_text"):
        parts.append(f"cs2={_esc(str(row['operator_text']))}")
        parts.append("cs2Label=operatorText")
    msg = json.dumps(payload, separators=(",", ":"), default=str)
    if len(msg) > _MSG_MAX:
        msg = msg[: _MSG_MAX - 1] + "…"
    parts.append(f"msg={_esc(msg)}")
    return (head + " ".join(parts)).encode("utf-8")


def frame(message: bytes) -> bytes:
    """An RFC 5425 octet-counted frame."""
    return str(len(message)).encode("ascii") + b" " + message


def _temp_pem(value: str) -> Path:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
    try:
        os.chmod(handle.name, 0o600)
        handle.write(value)
    finally:
        handle.close()
    return Path(handle.name)


def deliver(dest: Destination, messages: list[bytes]) -> None:
    """Write ``messages`` as octet-counted frames. Raises if the socket fails."""
    if not messages or dest.port is None:
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=dest.ca_pem)
    paths: list[Path] = []
    try:
        if dest.cert_pem and dest.key_pem:
            cert = _temp_pem(dest.cert_pem)
            key = _temp_pem(dest.key_pem)
            paths.extend((cert, key))
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        payload = b"".join(frame(message) for message in messages)
        with socket.create_connection((dest.host, dest.port), _CONNECT_TIMEOUT) as sock:
            sock.settimeout(_CONNECT_TIMEOUT)
            with ctx.wrap_socket(sock, server_hostname=dest.host) as tls:
                tls.sendall(payload)
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def _safe_error(exc: BaseException) -> str:
    text_value = str(exc).replace("\n", " ")
    if "BEGIN " in text_value:
        return type(exc).__name__
    return text_value[:300]


@contextmanager
def _tx() -> Iterator:
    session = get_db_session()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def _table(session) -> bool:
    return (
        session.execute(text("SELECT to_regclass('public.audit_feed_state')")).scalar()
        is not None
    )


def _load(session) -> Optional[State]:
    row = session.execute(text("""
                SELECT last_delivered_seq, failing, last_checkpoint_at,
                       checkpoint_through, history_offset
                FROM public.audit_feed_state WHERE id = 1 FOR UPDATE
            """)).mappings().first()
    if row is None:
        return None
    return State(
        last_delivered_seq=row["last_delivered_seq"],
        failing=bool(row["failing"]),
        last_checkpoint_at=row["last_checkpoint_at"],
        checkpoint_through=int(row["checkpoint_through"]),
        history_offset=int(row["history_offset"]),
    )


def _history_file() -> Optional[Path]:
    raw = (get_settings().vigil_upgrade_dir or "").strip()
    if not raw:
        return None
    path = Path(raw) / "history.jsonl"
    return path if path.is_file() else None


def _seen(session, digest: str) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM public.audit_trail "
                "WHERE payload->>'history_line' = :d LIMIT 1"
            ),
            {"d": digest},
        ).first()
        is not None
    )


def _import_history(session, offset: int) -> int:
    """New lines of upgrade history, once each. Returns the byte offset consumed."""
    path = _history_file()
    if path is None:
        return offset
    try:
        data = path.read_bytes()
    except OSError:
        logger.warning("audit feed could not read upgrade history")
        return offset
    if offset < 0 or offset > len(data):
        offset = 0
    start = offset
    while start < len(data):
        end = data.find(b"\n", start)
        if end < 0:
            break
        raw = data[start:end]
        start = end + 1
        if not raw.strip():
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if _seen(session, digest):
            continue
        try:
            entry = json.loads(raw.decode("utf-8"))
        except ValueError:
            logger.warning("upgrade history line is not json")
            continue
        if not isinstance(entry, dict):
            continue
        reason = entry.get("reason")
        operator = (
            reason.strip() if isinstance(reason, str) and reason.strip() else None
        )
        trail.append(
            session,
            {
                trail.ACTION: ACTION_UPGRADE,
                trail.ACTOR: trail.SYSTEM,
                "history_line": digest,
                "outcome": entry.get("outcome"),
                "from": entry.get("from"),
                "to": entry.get("to"),
                "restore_point": entry.get("restore_point"),
            },
            operator_text=operator,
        )
    return start


def _checkpoint(session, state: State) -> None:
    now = session.execute(text("SELECT now()")).scalar_one()
    tip = trail.current_tip(session)
    if tip.seq <= state.checkpoint_through:
        return
    last = state.last_checkpoint_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now - last < CHECKPOINT_INTERVAL:
        return
    trail.append(
        session,
        {
            trail.ACTION: trail.ACTION_CHECKPOINT,
            trail.ACTOR: trail.SYSTEM,
            "checkpoint": {"seq": tip.seq, "hash": tip.entry_hash},
        },
    )
    state.checkpoint_through = trail.current_tip(session).seq
    state.last_checkpoint_at = now


def _destination(session) -> Destination:
    row = (
        session.execute(
            text(
                "SELECT enabled, config FROM public.integration_configs "
                "WHERE integration_id = :id"
            ),
            {"id": INTEGRATION_ID},
        )
        .mappings()
        .first()
    )
    config = {}
    enabled = False
    if row is not None:
        enabled = bool(row["enabled"])
        if isinstance(row["config"], dict):
            config = row["config"]
    host = str(config.get("host") or "").strip()
    raw_port = config.get("port")
    try:
        port = int(raw_port) if raw_port is not None and raw_port != "" else None
    except (TypeError, ValueError):
        port = None
    cert = get_secret(CERT_SECRET) or ""
    key = get_secret(KEY_SECRET) or ""
    return Destination(
        enabled=enabled,
        host=host,
        port=port,
        ca_pem=get_secret(CA_SECRET) or "",
        cert_pem=cert or None,
        key_pem=key or None,
    )


def _fail(session, state: State, error: str) -> None:
    if not state.failing:
        trail.append(
            session,
            {trail.ACTION: ACTION_FAILED, trail.ACTOR: trail.SYSTEM, "error": error},
        )
        state.failing = True
    session.execute(_MARK_FAIL, {"error": error})


def maintenance_and_send(clock: SendClock) -> None:
    """One leader tick. Sending is its own transaction, after the socket write."""
    with _tx() as session:
        if not trail.trail_ready(session) or not _table(session):
            return
        state = _load(session)
        if state is None:
            return
        trail.drain_late(session)
        trail.close_windows(session)
        _checkpoint(session, state)
        state.history_offset = _import_history(session, state.history_offset)
        session.execute(
            _SAVE_MAINT,
            {
                "checkpoint_at": state.last_checkpoint_at,
                "checkpoint_through": state.checkpoint_through,
                "history_offset": state.history_offset,
            },
        )
    _send(clock)


def _send(clock: SendClock) -> None:
    if not clock.due():
        return
    with _tx() as session:
        if not _table(session):
            return
        dest = _destination(session)
        state = _load(session)
        if state is None or not dest.enabled:
            return
        if not dest.ready:
            _fail(session, state, "the feed is on but the destination is incomplete")
            clock.failed()
            return
        after = state.last_delivered_seq or 0
        rows = trail.read(session, trail.make_query(after=after), BATCH)
    if not rows:
        return
    try:
        deliver(dest, [cef(row) for row in rows])
    except Exception as exc:  # noqa: BLE001 - the next tick resends this batch
        logger.warning("audit feed send failed: %s", type(exc).__name__)
        with _tx() as session:
            state = _load(session)
            if state is not None:
                _fail(session, state, _safe_error(exc))
        clock.failed()
        return
    with _tx() as session:
        state = _load(session)
        if state is None:
            return
        if state.failing:
            trail.append(
                session, {trail.ACTION: ACTION_RECOVERED, trail.ACTOR: trail.SYSTEM}
            )
        session.execute(_MARK_OK, {"seq": rows[-1]["seq"]})
    clock.ok()


def _try_lock(conn) -> bool:
    return bool(
        conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
    )


def attempt(conn, clock: SendClock, leading: bool) -> bool:
    """Take the lock if ``leading`` is false, then run one tick.

    Returns false when another session holds the lock. The caller keeps the
    connection, and the lock, until it stops leading.
    """
    if not leading:
        if not _try_lock(conn):
            return False
        on_become_leader()
    maintenance_and_send(clock)
    return True


def on_become_leader() -> None:
    with _tx() as session:
        if trail.trail_ready(session) and _table(session):
            trail.close_windows(session, everything=True)


def _connect():
    manager = get_db_manager()
    if manager.engine is None:
        manager.initialize()
    return manager.engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _alive(conn) -> bool:
    try:
        conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - a dead session has lost the lock
        return False


def _release(conn, leading: bool) -> None:
    try:
        if leading and conn is not None:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    except Exception:  # noqa: BLE001
        logger.debug("audit feed lock release failed", exc_info=True)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _ready() -> bool:
    try:
        with _tx() as session:
            return trail.trail_ready(session)
    except Exception:  # noqa: BLE001
        logger.debug("audit feed could not read the trail revision", exc_info=True)
        return False


def _loop(stop: threading.Event) -> None:
    """The lock stays on this thread's connection for as long as we lead."""
    conn = None
    leading = False
    clock = SendClock()
    try:
        while not stop.is_set():
            try:
                if not _ready():
                    if leading:
                        _release(conn, True)
                        conn = None
                        leading = False
                    stop.wait(TICK_SECONDS)
                    continue
                if conn is None or not _alive(conn):
                    _release(conn, leading)
                    conn = _connect()
                    leading = False
                clock.now = time.monotonic()
                was = leading
                leading = attempt(conn, clock, leading)
                if leading and not was:
                    logger.info("this replica is the audit feed leader")
            except Exception:  # noqa: BLE001 - the next tick tries again
                logger.exception("audit feed tick failed")
                _release(conn, leading)
                conn = None
                leading = False
            stop.wait(TICK_SECONDS)
    finally:
        _release(conn, leading)


async def _run() -> None:
    stop = threading.Event()
    thread = threading.Thread(
        target=_loop, args=(stop,), name="audit-feed", daemon=True
    )
    thread.start()
    try:
        while thread.is_alive():
            await asyncio.sleep(0.5)
    finally:
        stop.set()
        await asyncio.to_thread(thread.join, 15)


def start_leader() -> asyncio.Task:
    """A task the API cancels on shutdown. The lock lives on the thread it starts."""
    return asyncio.create_task(_run(), name="audit-feed")


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def health(session) -> dict:
    """What the integrations block and, later, the audit screen show."""
    if not _table(session):
        raise FeedUnavailable("the audit feed is not available")
    row = session.execute(text("""
                SELECT last_delivered_seq, last_delivered_at, last_error
                FROM public.audit_feed_state WHERE id = 1
            """)).mappings().first()
    dest = _destination(session)
    tip = trail.current_tip(session) if trail.trail_ready(session) else trail.Tip(0, "")
    delivered = row["last_delivered_seq"] if row else None
    behind = max(0, tip.seq - (delivered or 0))
    return {
        "enabled": dest.enabled,
        "host": dest.host,
        "port": dest.port,
        "ca_set": bool(dest.ca_pem),
        "client_cert_set": bool(dest.cert_pem and dest.key_pem),
        "last_delivered_at": _iso(row["last_delivered_at"]) if row else None,
        "last_delivered_seq": delivered,
        "last_error": row["last_error"] if row else None,
        "behind": behind,
        "tip_seq": tip.seq,
    }


def _pem(value: Optional[str], label: str) -> Optional[str]:
    """None keeps the stored secret. A blank string clears it."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > _PEM_MAX:
        raise FeedError(f"{label} is too long")
    return value


def _store(key: str, value: str) -> None:
    if value:
        if not set_secret(key, value):
            raise RuntimeError("could not store feed TLS material")
        return
    delete_secret(key)


def _host_ok(host: str) -> bool:
    if not host or len(host) > 253 or any(ch.isspace() for ch in host):
        return False
    return all(ch.isalnum() or ch in "._:-[]" for ch in host)


def save(
    user_id: str,
    *,
    enabled: bool,
    host: str,
    port: Optional[int],
    ca_pem: Optional[str],
    client_cert_pem: Optional[str],
    client_key_pem: Optional[str],
) -> dict:
    """Host, port and enabled through the integration config; TLS in the secrets store.

    The config write is what records the trail entry. This does not append another.
    """
    host = host.strip()
    ca_pem = _pem(ca_pem, "CA certificate")
    client_cert_pem = _pem(client_cert_pem, "Client certificate")
    client_key_pem = _pem(client_key_pem, "Client key")
    if host and not _host_ok(host):
        raise FeedError("Host is not a hostname or address")
    if port is not None and not 1 <= port <= 65535:
        raise FeedError("Port must be between 1 and 65535")

    with _tx() as session:
        current = _destination(session)
    new_ca = current.ca_pem if ca_pem is None else ca_pem
    new_cert = (current.cert_pem or "") if client_cert_pem is None else client_cert_pem
    new_key = (current.key_pem or "") if client_key_pem is None else client_key_pem
    if bool(new_cert) != bool(new_key):
        raise FeedError("A client certificate and its key are set together")
    if enabled and (not host or port is None or not new_ca):
        raise FeedError(
            "Host, port and a CA certificate are required to turn the feed on"
        )

    if ca_pem is not None:
        _store(CA_SECRET, ca_pem)
    if client_cert_pem is not None:
        _store(CERT_SECRET, client_cert_pem)
    if client_key_pem is not None:
        _store(KEY_SECRET, client_key_pem)

    written = get_config_service(user_id=user_id).set_integration_config(
        INTEGRATION_ID,
        {"host": host, "port": port, "enabled": enabled},
        enabled=enabled,
        integration_name="Audit trail",
        integration_type="syslog",
        description="CEF over TLS syslog",
        change_reason=(
            "turned the audit feed on" if enabled else "saved the audit feed"
        ),
    )
    if not written:
        raise RuntimeError("could not save the audit feed")
    with _tx() as session:
        return health(session)


def send_test(session) -> dict:
    """One CEF frame that is not a trail entry and does not move the cursor."""
    if not _table(session):
        raise FeedUnavailable("the audit feed is not available")
    dest = _destination(session)
    if not dest.ready:
        raise FeedError("Save a host, port and CA certificate before sending a test")
    when = datetime.now(timezone.utc)
    message = (
        f"<134>1 {when.strftime('%Y-%m-%dT%H:%M:%S.%fZ')} {_syslog_host()} "
        f"Vigil - audit - - CEF:0|Vigil|SOC|{_esc(__version__, True)}|"
        f"audit.feed.test|Audit feed test|1|msg=test"
    ).encode("utf-8")
    try:
        deliver(dest, [message])
    except Exception as exc:  # noqa: BLE001 - the button reports the failure
        get_config_service(user_id="system").record_integration_test(
            INTEGRATION_ID, success=False, error=_safe_error(exc), tested_at=when
        )
        raise FeedError(_safe_error(exc)) from exc
    get_config_service(user_id="system").record_integration_test(
        INTEGRATION_ID, success=True, error=None, tested_at=when
    )
    return {"ok": True}
