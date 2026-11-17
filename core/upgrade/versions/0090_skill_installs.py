"""Record which git repo and commit each installed skill came from."""

from core.upgrade.revision import run_sql

revision = "0090_skill_installs"
down_revision = "0089_join_0085_merge_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
