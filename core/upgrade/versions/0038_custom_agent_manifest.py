"""Every custom agent becomes an operator manifest (#4023).

Each row is converted by ``manifest_from_row`` as it shipped with this revision,
frozen in ``frozen/persona_manifest.py``.
"""

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql, stash_report

revision = "0038_custom_agent_manifest"
down_revision = "0037_audit_webhook"


def upgrade() -> None:
    from core.upgrade.versions.frozen.persona_manifest import manifest_from_row

    conn = op.get_bind()
    run_sql(__file__)
    rows = conn.execute(text("SELECT * FROM public.custom_agents")).mappings().all()
    for row in rows:
        conn.execute(
            text("UPDATE public.custom_agents SET manifest = :m WHERE id = :id"),
            {"m": manifest_from_row(dict(row)), "id": row["id"]},
        )
    stash_report(revision, {"converted": len(rows)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
