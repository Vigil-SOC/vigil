"""Grant cases.export to built-in Admin and Manager."""

from core.upgrade.revision import run_sql

revision = "0108_cases_export"
down_revision = "0107_merge_0106_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
