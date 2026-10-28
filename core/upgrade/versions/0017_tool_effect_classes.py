"""One stored effect class per outside tool, seeded from the static registries (#3304).

The seed is literals in the SQL beside this file, not read from the
integration servers: a later tool edit must not change what this revision
writes on a fresh install. The report counts the rows by class.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql, stash_report

revision = "0017_tool_effect_classes"
down_revision = "0016_retention"


def _counts(conn) -> dict[str, int]:
    rows = conn.execute(text("""
            SELECT effect_class, count(*) FROM public.tool_effect_classes
            GROUP BY effect_class
            """))
    found = {effect_class: int(n) for effect_class, n in rows}
    return {
        "irreversible": found.get("irreversible", 0),
        "read_unverified": found.get("read_unverified", 0),
    }


def _trail(conn, body: dict) -> None:
    from core.audit import trail
    from core.version import __version__

    if not trail.trail_ready(conn):
        return
    trail.append(
        conn,
        {
            trail.ACTION: "tools.classified",
            trail.ACTOR: trail.SYSTEM,
            "release": __version__,
            "report": body,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    run_sql(__file__)
    body = _counts(conn)
    _trail(conn, body)
    stash_report(revision, body)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
