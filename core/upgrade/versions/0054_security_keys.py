"""A person's security keys: one row per registered WebAuthn credential (#4234).

Also joins the two 0053 revisions, which both followed 0052.
"""

from core.upgrade.revision import run_sql

revision = "0054_security_keys"
down_revision = ("0053_erased_assignees", "0054_get_case_shell")


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
