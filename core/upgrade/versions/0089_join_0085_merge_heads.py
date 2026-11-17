"""Join 0085_merge_heads with the rest of the tree.

#4577 added 0085_merge_heads to join the three 0084 heads, while
0085_join_0084_heads already did that and 0086 to 0088 were built on it. That
leaves two heads, and the API refuses to start on more than one. Neither runs any
DDL, so there is nothing to run here. 0085_merge_heads is not removed, because a
database may already be stamped at it.
"""

revision = "0089_join_0085_merge_heads"
down_revision = ("0085_merge_heads", "0088_identifier_reservations")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
