"""The one investigation-model replay row (#4389)."""

from core.upgrade.revision import run_sql

revision = "0070_investigation_replay"
down_revision = "0069_dismissal_sample"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
