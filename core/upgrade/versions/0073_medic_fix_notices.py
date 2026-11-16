"""Event names for a fix Medic can run (#4421).

Widens the notice event check. ``opened``, ``cleared`` and ``summary`` stay
the only names the #4338 path reads. Does not edit ``0068_medic_incidents``.
"""

from core.upgrade.revision import run_sql

revision = "0073_medic_fix_notices"
down_revision = "0072_merge_0071_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
