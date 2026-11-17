"""Join the break-glass revision with the rest of the tree.

#4532 (break-glass accounts) added 0081_break_glass on 0080_merge_0079_heads
while the other 0081 revisions were joined, through 0083 to 0085, into
0085_join_0084_heads. That leaves two heads, and the API refuses to start on
more than one. 0085_join_0084_heads runs no DDL and 0081_break_glass only adds
users.break_glass_at, so there is nothing to run here.
"""

revision = "0086_join_break_glass"
down_revision = ("0081_break_glass", "0085_join_0084_heads")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
