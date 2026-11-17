"""Join the three dangling revisions into one head.

0083_merge_0081_heads (#4561) joined the 0081 series and nothing was built on
it. 0084_join_0083_heads (#4567) and 0084_merge_0083_heads (#4563) each joined
the same two 0083 revisions and landed side by side. That leaves three heads,
and the API refuses to start on more than one. None of the three runs any
DDL, so there is nothing to run here. A database at any of them upgrades
through this; neither 0084 file is removed, because a database may already
be stamped at either.
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
