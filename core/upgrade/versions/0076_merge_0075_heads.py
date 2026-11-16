"""Join the approval-held and directory-group revisions into one head.

#4431 (hold approved actions across a stop) and #4424 (directory groups)
landed side by side, leaving two heads, and the API refuses to start on
more than one. Neither touches the other's tables, so there is nothing
to run here.
"""

revision = "0076_merge_0075_heads"
down_revision = ("0074_approval_held", "0075_directory_groups")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
