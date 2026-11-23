"""Move Splunk and CrowdStrike off "Low and up" (#4893).

Those two sources stored an informational finding as low, so "Low and up"
kept everything they sent. Informational findings now rank below low, and Any
keeps the same findings. Other floors and every other source stay. The report
lists the source ids whose floor was cleared.
"""

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import stash_report

revision = "0108_info_below_low"
down_revision = "0107_merge_0106_heads"


_CLEAR_LOW_FLOOR = text(
    "UPDATE public.federation_sources "
    "SET min_severity = NULL "
    "WHERE source_id IN ('splunk', 'crowdstrike') AND min_severity = 'low' "
    "RETURNING source_id"
)


def upgrade() -> None:
    moved = list(op.get_bind().execute(_CLEAR_LOW_FLOOR).scalars())
    stash_report(revision, {"moved_to_any": sorted(moved)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
