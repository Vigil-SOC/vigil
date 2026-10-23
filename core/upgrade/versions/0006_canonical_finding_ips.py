"""Rewrite findings' entity IP fields in canonical form, the form new findings
are stored in, so the exclusion filter matches findings stored before it."""

import json

from alembic import op
from sqlalchemy import text

from core.storage.ip_address import FINDING_IP_KEYS, canonical_entity_context

revision = "0006_canonical_finding_ips"
down_revision = "0005_agent_attribution"


def canonicalize(conn) -> int:
    rows = conn.execute(
        text(
            "SELECT finding_id, entity_context FROM findings "
            "WHERE entity_context ?| :keys"
        ).execution_options(stream_results=True, yield_per=1000),
        {"keys": list(FINDING_IP_KEYS)},
    )
    changed = [
        {"id": finding_id, "ctx": json.dumps(canonical)}
        for finding_id, context in rows
        if (canonical := canonical_entity_context(context)) != context
    ]
    if changed:
        conn.execute(
            text(
                "UPDATE findings SET entity_context = CAST(:ctx AS jsonb) "
                "WHERE finding_id = :id"
            ),
            changed,
        )
    return len(changed)


def upgrade() -> None:
    canonicalize(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
