"""Copy the old audit stores onto the trail (#3309, #2755 decision 14).

Every row of case_audit_logs, config_audit_log and investigation_logs becomes
one trail entry through ``trail.append``, at the row's own time, keyed in its
hashed payload by ``legacy: {table, id}``. A key already on the trail is
skipped, so a second run adds nothing. The reconciliation goes in
``audit_legacy_copy``; the read-only stamp beside it is left empty for
``python -m core.upgrade`` to set. agent_events and upgrade history are not
copied, and nothing is dropped here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

from alembic import op
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.upgrade.revision import run_sql, stash_report

revision = "0018_legacy_audit_copy"
down_revision = "0017_tool_effect_classes"

CASE, CONFIG, INVESTIGATION = (
    "case_audit_logs",
    "config_audit_log",
    "investigation_logs",
)
SOURCES = (CASE, CONFIG, INVESTIGATION)
CASE_HISTORY = "case.history"
INVESTIGATION_LOG = "investigation.log"
_SYSTEM_TEXT = frozenset({"system", "orchestrator"})
_BATCH = 1000

_SELECT = {
    CASE: """
        SELECT audit_id AS pk, entity_type, entity_id, action, field_name,
               old_value, new_value, change_summary, changed_by, user_agent,
               ip_address, "timestamp"
        FROM public.case_audit_logs WHERE audit_id > :after
        ORDER BY audit_id LIMIT :n
    """,
    CONFIG: """
        SELECT id AS pk, config_type, config_key, action, old_value, new_value,
               changed_by, change_reason, "timestamp"
        FROM public.config_audit_log WHERE id > :after
        ORDER BY id LIMIT :n
    """,
    INVESTIGATION: """
        SELECT id AS pk, investigation_id, event_type, details, tokens_used,
               "timestamp"
        FROM public.investigation_logs WHERE id > :after
        ORDER BY id LIMIT :n
    """,
}


def _rows(conn, table: str) -> Iterator[Any]:
    after = 0
    while True:
        batch = conn.execute(text(_SELECT[table]), {"after": after, "n": _BATCH}).all()
        yield from batch
        if len(batch) < _BATCH:
            return
        after = batch[-1].pk


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _on_trail(conn) -> set[tuple[str, str]]:
    return {(table, key) for table, key in conn.execute(text("""
            SELECT payload #>> '{legacy,table}', payload #>> '{legacy,id}'
            FROM public.audit_trail WHERE payload ? 'legacy'
        """))}


def _linker(conn) -> Callable[[str], dict[str, Any]]:
    """changed_by text → the actor and attribution fields of a payload."""
    from core.audit import trail
    from core.auth.principals import SYSTEM_PRINCIPAL_ID
    from core.cases.agent_closure import principal_for_username

    by_user_id = dict(
        conn.execute(text("SELECT user_id, principal_id FROM public.users")).all()
    )
    session = Session(bind=conn)
    seen: dict[str, dict[str, Any]] = {}

    def link(changed_by: str) -> dict[str, Any]:
        if changed_by in seen:
            return seen[changed_by]
        if changed_by in _SYSTEM_TEXT:
            found = {
                trail.ACTOR: SYSTEM_PRINCIPAL_ID,
                trail.ATTRIBUTION: trail.ATTRIBUTION_VERIFIED,
            }
        elif changed_by in by_user_id:
            found = {
                trail.ACTOR: by_user_id[changed_by],
                trail.ATTRIBUTION: trail.ATTRIBUTION_VERIFIED,
            }
        elif principal := principal_for_username(session, changed_by):
            found = {
                trail.ACTOR: principal,
                trail.ATTRIBUTION: trail.ATTRIBUTION_MATCHED_BY_NAME,
            }
        else:  # "SOC Analyst", a browser-sent name, anyone no longer live
            found = {trail.ATTRIBUTION: trail.ATTRIBUTION_UNVERIFIED_LEGACY}
        seen[changed_by] = found
        return found

    return link


def _case_text(row) -> Optional[str]:
    """What the Record tab printed for the row; beside the hash, not in it."""
    summary = (row.change_summary or "").strip()
    if summary:
        return summary
    if row.old_value or row.new_value:
        return f"{row.old_value or '—'} → {row.new_value or '—'}"
    return None


def _entry(table: str, row, link) -> tuple[dict[str, Any], dict[str, Any]]:
    """The payload and the beside-columns for one source row."""
    from core.audit import trail
    from core.storage.config_service import (
        CONFIG_CHANGE,
        _redact_tree,
        redacted_config_copy,
    )

    def masked(value):
        # The integration redactor only knows fields of integrations registered
        # now; old rows may name one since removed, so secret-shaped keys go too.
        copied = redacted_config_copy(row.config_type, row.config_key, value)
        return _redact_tree(copied) if row.config_type == "integration" else copied

    beside: dict[str, Any] = {}
    if table == CASE:
        payload = {
            trail.ACTION: CASE_HISTORY,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "verb": row.action,
            "field_name": row.field_name,
            "user_agent": row.user_agent,
        }
        if row.entity_type == "case":
            payload["case_id"] = row.entity_id
        beside = {"operator_text": _case_text(row), "source_ip": row.ip_address}
    elif table == CONFIG:
        # The fields a live config.change carries (config_service.write_config_audit).
        payload = {
            trail.ACTION: CONFIG_CHANGE,
            "verb": row.action,
            "config_type": row.config_type,
            "config_key": row.config_key,
            "change_reason": row.change_reason,
            "old_value": masked(row.old_value),
            "new_value": masked(row.new_value),
        }
    else:
        # No actor column: nobody to link, and no system actor invented.
        payload = {
            trail.ACTION: INVESTIGATION_LOG,
            "investigation_id": row.investigation_id,
            "event_type": row.event_type,
            "details": row.details,
            "tokens_used": row.tokens_used,
            trail.ATTRIBUTION: trail.ATTRIBUTION_UNVERIFIED_LEGACY,
        }
    if table != INVESTIGATION:
        actor_text = (row.changed_by or "").strip()
        payload.update(link(actor_text))
        beside["actor_text"] = actor_text or None
    payload = {k: v for k, v in payload.items() if v is not None}
    payload["legacy"] = {"table": table, "id": row.pk}
    return payload, {k: v for k, v in beside.items() if v is not None}


def copy(conn) -> dict[str, Any]:
    """Append every source row not yet on the trail; the reconciliation."""
    # Registers the integration secret redactor redacted_config_copy needs.
    import core.integrations.integration_secrets  # noqa: F401
    from core.audit import trail

    keys = _on_trail(conn)
    link = _linker(conn)
    seen: dict[str, list[str]] = {}
    copied: dict[str, int] = {}
    for table in SOURCES:
        seen[table], copied[table] = [], 0
        for row in _rows(conn, table):
            key = (table, str(row.pk))
            seen[table].append(key[1])
            if key in keys:
                continue
            payload, beside = _entry(table, row, link)
            trail.append(
                conn, payload, occurred_at=_utc(row.timestamp), gated=False, **beside
            )
            keys.add(key)
            copied[table] += 1
    # Counted again from the trail as written, not from what was appended.
    keys = _on_trail(conn)
    sources = {
        table: {
            "rows": len(ids),
            "on_trail": sum((table, i) in keys for i in ids),
            "copied": copied[table],
        }
        for table, ids in seen.items()
    }
    passed = all(c["rows"] == c["on_trail"] for c in sources.values())
    return {"sources": sources, "copied": sum(copied.values()), "passed": passed}


def _store(conn, body: dict[str, Any]) -> None:
    import json

    # The stamp column is not touched: only the upgrade command sets it.
    conn.execute(
        text("""
            INSERT INTO public.audit_legacy_copy (id, sources, copied, passed)
            VALUES (1, CAST(:sources AS jsonb), :copied, :passed)
            ON CONFLICT (id) DO UPDATE SET
                sources = EXCLUDED.sources, copied = EXCLUDED.copied,
                passed = EXCLUDED.passed, reconciled_at = now()
        """),
        {
            "sources": json.dumps(body["sources"]),
            "copied": body["copied"],
            "passed": body["passed"],
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    run_sql(__file__)
    body = copy(conn)
    _store(conn, body)
    stash_report(revision, body)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
