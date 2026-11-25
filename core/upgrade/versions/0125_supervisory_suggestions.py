"""Supervisory model suggestions and their history.

Chains after ``0124_recheck_review``. Suggestions keep their card as made;
history is append-only.
"""

from core.upgrade.revision import run_sql

revision = "0125_supervisory_suggestions"
down_revision = "0124_recheck_review"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
