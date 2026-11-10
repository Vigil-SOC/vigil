"""Single-use links: invites now, password resets later (#4226)."""

from core.upgrade.revision import run_sql

revision = "0055_account_links"
# Also joins the two 0054 heads main was left with.
down_revision = ("0054_merge_0053_heads", "0054_security_keys")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
