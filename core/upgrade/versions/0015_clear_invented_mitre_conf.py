"""Null the constant ATT&CK confidences collectors used to invent (#3223).

Each writer stored one float: Darktrace 0.7, Elastic (Kibana and Wazuh) 0.9,
OpenSearch 0.9, CrowdStrike 0.9, Defender 1.0. The comparison is that float,
not a numeric literal. A finding created in this transaction stays as stored.
"""

from alembic import op
from sqlalchemy import text

from core.audit import trail

revision = "0015_clear_invented_mitre_conf"
down_revision = "0014_mitre_confidence_nullable"

# The only confidence each of those writers stores, under one source string.
_CONSTANTS = (
    ("darktrace", 0.7),
    ("elastic", 0.9),
    ("opensearch", 0.9),
    ("crowdstrike", 0.9),
    ("microsoft_defender", 1.0),
)

_CLEAR = text("""
    UPDATE finding_mitre_predictions AS prediction
    SET confidence = NULL
    FROM findings
    WHERE prediction.finding_id = findings.finding_id
      AND findings.data_source = :data_source
      AND findings.created_at < (transaction_timestamp() AT TIME ZONE 'UTC')
      AND prediction.confidence = CAST(:constant AS double precision)
    RETURNING prediction.finding_id, prediction.technique_id
    """)


def clear(conn) -> None:
    ready = trail.trail_ready(conn)
    for data_source, constant in _CONSTANTS:
        rows_cleared = len(
            conn.execute(
                _CLEAR, {"data_source": data_source, "constant": constant}
            ).all()
        )
        if not ready or rows_cleared == 0:
            continue
        trail.append(
            conn,
            {
                trail.ACTION: "findings.technique_confidence_cleared",
                trail.ACTOR: trail.SYSTEM,
                "data_source": data_source,
                "constant": constant,
                "rows_cleared": rows_cleared,
                "revision": revision,
            },
        )


def upgrade() -> None:
    clear(op.get_bind())


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
