"""A SIEM rule's link to the repo rule it came from (#3706)."""

from core.upgrade.revision import run_sql

revision = "0028_detection_rule_origin"
down_revision = "0027_detection_rule_checks"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
