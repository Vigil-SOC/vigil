"""The Medic watch's one state row (#4340).

The announced status word, the word being confirmed and its poll count,
the ``medic_last_seen.updated_at`` those polls were counted from, and when
the current red word was first read. No init SQL.
"""

from core.upgrade.revision import run_sql

revision = "0065_medic_watch"
down_revision = "0064_teams"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
