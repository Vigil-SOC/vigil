"""Join the two 0012 revisions, both written against 0011, into one head.

#3191 (audit feed pause) and #3192 (closure channel) landed side by side,
leaving two heads, and the API refuses to start on more than one. Neither
touches the other's tables, so there is nothing to run here.
"""

revision = "0013_merge_0012_heads"
down_revision = ("0012_audit_feed_pause", "0012_closure_channel")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
