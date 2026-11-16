"""Join 0074_approval_held and 0075_directory_groups into one head.

#4468 (held outside actions) and the directory-sync revisions were both
written against 0073, leaving two heads, and the API refuses to start on more
than one. Neither touches the other's tables, so there is nothing to run here.
"""

revision = "0076_merge_0075_heads"
down_revision = ("0074_approval_held", "0075_directory_groups")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
