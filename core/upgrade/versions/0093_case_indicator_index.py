"""A Case is the subject of the shared IOC index (#4733).

Adds ``shared_iocs.case_id``, makes ``investigation_id`` nullable, and adds
``cases.indicators_synced_at`` and ``cases.indicators_stale_since``. No row is
copied or changed: a Case that exists now keeps ``indicators_synced_at`` NULL,
and matching reads its legacy rows until its first reconcile.
"""

from core.upgrade.revision import run_sql

revision = "0093_case_indicator_index"
down_revision = "0092_token_reminded_at"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
