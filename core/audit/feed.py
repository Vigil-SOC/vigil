"""The syslog feed and the one replica that sends it (#2767).

One backend task holds a session advisory lock and, each tick, drains late
entries, closes collapse windows, writes the 15-minute checkpoint, imports
upgrade history, and sends the trail as CEF over TLS (RFC 5425) when an admin
has turned the feed on. The cursor is ``audit_feed_state``, outside the chain.
The trail is the backlog: a failed batch is resent from the same sequence.

Beside syslog, the same tick pushes the trail to one webhook as signed
CloudEvents batches (#4019), with its own cursor (``audit_webhook_state``),
backoff and failure state. Syslog's restore pause and erasure bookkeeping
stay syslog's.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
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
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text

from core.audit import narrow, retention, trail
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

WEBHOOK_INTEGRATION_ID = "audit_webhook"
WEBHOOK_SECRET = "audit_webhook_secret"
# CloudEvents ``type`` is this prefix, a dot, then the trail action.
EVENT_TYPE_PREFIX = "ai.deeptempo.vigil.audit"
EVENT_SOURCE = "/vigil/audit"
_SECRET_PREFIX = "whsec_"
_WEBHOOK_TIMEOUT = 10.0
_URL_MAX = 2048
_AREA = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

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
            gated=False,
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
        gated=False,
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


def pause_after_restore(session, seq: int) -> None:
    """Flag the restore entry ``seq`` as not yet off the box (decision 11).

    Writes wait for it only when the feed is on; a feed-off restore never
    pauses, and its flag clears on a poll or an export.
    """
    if not _table(session) or not trail.pause_ready(session):
        return
    session.execute(
        text("""
            UPDATE public.audit_feed_state
            SET unconfirmed_seq = :seq,
                pause_until = CASE WHEN EXISTS (
                    SELECT 1 FROM public.integration_configs
                    WHERE integration_id = :id AND enabled
                ) THEN now() + :pause END
            WHERE id = 1
        """),
        {"seq": seq, "id": INTEGRATION_ID, "pause": trail.PAUSE},
    )


def _fail(session, state: State, error: str) -> None:
    if not state.failing:
        trail.append(
            session,
            {trail.ACTION: ACTION_FAILED, trail.ACTOR: trail.SYSTEM, "error": error},
            gated=False,
        )
        state.failing = True
    session.execute(_MARK_FAIL, {"error": error})


def maintenance_and_send(
    clock: SendClock,
    *,
    now: Optional[datetime] = None,
    hook: Optional[SendClock] = None,
) -> None:
    """One leader tick. Sending is its own transaction, after the socket write.

    ``now`` is the wall clock for retention and erasure. The send clock stays
    monotonic. Once a period is in force this tick prefix-deletes; an
    ``audit.manage`` erasure whose seven days have elapsed is blanked here too.
    """
    wall = now or datetime.now(timezone.utc)
    with _tx() as session:
        if not trail.trail_ready(session) or not _table(session):
            return
        state = _load(session)
        if state is None:
            return
        # Retention row, then any accounts a due erasure will touch, before
        # the trail lock. The other order deadlocks with a save or a deletion.
        retention.lock_due_accounts(session, wall)
        trail.drain_late(session)
        trail.close_windows(session)
        retention.apply_due(session, wall)
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
    if hook is not None:
        _send_webhook(hook)


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
        # No person reads what is forwarded: open cases only (#4362).
        rows = narrow.entries(session, rows, narrow.nobody())
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
                session,
                {trail.ACTION: ACTION_RECOVERED, trail.ACTOR: trail.SYSTEM},
                gated=False,
            )
        delivered = rows[-1]["seq"]
        session.execute(_MARK_OK, {"seq": delivered})
        retention.cover_through(session, delivered)
        trail.confirm(session, through=delivered)
    clock.ok()


def _try_lock(conn) -> bool:
    return bool(
        conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
    )


def attempt(
    conn, clock: SendClock, leading: bool, hook: Optional[SendClock] = None
) -> bool:
    """Take the lock if ``leading`` is false, then run one tick.

    Returns false when another session holds the lock. The caller keeps the
    connection, and the lock, until it stops leading.
    """
    if not leading:
        if not _try_lock(conn):
            return False
        on_become_leader()
    maintenance_and_send(clock, hook=hook)
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
    hook = SendClock()
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
                clock.now = hook.now = time.monotonic()
                was = leading
                leading = attempt(conn, clock, leading, hook)
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
    unconfirmed, paused = trail.pause_state(session)
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
        "unconfirmed": unconfirmed,
        "paused": paused,
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


# --- Webhook (#4019): signed CloudEvents batches, one destination ---------


@dataclass(frozen=True)
class Webhook:
    enabled: bool
    url: str
    area: Optional[str]
    secret: str

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.url and self.secret)


@dataclass
class HookState:
    last_delivered_seq: Optional[int]
    failing: bool
    pending_through: Optional[int]


def _hook_table(session) -> bool:
    return (
        session.execute(
            text("SELECT to_regclass('public.audit_webhook_state')")
        ).scalar()
        is not None
    )


def _hook_load(session) -> Optional[HookState]:
    row = session.execute(text("""
                SELECT last_delivered_seq, failing, pending_through
                FROM public.audit_webhook_state WHERE id = 1 FOR UPDATE
            """)).mappings().first()
    if row is None:
        return None
    return HookState(
        last_delivered_seq=row["last_delivered_seq"],
        failing=bool(row["failing"]),
        pending_through=row["pending_through"],
    )


def _webhook(session) -> Webhook:
    row = (
        session.execute(
            text(
                "SELECT enabled, config FROM public.integration_configs "
                "WHERE integration_id = :id"
            ),
            {"id": WEBHOOK_INTEGRATION_ID},
        )
        .mappings()
        .first()
    )
    config = (
        row["config"] if row is not None and isinstance(row["config"], dict) else {}
    )
    return Webhook(
        enabled=bool(row["enabled"]) if row is not None else False,
        url=str(config.get("url") or "").strip(),
        area=(str(config.get("area") or "").strip() or None),
        secret=get_secret(WEBHOOK_SECRET) or "",
    )


def cloudevent(row: dict) -> dict:
    """One CloudEvents 1.0 event whose ``data`` is the trail entry as poll returns it."""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    action = str(payload.get(trail.ACTION) or "audit")
    return {
        "specversion": "1.0",
        "id": str(row["seq"]),
        "source": EVENT_SOURCE,
        "type": f"{EVENT_TYPE_PREFIX}.{action}",
        "time": row["occurred_at"],
        "datacontenttype": "application/json",
        "data": row,
    }


def _signing_key(secret: str) -> bytes:
    return base64.b64decode(secret.removeprefix(_SECRET_PREFIX), validate=True)


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """The Standard Webhooks ``webhook-signature`` header value."""
    signed = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(_signing_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def post_webhook(hook: Webhook, msg_id: str, events: list[dict]) -> None:
    """POST a signed batch. Raises on a transport error or a non-2xx answer."""
    body = json.dumps(events, separators=(",", ":"), default=str).encode("utf-8")
    timestamp = int(time.time())  # fresh per attempt; the id stays the batch's
    headers = {
        "content-type": "application/cloudevents-batch+json; charset=utf-8",
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(hook.secret, msg_id, timestamp, body),
        "user-agent": f"Vigil/{__version__}",
    }
    response = httpx.post(
        hook.url, content=body, headers=headers, timeout=_WEBHOOK_TIMEOUT
    )
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"the webhook answered HTTP {response.status_code}")


def _hook_fail(session, state: HookState, error: str, pending: Optional[int]) -> None:
    if not state.failing:
        trail.append(
            session,
            {
                trail.ACTION: ACTION_FAILED,
                trail.ACTOR: trail.SYSTEM,
                "destination": "webhook",
                "error": error,
            },
            gated=False,
        )
    session.execute(
        text("""
            UPDATE public.audit_webhook_state
            SET failing = true, last_error = :error,
                pending_through = COALESCE(:pending, pending_through)
            WHERE id = 1
        """),
        {"error": error, "pending": pending},
    )


def _send_webhook(clock: SendClock) -> None:
    """One webhook batch. Advances only its own cursor, and only after a 2xx."""
    if not clock.due():
        return
    with _tx() as session:
        if not _hook_table(session):
            return
        hook = _webhook(session)
        state = _hook_load(session)
        if state is None or not hook.enabled:
            return
        if not hook.ready:
            _hook_fail(
                session, state, "the webhook is on but has no URL or secret", None
            )
            clock.failed()
            return
        cursor = state.last_delivered_seq or 0
        # Tip before the read: a short page means nothing else matches up to it,
        # so a filtered cursor moves past entries it skips instead of rescanning.
        tip = trail.current_tip(session).seq
        rows = trail.read(
            session, trail.make_query(after=cursor, area=hook.area), BATCH
        )
        through = rows[-1]["seq"] if len(rows) == BATCH else tip
        # A resend is the batch that failed, so it keeps its webhook-id.
        if state.pending_through is not None:
            kept = [row for row in rows if row["seq"] <= state.pending_through]
            if kept:
                rows, through = kept, kept[-1]["seq"]
        rows = narrow.entries(session, rows, narrow.nobody())
        if not rows:
            if through > cursor:
                session.execute(
                    text(
                        "UPDATE public.audit_webhook_state"
                        " SET last_delivered_seq = :seq WHERE id = 1"
                    ),
                    {"seq": through},
                )
            return
    first, last = rows[0]["seq"], rows[-1]["seq"]
    through = max(through, last)
    try:
        post_webhook(hook, f"vigil-audit-{first}-{last}", [cloudevent(r) for r in rows])
    except Exception as exc:  # noqa: BLE001 - the next due tick resends this batch
        logger.warning(
            "audit webhook send failed: %s; next try in %.0fs",
            type(exc).__name__,
            clock.backoff,
        )
        with _tx() as session:
            state = _hook_load(session)
            if state is not None:
                _hook_fail(session, state, _safe_error(exc), last)
        clock.failed()
        return
    with _tx() as session:
        state = _hook_load(session)
        if state is None:
            return
        if state.failing:
            trail.append(
                session,
                {
                    trail.ACTION: ACTION_RECOVERED,
                    trail.ACTOR: trail.SYSTEM,
                    "destination": "webhook",
                },
                gated=False,
            )
        session.execute(
            text("""
                UPDATE public.audit_webhook_state
                SET failing = false, last_error = NULL, pending_through = NULL,
                    last_delivered_seq = :seq, last_delivered_at = now()
                WHERE id = 1
            """),
            {"seq": through},
        )
    clock.ok()


def webhook_health(session) -> dict:
    """URL, filter and cursor. Never the secret."""
    if not _hook_table(session):
        raise FeedUnavailable("the audit webhook is not available")
    row = session.execute(text("""
                SELECT last_delivered_seq, last_delivered_at, last_error, failing
                FROM public.audit_webhook_state WHERE id = 1
            """)).mappings().first()
    hook = _webhook(session)
    tip = trail.current_tip(session) if trail.trail_ready(session) else trail.Tip(0, "")
    delivered = row["last_delivered_seq"] if row else None
    return {
        "enabled": hook.enabled,
        "url": hook.url,
        "area": hook.area,
        "secret_set": bool(hook.secret),
        "failing": bool(row["failing"]) if row else False,
        "last_delivered_at": _iso(row["last_delivered_at"]) if row else None,
        "last_delivered_seq": delivered,
        "last_error": row["last_error"] if row else None,
        # With an area filter this counts entries the webhook will never send.
        "behind": max(0, tip.seq - (delivered or 0)),
        "tip_seq": tip.seq,
    }


def _url_ok(url: str) -> bool:
    if len(url) > _URL_MAX or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return False
    # No user:pass@: the URL is written to the trail with the config change.
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and not (parts.username or parts.password)
    )


def _secret_ok(secret: str) -> bool:
    if not secret.startswith(_SECRET_PREFIX):
        return False
    try:
        key = _signing_key(secret)
    except ValueError:
        return False
    return 24 <= len(key) <= 64


def save_webhook(
    user_id: str,
    *,
    enabled: bool,
    url: str,
    area: Optional[str],
    secret: Optional[str],
) -> dict:
    """URL, area and enabled through the integration config; the secret in the store.

    None keeps the stored secret, or generates one when none is stored. A
    generated secret is in this response and never again.
    """
    url = url.strip()
    area = (area or "").strip() or None
    if url and not _url_ok(url):
        raise FeedError("URL must be an http or https URL with no credentials in it")
    if area is not None and not _AREA.match(area):
        raise FeedError("Area is a trail area such as case or audit")
    if enabled and not url:
        raise FeedError("A URL is required to turn the webhook on")
    if secret is not None:
        secret = secret.strip()
        if not _secret_ok(secret):
            raise FeedError("Secret must be whsec_ and 24 to 64 base64-encoded bytes")
    with _tx() as session:
        if not _hook_table(session):
            raise FeedUnavailable("the audit webhook is not available")
        generated = None
        if secret is None and not _webhook(session).secret:
            secret = generated = (
                _SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()
            )
    if secret is not None and not set_secret(WEBHOOK_SECRET, secret):
        raise RuntimeError("could not store the webhook secret")

    written = get_config_service(user_id=user_id).set_integration_config(
        WEBHOOK_INTEGRATION_ID,
        {"url": url, "area": area, "enabled": enabled},
        enabled=enabled,
        integration_name="Audit trail webhook",
        integration_type="webhook",
        description="Signed CloudEvents batches",
        change_reason=(
            "turned the audit webhook on" if enabled else "saved the audit webhook"
        ),
    )
    if not written:
        if generated is not None:  # never shown, so do not keep it
            delete_secret(WEBHOOK_SECRET)
        raise RuntimeError("could not save the audit webhook")
    with _tx() as session:
        result = webhook_health(session)
    if generated is not None:
        result["secret"] = generated
    return result


def send_webhook_test(session) -> dict:
    """One signed event that is not a trail entry and does not move the cursor."""
    if not _hook_table(session):
        raise FeedUnavailable("the audit webhook is not available")
    hook = _webhook(session)
    if not hook.url or not hook.secret:
        raise FeedError("Save a URL before sending a test")
    when = datetime.now(timezone.utc)
    event_id = f"test-{secrets.token_hex(8)}"
    event = {
        "specversion": "1.0",
        "id": event_id,
        "source": EVENT_SOURCE,
        "type": f"{EVENT_TYPE_PREFIX}.audit.feed.test",
        "time": when.isoformat(),
        "datacontenttype": "application/json",
        "data": {"test": True},
    }
    try:
        post_webhook(hook, f"vigil-audit-{event_id}", [event])
    except Exception as exc:  # noqa: BLE001 - the caller reports the failure
        get_config_service(user_id="system").record_integration_test(
            WEBHOOK_INTEGRATION_ID,
            success=False,
            error=_safe_error(exc),
            tested_at=when,
        )
        raise FeedError(_safe_error(exc)) from exc
    get_config_service(user_id="system").record_integration_test(
        WEBHOOK_INTEGRATION_ID, success=True, error=None, tested_at=when
    )
    return {"ok": True}
