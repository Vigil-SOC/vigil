"""Teams, membership, and a team on every case (#4324).

The grants are literals in the SQL beside this file. They are not read from
``core.auth.permissions``: a later catalog edit must not change what this
revision writes. The one trail entry is this revision's counts. If the trail
cannot take it, the revision fails and says why.
"""

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql

revision = "0064_teams"
down_revision = "0063_auth_links"


def upgrade() -> None:
    from core.audit import trail

    conn = op.get_bind()
    if not trail.trail_ready(conn):
        raise RuntimeError(
            "the audit trail cannot take the teams entry: the trail is not ready"
        )
    run_sql(__file__)
    people = conn.execute(
        text("SELECT count(*) FROM public.team_memberships")
    ).scalar_one()
    cases = conn.execute(text("SELECT count(*) FROM public.cases")).scalar_one()
    team_id = conn.execute(
        text("SELECT team_id FROM public.teams WHERE is_default")
    ).scalar_one()
    trail.append(
        conn,
        {
            trail.ACTION: "teams.placed",
            trail.ACTOR: trail.SYSTEM,
            "team_id": team_id,
            "people_joined": int(people),
            "cases_placed": int(cases),
        },
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
