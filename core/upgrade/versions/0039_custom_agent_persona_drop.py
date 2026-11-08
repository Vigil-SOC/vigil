"""A custom agent is its manifest; the persona columns go (#4024).

A row written between 0038 and the console moving to /api/v1/agents may lack a
manifest; it is filled through ``manifest_from_row`` (frozen in
``frozen/persona_manifest.py``) before the columns go.
"""

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql, stash_report

revision = "0039_custom_agent_persona_drop"
down_revision = "0038_custom_agent_manifest"


def upgrade() -> None:
    from core.upgrade.versions.frozen.persona_manifest import manifest_from_row

    conn = op.get_bind()
    rows = (
        conn.execute(text("SELECT * FROM public.custom_agents WHERE manifest IS NULL"))
        .mappings()
        .all()
    )
    for row in rows:
        conn.execute(
            text("UPDATE public.custom_agents SET manifest = :m WHERE id = :id"),
            {"m": manifest_from_row(dict(row)), "id": row["id"]},
        )
    run_sql(__file__)
    stash_report(revision, {"filled": len(rows)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
