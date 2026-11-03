"""Only python -m core.upgrade creates objects in public, not the app's role."""

from core.upgrade.revision import run_sql

revision = "0022_runtime_role_no_create"
down_revision = "0021_federation_error_kind"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
