"""Grant skills.approve to built-in Admin."""

from core.upgrade.revision import run_sql

revision = "0082_skills_approve"
down_revision = "0081_skill_scans"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
