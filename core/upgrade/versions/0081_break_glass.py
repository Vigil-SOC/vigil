"""A break-glass mark on an account (#4532).

Adds ``users.break_glass_at``: set and cleared only by the host's
``scripts/reset_password.py``. No account is marked at the upgrade. Does not
edit an earlier revision.
"""

from core.upgrade.revision import run_sql

revision = "0081_break_glass"
down_revision = "0080_merge_0079_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
