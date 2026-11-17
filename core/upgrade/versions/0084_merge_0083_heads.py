"""Join the erasure and the threat-indicator/skill-approval merge into one head.

#4558 (0083_erased_personal_rows) was written against the two 0081/0082 heads
while #4561 (0083_merge_0082_heads) joined the same two, so main had two heads
again and the API refuses to start on more than one. Neither changes the
other's tables, so there is nothing to run here.
"""

revision = "0084_merge_0083_heads"
down_revision = ("0083_erased_personal_rows", "0083_merge_0082_heads")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
