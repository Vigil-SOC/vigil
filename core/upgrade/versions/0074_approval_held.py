"""A stop holds approved outside actions until a person releases them.

One boolean, default false. When the stored ceiling is already stopped,
the approved rows the executor runs are marked held so a resume does not
start them. Does not edit an earlier revision.
"""

from core.upgrade.revision import run_sql

revision = "0074_approval_held"
down_revision = "0073_medic_fix_notices"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
