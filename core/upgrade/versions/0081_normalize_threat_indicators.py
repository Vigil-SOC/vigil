"""Store feed indicators in the form lookups match on (#4537).

IP, domain and hash rows are rewritten to ``normalize_indicator_value`` (the
defang rule, then case-folded), so an indicator stored before the fix matches
without the feed re-publishing it. Rows that fold onto one value under the same
source and type are merged into the most recently seen one: earliest
first_seen, latest last_seen, and the latest valid_until unless any of them
never expires. URL and email rows stay as stored. A second run changes nothing.
"""

from __future__ import annotations

from collections import defaultdict

from alembic import op
from sqlalchemy import bindparam, text

from core.audit import trail
from core.threat_intel.threat_feed_service import (
    FOLDED_INDICATOR_TYPES,
    normalize_indicator_value,
)

revision = "0081_normalize_threat_indicators"
down_revision = "0080_merge_0079_heads"


def upgrade() -> None:
    conn = op.get_bind()
    if (
        conn.execute(text("SELECT to_regclass('public.threat_indicators')")).scalar()
        is None
    ):
        return
    rows = conn.execute(
        text("""
            SELECT id, source, indicator_type, indicator_value,
                   first_seen, last_seen, valid_until
            FROM public.threat_indicators
            WHERE indicator_type IN :types
            """).bindparams(bindparam("types", expanding=True)),
        {"types": sorted(FOLDED_INDICATOR_TYPES)},
    ).all()

    groups = defaultdict(list)
    for row in rows:
        normal = normalize_indicator_value(row.indicator_type, row.indicator_value)
        if normal:  # a value that folds to nothing is left as stored
            groups[(row.source, row.indicator_type, normal)].append(row)

    deleted: list[int] = []
    updates: list[dict] = []
    for (_, _, normal), group in groups.items():
        if len(group) == 1 and group[0].indicator_value == normal:
            continue
        # The most recently seen row keeps its confidence, labels and raw_stix.
        keep = max(group, key=lambda r: (r.last_seen, r.id))
        expiries = [r.valid_until for r in group]
        updates.append(
            {
                "id": keep.id,
                "value": normal,
                "first_seen": min(r.first_seen for r in group),
                "last_seen": keep.last_seen,
                "valid_until": None if None in expiries else max(expiries),
            }
        )
        deleted.extend(r.id for r in group if r.id != keep.id)

    # Merged-away rows go first, so no rewrite collides with threat_indicators_unique.
    if deleted:
        conn.execute(
            text("DELETE FROM public.threat_indicators WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": deleted},
        )
    if updates:
        conn.execute(
            text("""
                UPDATE public.threat_indicators
                SET indicator_value = :value, first_seen = :first_seen,
                    last_seen = :last_seen, valid_until = :valid_until
                WHERE id = :id
                """),
            updates,
        )
    if (updates or deleted) and trail.trail_ready(conn):
        trail.append(
            conn,
            {
                trail.ACTION: "threat_indicators.normalized",
                trail.ACTOR: trail.SYSTEM,
                "rows_rewritten": len(updates),
                "rows_merged": len(deleted),
                "revision": revision,
            },
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
