"""Join the erasure revision and the 0082 merge into one head.

#4558 (erased personal rows) and #4561 (the 0082 merge) both took
0081_normalize_threat_indicators and 0082_skills_approve as parents, leaving two
heads, and the API refuses to start on more than one. The merge runs nothing, so
there is nothing to run here either.
"""

revision = "0084_merge_0083_heads"
down_revision = ("0083_erased_personal_rows", "0083_merge_0082_heads")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
