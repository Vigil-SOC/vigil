"""Join the two 0083 revisions into one head.

0083_merge_0082_heads (#4561) and 0083_erased_personal_rows (#4558) each joined
0081 and 0082 and landed side by side, leaving two heads, and the API refuses to
start on more than one. Neither touches what the other does, so there is
nothing to run here. A database at either one upgrades through this.
"""

revision = "0084_join_0083_heads"
down_revision = ("0083_merge_0082_heads", "0083_erased_personal_rows")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
