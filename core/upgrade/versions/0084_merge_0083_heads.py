"""Join the erasure and threat-indicator/skill-approval merge revisions into one head.

#4558 (0083_erased_personal_rows) and #4561 (0083_merge_0082_heads) both landed on
0081_normalize_threat_indicators + 0082_skills_approve, leaving two heads, and the
API refuses to start on more than one. Nothing to run here.
"""

revision = "0084_merge_0083_heads"
down_revision = ("0083_erased_personal_rows", "0083_merge_0082_heads")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
