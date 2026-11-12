"""End unfinished runs whose stored principal can't carry them (#4348).

Run scoping refuses a run's call unless its principal names that run and has not
expired. A started run (one with a ledger) is judged by the principal on its
``run`` event: missing, unreadable, naming another run or expired, it is ended
as failed. Its console row, its ledger and its pending approvals are written as
``finalize_run``, ``force_terminal`` and ``reject_action`` would, but here on
the migration's connection, so they commit with the upgrade or not at all.

The principal is read without verifying: the Helm upgrade Jobs have no app
Secret, and reading only ever ends a run. Queued runs (no ledger) and child runs
(no console row) are left alone, as are Redis and the job queue.
"""

import json
import time

import jwt
from alembic import op
from sqlalchemy import text

from core.upgrade.revision import stash_report

revision = "0065_end_unscoped_runs"
down_revision = "0064_teams"

# Literals, not imports: a later edit there must not change what this writes.
REASON = "started before run scoping; run it again"
IN_FLIGHT = ["running", "paused"]
EVENT_SCHEMA_VERSION = 1


def _carries(run_id: str, principal) -> bool:
    """Whether the stored principal names this run and is still live."""
    if not isinstance(principal, str) or not principal:
        return False
    try:
        claims = jwt.decode(
            principal,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["HS256"],
        )
    except jwt.InvalidTokenError:
        return False
    exp = claims.get("exp")
    return (
        claims.get("run_id") == run_id
        and isinstance(exp, (int, float))
        and exp > time.time()
    )


def _end(conn, run_id: str) -> None:
    from core.cases import case_trail

    conn.execute(
        text(
            "UPDATE workflow_runs SET status = 'failed',"
            " finished_at = timezone('utc', now()), error = :reason,"
            " outcome = 'failed', reason = :reason, stop_cause = NULL,"
            " duration_ms = CASE WHEN started_at IS NULL THEN duration_ms ELSE"
            "  (extract(epoch FROM timezone('utc', now()) - started_at) * 1000)::int"
            "  END"
            " WHERE run_id = :run_id AND status = ANY(:live)"
        ),
        {"run_id": run_id, "reason": REASON, "live": IN_FLIGHT},
    )
    # force_terminal's insert, outcome failed; the ledger's trigger chains it.
    conn.execute(
        text(
            "INSERT INTO agent_events "
            "(run_id, run_kind, seq, kind, payload, schema_version) "
            "SELECT CAST(:run_id AS uuid), "
            "  (SELECT run_kind FROM agent_events "
            "   WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq LIMIT 1), "
            "  coalesce((SELECT max(seq) FROM agent_events "
            "            WHERE run_id = CAST(:run_id AS uuid)), -1) + 1, "
            "  'terminal', CAST(:payload AS jsonb), :version "
            "WHERE EXISTS (SELECT 1 FROM agent_events "
            "              WHERE run_id = CAST(:run_id AS uuid)) "
            "  AND NOT EXISTS (SELECT 1 FROM agent_events "
            "                  WHERE run_id = CAST(:run_id AS uuid) "
            "                    AND kind = 'terminal')"
        ),
        {
            "run_id": run_id,
            "payload": json.dumps({"outcome": "failed", "reason": REASON}),
            "version": EVENT_SCHEMA_VERSION,
        },
    )
    # reject_action by the system, for each approval still pending.
    rejected = conn.execute(
        text(
            "UPDATE approval_actions SET status = 'rejected',"
            " rejection_reason = :reason, approved_by = 'system',"
            " approved_by_principal_id = 'system',"
            " approved_at = timezone('utc', now())"
            " WHERE workflow_run_id = :run_id AND status = 'pending'"
            " RETURNING action_id"
        ),
        {"run_id": run_id, "reason": REASON},
    ).scalars()
    for action_id in rejected.all():
        case_trail.append_approval(
            conn,
            action=case_trail.REJECT,
            principal_id="system",
            action_id=action_id,
            username="system",
            operator_text=REASON,
        )


def upgrade() -> None:
    conn = op.get_bind()
    # Started runs only: the console row is in flight and the ledger has its run event.
    rows = conn.execute(
        text(
            "SELECT w.run_id, e.payload->>'principal' FROM workflow_runs w"
            " CROSS JOIN LATERAL (SELECT payload FROM agent_events"
            # A console id that is not a uuid has no ledger; CASE keeps the cast safe.
            "  WHERE run_id = CASE WHEN w.run_id ~* :uuid"
            "   THEN CAST(w.run_id AS uuid) END AND kind = 'run'"
            "  ORDER BY seq LIMIT 1) e"
            " WHERE w.status = ANY(:live)"
        ),
        {"live": IN_FLIGHT, "uuid": "^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$"},
    ).all()
    ended = 0
    for run_id, principal in rows:
        if not _carries(run_id, principal):
            _end(conn, run_id)
            ended += 1
    stash_report(revision, {"ended_runs": ended})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
