"""Join the three heads main was left with into one.

0083_merge_0081_heads (#4551), 0084_join_0083_heads (#4561) and
0084_merge_0083_heads (#4563) each joined their own set of parents and landed
side by side, and the API refuses to start on more than one head. None of them
runs anything, so there is nothing to run here. A database at any of them
upgrades through this.
"""

revision = "0085_join_0084_heads"
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
