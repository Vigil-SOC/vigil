"""A false-positive close and a noise mark can each store one cause.

Two nullable text columns. Nothing is backfilled: absence is null, and
``not_given`` is not a stored value. No check constraint.
"""

from core.upgrade.revision import run_sql

revision = "0071_false_positive_cause"
down_revision = "0070_investigation_replay"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
