"""Join the three heads left on main into one.

0083_merge_0081_heads, 0084_join_0083_heads and 0084_merge_0083_heads landed side
by side (the last two both join the two 0083 revisions), and the API refuses to
start on more than one head. None of them runs anything, so there is nothing to
run here. A database at any of them upgrades through this.
"""

revision = "0085_merge_heads"
down_revision = (
    "0083_merge_0081_heads",
    "0084_join_0083_heads",
    "0084_merge_0083_heads",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
